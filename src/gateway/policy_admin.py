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
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .policy import PolicyDoc, decide, invalidate_policy_cache, load_policy, DEFAULT_POLICY

ACTION_ACTION = ("allow", "route_local", "block")

# 5.2 表单的"内容类型" -> when 条件字段的映射（下拉框语义）
CONTENT_TYPE_FIELD = {
    "prompt": "text",           # 提示词
    "filename": "file.filename",  # 文件名
    "file": "file.text",        # 文件内容（含 OCR 抽取文本）
    "ocr": "file.text",         # OCR 同样落在 file.text（文件分支抽取）
}
MATCH_TYPE_OP = {
    "regex": "matches_regex",   # 正则
    "contains": "contains_any",  # 包含任一（比单条正则更好用，保留）
    "size": ">=",               # 文件大小（配 file.size 字段）
}


def _policy_path() -> Path:
    return Path(os.getenv("AI_GATEWAY_POLICY_PATH", "") or DEFAULT_POLICY)


def list_policies() -> List[Dict[str, Any]]:
    return [p.model_dump(exclude_none=True) for p in load_policy(_policy_path())]


def _validate_doc(doc: Dict[str, Any]) -> None:
    PolicyDoc.model_validate(doc)


def _write_doc(doc: Dict[str, Any]) -> None:
    _validate_doc(doc)
    text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    p = _policy_path()
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    invalidate_policy_cache()  # P1: 写后失效，读走 mtime 缓存


def upsert_policy(rule: Dict[str, Any], original_name: Optional[str] = None) -> Dict[str, Any]:
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
    _write_doc({"policies": policies})
    return rule


def delete_policy(name: str) -> bool:
    data = yaml.safe_load(_policy_path().read_text(encoding="utf-8")) or {}
    policies: List[Dict[str, Any]] = data.get("policies", []) if isinstance(data, dict) else []
    kept = [p for p in policies if p.get("name") != name]
    if len(kept) == len(policies):
        return False
    _write_doc({"policies": kept})
    return True


def build_when(content_type: str, match_type: str, match_value: Any) -> Dict[str, Any]:
    """5.2 表单语义 -> policy when 条件。
    content_type: prompt | filename | file | ocr
    match_type:   regex | contains | size
    match_value:  正则串 / 关键词列表 / 字节数
    """
    if content_type == "size" or match_type == "size":
        # 文件大小：仅对文件分支有意义
        return {"file.size >=": int(match_value)}
    field = CONTENT_TYPE_FIELD.get(content_type)
    if not field:
        raise ValueError(f"unknown content_type: {content_type}")
    op = MATCH_TYPE_OP.get(match_type)
    if not op:
        raise ValueError(f"unknown match_type: {match_type}")
    if op == "matches_regex":
        # 预编译校验正则合法性
        import re
        re.compile(str(match_value))
        return {f"{field} matches_regex": str(match_value)}
    if op == "contains_any":
        vals = match_value if isinstance(match_value, list) else [x.strip() for x in str(match_value).split(",") if x.strip()]
        if not vals:
            raise ValueError("contains_any needs at least one keyword")
        return {f"{field} contains_any": vals}
    raise ValueError(f"unsupported match_type: {match_type}")


def test_rule(text: str = "", filename: str = "", size: int = 0) -> Dict[str, Any]:
    """L1 规则测试（5.3 的 L1 半边）：构造 ctx 跑 decide()，返回命中详情。"""
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
    }
    hit = decide(load_policy(_policy_path()), ctx)
    return {
        "matched": hit.get("name") != "default_allow" or hit.get("action") != "allow",
        "rule": hit.get("name"),
        "action": hit.get("action"),
        "target": hit.get("target"),
        "description": hit.get("description", ""),
    }
