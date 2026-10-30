"""规则名中文映射两份表同步测试。

界面上渲染用 ``web/admin/src/ruleNames.jsx``，后端全文搜索用 ``src/gateway/rule_names.py``，
两份必须一致——历史上后端新增系统规则只改了一处，审计页就露出英文规则名
（2026-10 local_alias_bypass）。此测试钉死两份表的 key/value 完全相同。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JSX = ROOT / "web/admin/src/ruleNames.jsx"


def _jsx_src() -> str:
    return JSX.read_text(encoding="utf-8")


def _jsx_rule_zh() -> dict:
    m = re.search(r"export const RULE_ZH = \{(.*?)\n\};", _jsx_src(), re.S)
    assert m, "ruleNames.jsx 找不到 RULE_ZH"
    out = {}
    for k, v in re.findall(r"^\s*(?:['\"])?(\w+)(?:['\"])?\s*:\s*['\"]([^'\"]+)['\"]", m.group(1), re.M):
        out[k] = v
    assert out, "RULE_ZH 解析为空"
    return out


def _jsx_system_rules() -> set:
    m = re.search(r"const SYSTEM_RULES = new Set\(\[(.*?)\]\)", _jsx_src(), re.S)
    assert m, "ruleNames.jsx 找不到 SYSTEM_RULES"
    return set(re.findall(r"['\"](\w+)['\"]", m.group(1)))


def test_rule_zh_parity_jsx_vs_py():
    from src.gateway.rule_names import RULE_ZH as PY_RULE_ZH
    js = _jsx_rule_zh()
    assert js == PY_RULE_ZH, (
        f"两份 RULE_ZH 不一致：仅 jsx 有 {set(js) - set(PY_RULE_ZH)}，"
        f"仅 py 有 {set(PY_RULE_ZH) - set(js)}，"
        f"值不同 { {k: (js[k], PY_RULE_ZH[k]) for k in set(js) & set(PY_RULE_ZH) if js[k] != PY_RULE_ZH[k]} }"
    )


def test_system_rules_parity_jsx_vs_py():
    from src.gateway.rule_names import SYSTEM_RULES as PY_SYSTEM
    js = _jsx_system_rules()
    assert js == set(PY_SYSTEM), (
        f"两份 SYSTEM_RULES 不一致：仅 jsx 有 {js - set(PY_SYSTEM)}，"
        f"仅 py 有 {set(PY_SYSTEM) - js}"
    )
