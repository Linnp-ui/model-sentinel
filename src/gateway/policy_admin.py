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
    "prompt": "text",           # 提示词
    "filename": "file.filename",  # 文件名
    "file": "file.text",        # 文件内容（含 OCR 抽取文本）
    "ocr": "file.text",         # OCR 同样落在 file.text（文件分支抽取）
    "model": "model",           # 请求模型名（别名或真实名，三条入口都传 model=）
    "client": "client.key",     # 客户端登记名（api_key_map.name，不是密钥本身）
    # 布尔开关字段（配 match_type=bool，取值 true/false）
    "session_confidential": "session.confidential",
    "tainted_file": "tainted_file.hit",
    "unparsed_binary": "findings.unparsed_binary",
    "parse_failed": "findings.parse_failed",
    "ocr_empty": "findings.ocr_empty_and_image",
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

# 构造器可选项：真实有效的 (内容类型, 匹配类型, 标签)，前端下拉直接吃这份表。
# 不给「内容类型 × 匹配类型」的笛卡尔积（66 种）：其中 10 种布尔字段配
# 正则/包含会产出永不命中的规则——保存成功、干跑通过、线上永不触发，
# 静默失效比报错难查得多；余下的无意义组合只是让用户猜。
# size / hour_range 自带字段（不绑内容类型，见 build_when），故不在表内。
MATCH_CHOICES: List[Tuple[str, str, str]] = [
    ("prompt", "regex", "提示词 · 正则"),
    ("prompt", "contains", "提示词 · 包含任一"),
    ("prompt", "in", "提示词 · 枚举任一"),
    ("prompt", "not_in", "提示词 · 枚举皆非"),
    ("filename", "regex", "文件名 · 正则"),
    ("filename", "contains", "文件名 · 包含任一"),
    ("filename", "in", "文件名 · 枚举任一"),
    ("filename", "not_in", "文件名 · 枚举皆非"),
    ("file", "regex", "文件内容 · 正则"),
    ("file", "contains", "文件内容 · 包含任一"),
    ("file", "in", "文件内容 · 枚举任一"),
    ("file", "not_in", "文件内容 · 枚举皆非"),
    ("ocr", "regex", "OCR · 正则"),
    ("ocr", "contains", "OCR · 包含任一"),
    ("model", "contains", "模型名 · 包含任一"),
    ("model", "in", "模型名 · 枚举任一"),
    ("model", "not_in", "模型名 · 枚举皆非"),
    ("client", "in", "客户端登记名 · 枚举任一"),
    ("client", "not_in", "客户端登记名 · 枚举皆非"),
    ("session_confidential", "bool", "开关 · 会话涉密 = 是"),
    ("tainted_file", "bool", "开关 · 文件名污染 = 是"),
    ("unparsed_binary", "bool", "开关 · 二进制未解析 = 是"),
    ("parse_failed", "bool", "开关 · 解析失败 = 是"),
    ("ocr_empty", "bool", "开关 · OCR 空 = 是"),
]
MATCH_PAIRS = {(ct, mt) for ct, mt, _ in MATCH_CHOICES}
# 自带字段的匹配类型：内容类型对它们无意义（build_when 直接忽略该参数）
SELF_FIELD_MATCH = ("size", "hour_range")
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


def parse_when(when: Any) -> Optional[Dict[str, Any]]:
    """build_when 的逆：把已有 when 还原成构造器的 content_type/match_type/match_value。

    用于「编辑已存在的规则」时预填控件。认不出的形状（多条件 any/all、
    手写 when、!= 这类无构造器等价物的运算符）返回 None，调用方保持构造器
    空白——绝不用默认值去覆盖用户已经写好的 when。

    注：policy.yaml 里的开关规则把值写进了 key（`session.confidential == true: true`），
    引擎只取 dict value（policy._eval_cond 按 == 切 key 后比 v），照此解析。
    """
    if not isinstance(when, Mapping):
        return None
    # 单层 any/all 只是包壳，剥掉再看
    if len(when) == 1 and next(iter(when)) in ("any", "all"):
        items = when[next(iter(when))]
        if not isinstance(items, list) or len(items) != 1:
            return None
        return parse_when(items[0])
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
        raise ValueError("invalid policy yaml (no policies list)")
    _write_doc(doc, operator=operator, source=source)
    return len(doc["policies"])


def upsert_policy(rule: Dict[str, Any], original_name: Optional[str] = None,
                  operator: str = "") -> Dict[str, Any]:
    """新增/更新一条规则。original_name 给定时为重命名更新。校验失败抛 pydantic.ValidationError。"""
    data = yaml.safe_load(_policy_path().read_text(encoding="utf-8")) or {}
    policies: List[Dict[str, Any]] = data.get("policies", []) if isinstance(data, dict) else []
    name = str(rule.get("name", "")).strip()
    if not name:
        raise ValueError("rule name is required")
    # 唯一性：新建时禁止重名；更新同名规则放行；重命名时目标名不得已存在
    for p in policies:
        if p.get("name") == name and name != (original_name or ""):
            raise ValueError(f"rule name already exists: {name}")
    if original_name:
        policies = [p for p in policies if p.get("name") != original_name]
    policies = [p for p in policies if p.get("name") != name] + [rule]
    policies.sort(key=lambda x: int(x.get("priority", 100)))
    _write_doc({"policies": policies}, operator=operator)
    return rule


def delete_policy(name: str, operator: str = "") -> bool:
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
        raise ValueError(f"unknown content_type: {content_type}")
    op = MATCH_TYPE_OP.get(match_type)
    if not op:
        raise ValueError(f"unknown match_type: {match_type}")
    if op == "matches_regex":
        # 预编译校验正则合法性
        re.compile(str(match_value))
        return {f"{field} matches_regex": str(match_value)}
    if op in ("contains_any", "in", "not_in"):
        vals = match_value if isinstance(match_value, list) else [x.strip() for x in str(match_value).split(",") if x.strip()]
        if not vals:
            raise ValueError(f"{op} needs at least one value")
        return {f"{field} {op}": vals}
    if op == "==":  # bool 开关：非真值串一律 False
        return {f"{field} ==": str(match_value).strip().lower() in _TRUTHY}
    raise ValueError(f"unsupported match_type: {match_type}")


def test_rule(text: str = "", filename: str = "", size: int = 0, model: str = "",
              client_key: str = "", hour: Optional[int] = None) -> Dict[str, Any]:
    """L1 规则测试（5.3 的 L1 半边）：构造 ctx 跑 decide()，返回命中详情。

    model / client_key / hour 对应 main._review_text ctx 的 model / client.key / time.hour
    （三者都可匹配了，2026-10-08）。留空则该字段为空串 / 当前小时——干跑传空 = 走默认，
    不再是「规则必然不命中」的假阴性。
    """
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
        "findings": {},
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
