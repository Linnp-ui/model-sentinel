"""Policy Admin — L1 规则可视化管理（需求 5.1 / 5.2）。
src/gateway/policy_admin.py

直接操作 policy.yaml（仓库根，AI_GATEWAY_POLICY_PATH 可覆盖）：
- 列表 / 新增 / 更新 / 删除，全部走 Pydantic 强校验（PolicyDoc）后原子落盘，
  校验失败不动原文件（回滚语义）。
- 规则测试（5.3 的 L1 半边）：给定 text/filename/size 构造 ctx 跑 decide()，
  返回命中的规则与动作 —— 供 UI 调规则用。
- file.size 支持：main.py 的 files/check ctx 已注入 "size"（本模块同步支持），
  规则可写 {"file.size >=": 102400}。
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .policy import PolicyDoc, decide, invalidate_policy_cache, load_policy, DEFAULT_POLICY

# 5.2 表单的"内容类型" -> when 条件字段的映射（下拉框语义）
CONTENT_TYPE_FIELD = {
    "prompt": "text",           # 用户query
    "filename": "file.filename",  # 文件名
    "ext": "file.ext",          # 文件扩展名（引擎 file.ext in 精确匹配特判）
    "file": "file.text",        # 文件内容（含 OCR 抽取文本）
    "sheet_names": "file.sheet_names",  # 工作表名（list，仅文件分支注入）
    "headers": "file.headers",  # 表头行（list，仅文件分支注入）
    "watermark": "file.watermark",      # 水印文字（str，仅文件分支注入）
    "ocr": "file.text",         # OCR 同样落在 file.text（文件分支抽取）
    "model": "model",           # 请求模型名（别名或真实名，三条入口都传 model=）
    "client": "client.key",     # 客户端登记名（api_key_map.name，不是密钥本身）
    # 布尔开关字段（配 match_type=bool，取值 true/false）
    "session_confidential": "session.confidential",
    "tainted_file": "tainted_file.hit",
    "unparsed_binary": "findings.unparsed_binary",
    "parse_failed": "findings.parse_failed",
    "ocr_empty": "findings.ocr_empty_and_image",
    "remote_attachment": "findings.remote_attachment",
}
MATCH_TYPE_OP = {
    "regex": "matches_regex",   # 正则
    "contains": "contains_any",  # 包含任一（比单条正则更好用，保留）
    "size": ">=",               # 文件大小（配 file.size 字段）
    "in": "in",                 # 枚举任一（值逗号分隔）
    "not_in": "not_in",         # 枚举皆非
    "bool": "==",               # 布尔开关（值为 true/false/1/0/是/否）
}
_TRUTHY = ("1", "true", "yes", "on", "是", "开")

# 构造器可选项：真实有效的 (内容类型, 匹配类型, 标签, 悬停说明)，前端下拉直接吃这份表。
# 说明的口径：大白话 2-4 句，讲清四件事——匹配的是什么数据（从哪来）、匹配项怎么写、
# 怎么算命中（含大小写/整段相等这类非显然语义）、什么时候生效（纯文本/附件/图片的
# 适用范围）。不堆引擎术语；实现细节（NFKC/零宽剥离）不进说明，但真实行为
# （如 in 运算符的包含回退、watermark 实为解析文字前 500 字）必须如实写。
# 不给「内容类型 × 匹配类型」的笛卡尔积（66 种）：其中 10 种布尔字段配
# 正则/包含会产出永不命中的规则——保存成功、干跑通过、线上永不触发，
# 静默失效比报错难查得多；余下的无意义组合只是让用户猜。
# 表内只留现网 policy.yaml 实际在用的组合（2026-10-13 精简）：in/not_in、
# 非用户query regex、模型名/客户端登记名/OCR 字段已从下拉移除——引擎支持不变，
# policy.yaml 手写照样生效（hour_range 同理只走手写），构造器不再提供。
# 2026-10-23 补回文件扩展名 ext·in：「只匹配 Excel/PDF」是高频真实需求，
# 引擎对 file.ext in 本有精确匹配特判（policy._eval_cond），构造器此前无入口。
# size / hour_range / pii_score 自带字段（不绑内容类型，见 build_when），故不在表内。
MATCH_CHOICES: List[Tuple[str, str, str, str]] = [
    ("prompt", "regex", "用户query · 正则", "匹配项=正则表达式（Python re 语法）：请求文本里任何位置匹配到就算命中。例：AKIA[0-9A-Z]{16} 圈 AWS 密钥、[0-9]{17}[0-9Xx] 圈身份证号。适合格式固定、要精确圈定的内容；写不好正则建议改用「包含任一」。请求文本=chat 的提示词正文、文件预检的解析文字"),
    ("prompt", "contains", "用户query · 包含任一", "匹配项=逗号分隔的关键词：请求文本包含任意一个词就算命中（多词之间是「或」，例：工资表, 薪资）。不区分大小写，词在句子中间也算（「查看工资表」命中「工资表」）。要所有词都出现：拆成多行并在下方选「全部命中」"),
    ("filename", "contains", "文件名 · 包含任一", "匹配项=逗号分隔的关键词：文件名包含任意一个词就算命中（例：报表, 财务）。不区分大小写，salary_2024.xlsx 命中 salary。只对带附件的请求生效"),
    ("ext", "in", "文件扩展名 · 精确任一", "匹配项=逗号分隔的扩展名（不带点，例：xlsx, pdf）：附件扩展名与任意一个完全相等才命中（不区分大小写，不做子串——填 xlsx 不会命中 xls）。只对带附件的请求生效，纯文本请求扩展名为空、永不命中"),
    ("file", "contains", "文件内容 · 包含任一", "匹配项=逗号分隔的关键词：附件解析出的正文文字包含任意一个词就算命中（例：财务报表, 资产负债）。不区分大小写；图片 OCR 识别出的文字也落在这里"),
    ("sheet_names", "contains", "工作表名 · 包含任一", "匹配项=逗号分隔的关键词：任何一张工作表的名字包含任意一个词就算命中（例：工资, 预算）。只对能解析出表格的文件生效（xlsx/csv）"),
    ("headers", "contains", "表头 · 包含任一", "匹配项=逗号分隔的关键词：表头（表格第一行的列名）包含任意一个词就算命中（例：姓名, 身份证, 金额）。不区分大小写；只对能解析出表格的文件生效（xlsx/csv）"),
    ("watermark", "contains", "水印 · 包含任一", "匹配项=逗号分隔的关键词：文件解析文字的前 500 字包含任意一个词就算命中（例：机密, 保密, confidential）。注意：实际匹配的是解析文字开头 500 字（水印若在文字层会被带出），不是独立水印检测；全文匹配请用「文件内容」"),
    ("session_confidential", "bool", "开关 · 会话涉密 = 是", "匹配项=是/否：同一会话内曾有请求被判定涉密时（会话按 X-Session-ID 识别，标记 7 天有效），填「是」命中、填「否」在未涉密时命中。会话级联动：一次文件判密，同会话后续的纯文本请求也跟着转本地"),
    ("tainted_file", "bool", "开关 · 文件名污染 = 是", "匹配项=是/否：同一 KEY 下曾判定机密的文件名再次出现在请求里时（污染标记按 KEY 隔离、7 天有效），填「是」命中。防「换个会话提一句机密文件名」的绕过"),
    ("unparsed_binary", "bool", "开关 · 二进制未解析 = 是", "匹配项=是/否：文件是二进制但没解析出任何文字（扩展名伪装、未知格式、文件损坏）时，填「是」命中。fail-closed 兜底：认不出的文件宁可转本地，不直接放行出境"),
    ("parse_failed", "bool", "开关 · 解析失败 = 是", "匹配项=是/否：文件解析过程失败（格式损坏/内容异常）时，填「是」命中。fail-closed 兜底，同上"),
    ("ocr_empty", "bool", "开关 · OCR 空 = 是", "匹配项=是/否：图片走了 OCR 但没识别出任何文字（纯扫描件、图里无字、OCR 引擎缺失）时，填「是」命中。fail-closed 兜底，同上"),
    ("remote_attachment", "bool", "开关 · 远端附件 = 是", "匹配项=是/否：请求带了远端 URL 附件（http(s) 的 image_url / document source.url 等）时，填「是」命中。这类附件的字节不过网关、无法预检，fail-closed 兜底：看不见的内容转本地，不直接放行出境"),
]
MATCH_PAIRS = {(ct, mt) for ct, mt, *_ in MATCH_CHOICES}
# 自带字段的匹配类型：内容类型对它们无意义（build_when 直接忽略该参数）
SELF_FIELD_MATCH = ("size", "hour_range", "pii_score")
# 字段 -> 内容类型（parse_when 反查用）。一个字段可能对应多个 content_type
# （file.text 同时是 file 和 ocr），取 CONTENT_TYPE_FIELD 里的首个，结果确定。
_CONTENT_TYPE_BY_FIELD = {f: ct for ct, f in reversed(list(CONTENT_TYPE_FIELD.items()))}
BOOL_FIELDS = frozenset(CONTENT_TYPE_FIELD[ct] for ct, mt in MATCH_PAIRS if mt == "bool")
# 条件 key 形如 "<field> <op>"，但 policy.yaml 里开关规则把值也写进了 key
# （"session.confidential == true": true）。字面量部分允许出现，交给调用方取值。
_COND_RE = re.compile(
    r"(?P<field>[A-Za-z_][\w.]*)\s+(?P<op>not_in|matches_regex|contains_any|==|!=|>=|<=|in)(?:\s+(?P<lit>.+))?"
)


def match_pair_ok(content_type: str, match_type: str) -> bool:
    """构造器组合是否合法。self-field 不绑内容类型，其余必须在 MATCH_PAIRS 内。"""
    return match_type in SELF_FIELD_MATCH or (content_type, match_type) in MATCH_PAIRS


def _fmt_value(v: Any) -> Any:
    """列表值回成构造器输入串（逗号分隔），其余原样。"""
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return v


def _fmt_bool(v: Any) -> str:
    return "true" if v else "false"

def _hour_bound(cond: Any) -> Optional[int]:
    """hour_range 壳的单条边界识别（time.hour >= / <= 整数）。"""
    if not isinstance(cond, Mapping) or len(cond) != 1:
        return None
    m = _COND_RE.fullmatch(str(next(iter(cond))).strip())
    if not m or m.group("field") != "time.hour" or m.group("op") not in (">=", "<="):
        return None
    v = next(iter(cond.values()))
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_when(when: Any) -> Optional[Dict[str, Any]]:
    """build_when / build_when_group 的逆：把已有 when 还原成构造器控件值。

    用于「编辑已存在的规则」时预填控件。返回形状：
    - 单条件：{"content_type", "match_type", "match_value"}
    - 条件组（any/all 多行，每行都能逆解析）：{"combiner": "any"|"all", "rows": [...]}
    认不出的形状（嵌套 any/all、手写 when、!= 这类无构造器等价物的运算符）
    返回 None，调用方保持构造器空白——绝不用默认值去覆盖用户已经写好的 when。

    注：policy.yaml 里的开关规则把值写进了 key（`session.confidential == true: true`），
    引擎只取 dict value（policy._eval_cond 按 == 切 key 后比 v），照此解析。
    """
    if not isinstance(when, Mapping):
        return None
    # 单层 any/all：一条=包壳剥掉再看；多条=条件组逐行逆解析（组内再嵌套认不出）
    if len(when) == 1 and next(iter(when)) in ("any", "all"):
        combiner = next(iter(when))
        items = when[combiner]
        if not isinstance(items, list):
            return None
        if len(items) == 1:
            return parse_when(items[0])
        # hour_range 自身就是 any/all 壳（22-6 跨午夜 -> any, 9-18 同日 -> all），
        # 优先识别为时段构造器，避免被当成无法逆解析的组。
        if len(items) == 2:
            bounds = [_hour_bound(c) for c in items]
            if all(b is not None for b in bounds):
                return {"content_type": "hour_range", "match_type": "hour_range",
                        "match_value": f"{bounds[0]}-{bounds[1]}"}
        if len(items) >= 2:
            rows = [parse_when(c) for c in items]
            if all(isinstance(r, Mapping) and "combiner" not in r for r in rows):
                return {"combiner": combiner, "rows": rows}
        return None
    if len(when) != 1:
        return None
    k, v = next(iter(when.items()))
    m = _COND_RE.fullmatch(str(k).strip())
    if not m:
        return None
    field, op, lit = m.group("field"), m.group("op"), m.group("lit")
    if op == "==":
        ct = _CONTENT_TYPE_BY_FIELD.get(field)
        if ct is None or field not in BOOL_FIELDS:
            return None  # 非开关字段的 == 没有构造器等价物
        return {"content_type": ct, "match_type": "bool", "match_value": _fmt_bool(v)}
    if op == ">=" and field == "file.size":
        return {"content_type": "size", "match_type": "size", "match_value": v}
    if op == ">=" and field == "findings.weighted_pii_score":
        return {"content_type": "pii_score", "match_type": "pii_score", "match_value": v}
    mt = {"matches_regex": "regex", "contains_any": "contains", "in": "in", "not_in": "not_in"}.get(op)
    if not mt:
        return None  # != / <= 在构造器里没有对应项
    ct = _CONTENT_TYPE_BY_FIELD.get(field)
    if ct is None or not match_pair_ok(ct, mt):
        return None
    return {"content_type": ct, "match_type": mt, "match_value": _fmt_value(v if lit is None else lit)}


def _policy_path() -> Path:
    return Path(os.getenv("AI_GATEWAY_POLICY_PATH", "") or DEFAULT_POLICY)


def list_policies() -> List[Dict[str, Any]]:
    return [p.model_dump(exclude_none=True) for p in load_policy(_policy_path())]


def _validate_doc(doc: Dict[str, Any]) -> None:
    PolicyDoc.model_validate(doc)


def _snapshot_old(operator: str = "", source: str = "save") -> None:
    """写盘前把现网 policy.yaml 存进版本表（可回滚）。fail-open：快照失败不挡保存。

    空文件/空 policies 不留版本（没有可回滚的东西）。"""
    try:
        p = _policy_path()
        if not p.exists():
            return
        old = p.read_text(encoding="utf-8")
        doc = yaml.safe_load(old) or {}
        n = len(doc.get("policies", [])) if isinstance(doc, dict) else 0
        if not old.strip() or n == 0:
            return
        from .admin_store import get_admin_store
        get_admin_store().save_policy_version(old, operator=operator, source=source, rule_count=n)
    except Exception as e:
        print(f"[policy_admin] version snapshot failed (not blocking save): {e!r}")


def _write_doc(doc: Dict[str, Any], operator: str = "", source: str = "save") -> None:
    _validate_doc(doc)
    _snapshot_old(operator=operator, source=source)
    text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    p = _policy_path()
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    invalidate_policy_cache()  # P1: 写后失效，读走 mtime 缓存


def restore_policy_yaml(yaml_text: str, operator: str = "", source: str = "rollback") -> int:
    """回滚：把历史快照写回 policy.yaml（同样走 Pydantic 校验 + 原子落盘 + 现状再快照，可反悔）。"""
    doc = yaml.safe_load(yaml_text)
    if not isinstance(doc, dict) or not isinstance(doc.get("policies"), list):
        raise ValueError("policy.yaml 不合法：缺少 policies 列表")
    _write_doc(doc, operator=operator, source=source)
    return len(doc["policies"])


_FIELD_ZH = {"action": "动作", "description": "描述", "when": "条件", "target": "定向"}


def _rules_map(yaml_text: str) -> dict:
    """policy.yaml 文本 → {规则名: 规则 dict}；结构不合法 raise。"""
    doc = yaml.safe_load(yaml_text or "") or {}
    if not isinstance(doc, dict) or not isinstance(doc.get("policies"), list):
        raise ValueError("policy.yaml 不合法：缺少 policies 列表")
    out = {}
    for p in doc["policies"]:
        if isinstance(p, dict) and p.get("name"):
            out[p["name"]] = p
    return out


def diff_policy_texts(old_text: str, new_text: str) -> list:
    """两份 policy.yaml 快照按规则名对比，返回中文变更摘要（空列表 = 无变化）。

    历史版本展示用：每版快照的「变更」= 与下一更新状态（最新版则与现网文件）的差。
    """
    try:
        old, new = _rules_map(old_text), _rules_map(new_text)
    except Exception:
        return ["无法解析，无法对比"]
    changes = []
    for name in new:
        if name not in old:
            changes.append(f"新增「{name}」")
    for name in old:
        if name not in new:
            changes.append(f"删除「{name}」")
    for name, p in old.items():
        q = new.get(name)
        if q is None:
            continue
        if p.get("priority") != q.get("priority"):
            changes.append(f"「{name}」优先级 {p.get('priority')}→{q.get('priority')}")
        _IGNORE_DIFF_KEYS = {"name", "priority", "priority_locked", "locked",
                             "locked_priority", "pinned", "fixed_priority"}
        others = [k for k in set(p) | set(q)
                  if k not in _IGNORE_DIFF_KEYS and p.get(k) != q.get(k)]
        if others:
            changes.append(f"「{name}」改: " + "、".join(_FIELD_ZH.get(k, k) for k in sorted(others)))
    return changes


def upsert_policy(rule: Dict[str, Any], original_name: Optional[str] = None,
                  operator: str = "") -> Dict[str, Any]:
    """新增/更新一条规则。original_name 给定时为重命名更新。校验失败抛 pydantic.ValidationError。"""
    data = yaml.safe_load(_policy_path().read_text(encoding="utf-8")) or {}
    policies: List[Dict[str, Any]] = data.get("policies", []) if isinstance(data, dict) else []
    name = str(rule.get("name", "")).strip()
    if not name:
        raise ValueError("规则名称不能为空")
    # 唯一性：新建时禁止重名；更新同名规则放行；重命名时目标名不得已存在
    for p in policies:
        if p.get("name") == name and name != (original_name or ""):
            raise ValueError(f"规则名已存在：{name}")
    if original_name:
        pass
    # 优先级锁定：已锁定规则的 priority 不允许被静默改写；除非本次显式解锁（priority_locked=false）
    target_name = original_name or name
    existing = next((p for p in policies if p.get("name") == target_name), None)
    locked_fields = ("priority_locked", "locked", "locked_priority", "pinned", "fixed_priority")
    was_locked = bool(existing and any(existing.get(k) for k in locked_fields))
    new_locked = any(rule.get(k) for k in locked_fields)
    if was_locked and new_locked and existing.get("priority") != rule.get("priority"):
        raise ValueError("该规则优先级已锁定，修改 priority 请先取消锁定")
    policies = [p for p in policies if p.get("name") != original_name]
    policies = [p for p in policies if p.get("name") != name] + [rule]
    policies.sort(key=lambda x: int(x.get("priority", 100)))
    _write_doc({"policies": policies}, operator=operator)
    return rule


def delete_policy(name: str, operator: str = "") -> bool:
    # 前端 RulesPage 已隐藏「default_allow」的删除按钮，后端同样拒（此前只有前端挡，
    # 直接打 API 能删掉兜底规则 → 审计/干跑再没有可归因的default_allow）
    if name == "default_allow":
        raise ValueError("default_allow是兜底规则，不可删除")
    data = yaml.safe_load(_policy_path().read_text(encoding="utf-8")) or {}
    policies: List[Dict[str, Any]] = data.get("policies", []) if isinstance(data, dict) else []
    kept = [p for p in policies if p.get("name") != name]
    if len(kept) == len(policies):
        return False
    _write_doc({"policies": kept}, operator=operator)
    return True


def build_when(content_type: str, match_type: str, match_value: Any) -> Dict[str, Any]:
    """5.2 表单语义 -> policy when 条件。
    content_type: 见 CONTENT_TYPE_FIELD（文本/文件名/模型名/客户端key/布尔开关…）
    match_type:   见 MATCH_TYPE_OP；另有 size（配 file.size）、hour_range（配 time.hour）
    match_value:  正则串 / 关键词列表 / 字节数 / 枚举值 / true|false / "起-止"

    组合合法性见 MATCH_CHOICES / match_pair_ok：布尔字段配 regex/contains
    会产出永不命中的 when，这里直接拒，不让静默失效的规则落盘。
    """
    if not match_pair_ok(content_type, match_type):
        raise ValueError(f"内容类型 {content_type} 不支持匹配类型 {match_type}")
    if match_type == "size":
        # 文件大小：自带字段，仅对文件分支有意义
        return {"file.size >=": int(match_value)}
    if match_type == "pii_score":
        # 加权 PII 评分：自带字段，仅对 findings.weighted_pii_score 生效
        return {"findings.weighted_pii_score >=": int(match_value)}
    if match_type == "hour_range":
        # 本地时段区间，自带字段（对齐 size 的做法）："9-18" 同日 -> all；"22-6" 跨午夜 -> any
        m = re.fullmatch(r"\s*(\d{1,2})\s*[-~]\s*(\d{1,2})\s*", str(match_value))
        if not m:
            raise ValueError('hour_range 需要 "起-止"，如 9-18 或 22-6')
        a, b = int(m.group(1)), int(m.group(2))
        if not (0 <= a <= 23 and 0 <= b <= 23):
            raise ValueError("hour_range 取值需在 0-23")
        if a == b:
            raise ValueError("hour_range 起止相同等于全天 24h，用默认规则即可")
        lo_hi = [{"time.hour >=": a}, {"time.hour <=": b}]
        return {"all" if a < b else "any": lo_hi}
    field = CONTENT_TYPE_FIELD.get(content_type)
    if not field:
        raise ValueError(f"未知 content_type：{content_type}")
    op = MATCH_TYPE_OP.get(match_type)
    if not op:
        raise ValueError(f"未知 match_type：{match_type}")
    if op == "matches_regex":
        # 预编译校验正则合法性
        re.compile(str(match_value))
        return {f"{field} matches_regex": str(match_value)}
    if op in ("contains_any", "in", "not_in"):
        vals = match_value if isinstance(match_value, list) else [x.strip() for x in str(match_value).split(",") if x.strip()]
        if not vals:
            raise ValueError(f"{op} 至少需要一个值")
        return {f"{field} {op}": vals}
    if op == "==":  # bool 开关：非真值串一律 False
        return {f"{field} ==": str(match_value).strip().lower() in _TRUTHY}
    raise ValueError(f"不支持的 match_type：{match_type}")


def build_when_group(combiner: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """条件组（多行 字段+匹配方式+匹配项，any/all 组合）-> when。

    单行退化为 build_when（不加壳，与单条件规则同形状，老 match 兼容）；
    多行 -> {"any"/"all": [每行条件]}，每行各走一遍 build_when 的校验
    （组合合法性/正则/至少一个值）。hour_range 自身产出 any/all 壳，
    展不平进组内列表，直接拒。
    """
    if combiner not in ("any", "all"):
        raise ValueError("combiner 必须是 any 或 all")
    if not isinstance(rows, list) or not rows:
        raise ValueError("条件组至少要一行条件")
    if len(rows) == 1:
        r = rows[0] if isinstance(rows[0], Mapping) else {}
        return build_when(r.get("content_type", ""), r.get("match_type", ""), r.get("match_value"))
    conds = []
    for r in rows:
        if not isinstance(r, Mapping) or r.get("match_type") == "hour_range":
            raise ValueError("hour_range 不能进条件组（它自身就是 any/all 壳）")
        w = build_when(r.get("content_type", ""), r.get("match_type", ""), r.get("match_value"))
        if len(w) != 1:
            raise ValueError("条件组每行必须是单条件")
        conds.append(w)
    return {combiner: conds}


def test_rule(text: str = "", filename: str = "", size: int = 0, model: str = "",
              client_key: str = "", hour: Optional[int] = None) -> Dict[str, Any]:
    """L1 规则测试（5.3 的 L1 半边）：构造 ctx 跑 decide()，返回命中详情。

    model / client_key / hour 对应 main._review_text ctx 的 model / client.key / time.hour
    （三者都可匹配了，2026-10-08）。留空则该字段为空串 / 当前小时——干跑传空 = 走默认，
    不再是「规则必然不命中」的假阴性。

    findings 走 inspect_text(text)（2026-10-19）：此前恒填空，按
    findings.weighted_pii_score 判的规则在干跑里永不命中——实测 AKIA 样本干跑报
    「拦截密钥 block」，真实请求是「隐私加权转内网 route_local」（密钥类权重 85-100 ≥60）。
    没有文件字节，故只带正文 findings（生产文件分支还会并 inspect_file 的 findings）。
    """
    from .inspection import inspect_text
    ctx = {
        "text": text or "",
        "file": {
            "ext": (filename.rsplit(".", 1)[-1] if "." in (filename or "") else ""),
            "filename": filename or "",
            "headers": [],
            "sheet_names": [],
            "text": text or "",
            "watermark": "",
            "size": int(size or 0),
        },
        "findings": inspect_text(text) if text else {},
        "session": {"confidential": False},
        "model": model or "",
        "client": {"key": client_key or ""},
        "time": {"hour": int(hour) if hour is not None else datetime.now().hour},
    }
    hit = decide(load_policy(_policy_path()), ctx)
    return {
        "matched": hit.get("name") != "default_allow" or hit.get("action") != "allow",
        "rule": hit.get("name"),
        "action": hit.get("action"),
        "target": hit.get("target"),
        "description": hit.get("description", ""),
    }
