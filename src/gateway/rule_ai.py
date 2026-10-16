"""Rule AI — L1 匹配条件的 AI 生成（需求 5.2「匹配条件（AI生成）」）。
src/gateway/rule_ai.py

管理员输入自然语言描述（如「工资表这类文件不能出外网」），调本地 L2 模型
（Qwen3-4B，small_model 同一端点）生成整组匹配条件：字段（content_type）+
匹配方式 + 匹配项，通常 1 行、跨字段需求 2-4 行（any/all）。只生成不落库 ——
UI 预览确认后由管理员保存，生成失败可手动填写（兜底）。
模型输出是幻觉高危区：parse_suggestion 逐行走 policy_admin.build_when
（组合白名单/正则/非空值）校验，认不出的整组拒掉，不产出静默失效的规则。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

import httpx

from . import policy_admin

SMALL_MODEL_URL = os.getenv("SMALL_MODEL_URL", "http://10.0.0.10:8002/v1/chat/completions")
SMALL_MODEL_NAME = os.getenv("SMALL_MODEL_NAME", "Qwen3-4B-Instruct-2507")
TIMEOUT = float(os.getenv("SMALL_MODEL_TIMEOUT", "10"))
# 生成采样温度：高温度让同一（尤其模糊）描述每次选出不同的关键词/字段组合；
# 输出仍逐行过 parse_suggestion/build_when 整组校验，抽坏了是显式失败（UI 手填兜底），
# 不会静默落坏规则
GEN_TEMPERATURE = float(os.getenv("RULE_AI_TEMPERATURE", "0.8"))

# 字段表与 policy_admin.MATCH_CHOICES 同源（唯一真源），提示词不手抄一份：
# 构造器加了字段/匹配方式，AI 可选集自动跟上。
def _field_table() -> str:
    seen: Dict[str, str] = {}
    for ct, _mt, label, _desc in policy_admin.MATCH_CHOICES:
        seen.setdefault(ct, label.split(" · ")[0])
    return "\n".join(
        f"- {ct} {seen[ct]}：{'/'.join(sorted({m for c, m, *_ in policy_admin.MATCH_CHOICES if c == ct}))}"
        for ct in seen
    )

_FIELD_TABLE = _field_table()
SYSTEM_PROMPT = f"""你是企业网关的规则生成助手。管理员会描述一条审查规则的需求，你需要输出严格 JSON：
{{"rows": [{{"content_type": 字段, "match_type": 匹配方式, "value": 匹配项}}], "combiner": "any"|"all", "explanation": "一句话说明"}}

可用字段（content_type）及其匹配方式（match_type）：
{_FIELD_TABLE}

规则：
- 默认只出 1 行；只有需求明确跨多个字段（如财务表格=工作表名+表头+文件名）才出 2-4 行，combiner 选 any（任一命中）或 all（全部命中）
- 固定格式（密钥/身份证/手机号/卡号等）用 regex，value 是单个正则字符串（Python re 语法）
- 关键词类用 contains，value 是关键词字符串数组（2-8 个）
- explanation 用中文，不超过 30 字
只输出 JSON，不要输出其他内容。

示例：需求「工资表这类财务文件不能出外网」
{{"rows": [{{"content_type": "sheet_names", "match_type": "contains", "value": ["工资", "薪资", "财务"]}}, {{"content_type": "headers", "match_type": "contains", "value": ["姓名", "身份证", "工资", "金额"]}}, {{"content_type": "filename", "match_type": "contains", "value": ["工资", "salary", "财务"]}}], "combiner": "any", "explanation": "工作表名/表头/文件名命中任一财务词"}}"""


def _gen_user_message(description: str, exclude: Optional[list] = None,
                      scopes: Optional[list] = None) -> str:
    """生成用 user 消息：描述 +（scopes 非空时）覆盖范围限定 +（exclude 非空时）换一组词。

    scopes=允许的 content_type 集合（文件名/用户query 等）；exclude 只该传关键词类
    （contains/in/not_in）的值，regex/bool 的匹配项是天然固定的——由调用方过滤。"""
    user = (description or "").strip()
    sc = [str(s).strip() for s in (scopes or []) if str(s).strip()]
    if sc:
        user += "\n覆盖范围限定：本次只允许生成以下字段的匹配条件：" + "、".join(sc)
    excl = [str(x).strip() for x in (exclude or []) if str(x).strip()]
    if excl:
        user += "\n注意：以下匹配项上次已生成过，这次请换一组不同的词（仍须满足上述需求）：" + "、".join(excl[:40])
    return user


def _check_scopes(rows: list, scopes: Optional[list]) -> None:
    """覆盖范围兜底：提示词约束是软约束，模型仍可能越界出字段——越界整组拒（同幻觉处理）。"""
    if not scopes:
        return
    allowed = {str(s).strip() for s in scopes if str(s).strip()}
    bad = sorted({r["content_type"] for r in rows if r["content_type"] not in allowed})
    if bad:
        raise RuntimeError(f"rows outside scope {sorted(allowed)}: {bad}")


def parse_suggestion(content: str) -> Dict[str, Any]:
    """模型输出 -> {rows: [{content_type, match_type, value}], combiner, explanation}。

    兼容旧格式 {match_type, value}（按 prompt 单行处理）。每行必须通过
    policy_admin.build_when 校验（组合白名单/正则合法/值非空），否则整组
    RuntimeError——宁可让用户手填，也不落静默失效的规则。"""
    m = re.search(r"\{.*\}", content or "", re.S)
    if not m:
        raise RuntimeError(f"model returned no json: {str(content)[:200]}")
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"model json invalid: {e}") from e
    # 旧格式兜底：没给字段就当提示词单行
    if not (isinstance(obj.get("rows"), list) and obj["rows"]):
        mt, value = obj.get("match_type"), obj.get("value")
        if mt not in ("regex", "contains", "in", "not_in", "bool") or not value:
            raise RuntimeError(f"bad suggestion: {obj}")
        obj = {"rows": [{"content_type": "prompt", "match_type": mt, "value": value}],
               "combiner": "any", "explanation": obj.get("explanation", "")}
    combiner = obj.get("combiner") or "any"
    if combiner not in ("any", "all"):
        raise RuntimeError(f"bad combiner: {combiner}")
    if not 1 <= len(obj["rows"]) <= 4:
        raise RuntimeError(f"bad row count: {len(obj['rows'])}")
    rows = []
    for r in obj["rows"]:
        if not isinstance(r, dict):
            raise RuntimeError(f"bad row: {r!r}")
        ct, mt, value = str(r.get("content_type", "")), str(r.get("match_type", "")), r.get("value")
        if not value:
            raise RuntimeError(f"empty value in row: {r!r}")
        try:
            policy_admin.build_when(ct, mt, value)  # 组合/正则/非空值 一次校验
        except (ValueError, re.error) as e:
            raise RuntimeError(f"bad row ({ct}/{mt}): {e}") from e
        if mt == "regex":
            value = str(value)
        elif mt != "bool":
            value = [str(x) for x in (value if isinstance(value, list) else [value])]
        rows.append({"content_type": ct, "match_type": mt, "value": value})
    return {"rows": rows, "combiner": combiner, "explanation": str(obj.get("explanation", ""))[:100]}


async def generate_rule_suggestion(description: str, exclude: Optional[list] = None,
                                   scopes: Optional[list] = None) -> Dict[str, Any]:
    """返回 {rows, combiner, explanation}；模型不可用/输出不合法时抛 RuntimeError。

    exclude=已生成的匹配项（前端重复点击「换一组」时传入），提示模型换词；
    scopes=覆盖范围限定（允许生成的 content_type 集合），越界输出整组拒。"""
    if not (description or "").strip():
        raise ValueError("description is required")
    # L2 关闭时（测试/降级）直接失败，让 UI 走手动填写兜底
    if os.getenv("SMALL_MODEL_ENABLED", "true").lower() != "true":
        raise RuntimeError("L2 small model is disabled (SMALL_MODEL_ENABLED=false)")
    client = httpx.AsyncClient(timeout=TIMEOUT, trust_env=False)
    try:
        resp = await client.post(SMALL_MODEL_URL, json={
            "model": SMALL_MODEL_NAME,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _gen_user_message(description, exclude, scopes)},
            ],
            "temperature": GEN_TEMPERATURE,
            "max_tokens": 500,
        })
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"] or ""
        out = parse_suggestion(content)
        _check_scopes(out["rows"], scopes)
        return out
    except httpx.HTTPError as e:
        raise RuntimeError(f"L2 model unreachable: {e}") from e
    finally:
        await client.aclose()
