"""内部规则名 → 中文显示名（后端全文搜索用）。

界面渲染用的一张同名表在 ``web/admin/src/ruleNames.jsx``；本表是后端那份，
存在的理由：用户搜的是界面上看到的中文（"L2 判密"），而 ``audit_logs.rule``
存的是内部英文标识（``l2_confidential``），只按原始值搜必然搜不到。

改这张表时前端那张也要同步（两份都只加不改，冲突面很小）。
"""

RULE_ZH = {
    "default_allow": "默认放行",
    "allow": "放行",
    "route_local": "本地路由",
    "block": "阻断",
    "l2_confidential": "L2 判密",
    "l2_unavailable": "L2 不可用",
    "first_chunk_fallback": "首块转内网",
    "prompt_too_long_fallback": "长提示转内网",
    "key_blacklist": "密钥黑名单",
    "encoded_keyword": "编码关键词转内网",
    "review_text_overflow": "超审查预算转内网",
    # 历史旧规则名 → 现名
    "financial_local_only": "财务表转内网",
    "drawing_local_only": "图纸转内网",
    "pii_weighted_route_local": "隐私加权转内网",
    "pii_critical_block": "高危隐私拦截",
    "block_secrets": "拦截密钥",
    "session_route_local": "会话机密转内网",
    "tainted_file_ref": "文件名转内网",
    "未解析二进制转内网": "二进制转内网",
    "文件名污染转内网": "文件名转内网",
    "empty_image": "空图转内网",
}

# 系统内置判定（网关自己的 fail-safe / 降级 / L2 结论），不是 policy 里可配置的规则
SYSTEM_RULES = frozenset({
    "l2_confidential", "l2_unavailable",
    "encoded_keyword", "review_text_overflow",
    "first_chunk_fallback", "prompt_too_long_fallback",
})


def rule_zh(name: str) -> str:
    """中文显示名（无映射时原样返回）。"""
    raw = (name or "").strip()
    return RULE_ZH.get(raw, raw)


def rule_match_text(name: str) -> str:
    """搜索用文本：原始名 + 中文显示名（含「（系统内置）」标记），任一命中即可。

    用户搜「判密」/「L2 判密」都能命中 rule=l2_confidential 的行。
    """
    raw = (name or "").strip()
    if not raw:
        return ""
    zh = RULE_ZH.get(raw, "")
    if raw in SYSTEM_RULES:
        zh = (zh + "（系统内置）").strip()
    return " ".join(x for x in (raw, zh) if x)
