"""Rule AI — L1 匹配项的 AI 生成（需求 5.2「匹配项（AI生成）」）。
src/gateway/rule_ai.py

管理员输入自然语言描述（如「拦截含 AWS 密钥的请求」），调本地 L2 模型
（Qwen3-4B，small_model 同一端点）生成正则/关键词建议。只生成不落库 ——
UI 预览确认后由管理员保存，生成失败可手动填写（兜底）。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict

import httpx

SMALL_MODEL_URL = os.getenv("SMALL_MODEL_URL", "http://10.0.0.10:8002/v1/chat/completions")
SMALL_MODEL_NAME = os.getenv("SMALL_MODEL_NAME", "Qwen3-4B-Instruct-2507")
TIMEOUT = float(os.getenv("SMALL_MODEL_TIMEOUT", "10"))

SYSTEM_PROMPT = """你是企业网关的规则生成助手。管理员会描述一条审查规则的需求，你需要输出严格 JSON：
{"match_type": "regex"|"contains", "value": <string 或 string数组>, "explanation": "一句话说明"}

规则：
- 精确模式（密钥、身份证号、手机号、卡号等有固定格式的）用 regex，value 是单个正则字符串（Python re 语法）
- 模糊模式（机密/财务/水印等关键词类）用 contains，value 是关键词字符串数组（2-8 个）
- explanation 用中文，不超过 30 字
只输出 JSON，不要输出其他内容。"""


async def generate_rule_suggestion(description: str) -> Dict[str, Any]:
    """返回 {match_type, value, explanation}；模型不可用/输出不合法时抛 RuntimeError。"""
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
                {"role": "user", "content": description.strip()},
            ],
            "temperature": 0.2,
            "max_tokens": 200,
        })
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"] or ""
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            raise RuntimeError(f"model returned no json: {content[:200]}")
        obj = json.loads(m.group(0))
        match_type = obj.get("match_type")
        value = obj.get("value")
        if match_type not in ("regex", "contains") or not value:
            raise RuntimeError(f"bad suggestion: {obj}")
        if match_type == "regex":
            re.compile(str(value))  # 校验正则合法性
            value = str(value)
        else:
            value = [str(x) for x in (value if isinstance(value, list) else [value])]
        return {
            "match_type": match_type,
            "value": value,
            "explanation": str(obj.get("explanation", ""))[:100],
        }
    except httpx.HTTPError as e:
        raise RuntimeError(f"L2 model unreachable: {e}") from e
    finally:
        await client.aclose()
