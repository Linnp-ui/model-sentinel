"""
Secret redaction utility — P2 low-risk item #6.
src/gateway/redaction.py

目的: audit / admin 响应里如果出现密钥/私钥, 立刻 mask 掉, 防止明文落
Redis (热缓存 5k) / MySQL (90 天) / admin 页面.

设计:
  - _scrub_secrets(text) -> str: 输入任意 text, 命中 AWS / PEM / GitHub PAT /
    Anthropic key / OpenAI sk- 模式时整段 mask 成 [REDACTED:aws-key] / 类似。
  - redact_audit_entry(entry: dict) -> dict: 把 text_preview / l2 result 等
    字段走过 _scrub_secrets。**就地修改**。调用方 log_entry() 用。
  - 模式与 policy.yaml block_secrets / pii_critical_block 同步, 避免两套
    regex 漂移。

不在本文件覆盖 (诚实声明):
  - 邮箱 / 手机 / 身份证: PII 走 inspection.py weighted_pii_score, 命中
    后降本地, 不需要 redact; audit 留 PII 原文方便事后追责, BYOK 模式下
    客户端自带 key, 责任在客户端。
  - 图片 base64: 已 _chat_part_ref / _openai_content_to_parts 拦截,
    不会进 text_preview。
"""
from __future__ import annotations
import re
from typing import Any, Dict

# 与 policy.yaml block_secrets / pii_critical_block 对齐 (P2 fail-fast 时同步)
_PATTERNS = [
    # AWS access key: AKIA[0-9A-Z]{16} (官方固定 20 字符)
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:aws-access-key]"),
    # AWS secret: 40 字符 base64, 跟在 "aws_secret" / "secret_key" 后面
    (re.compile(r"(?i)(aws[_\-]?secret[_\-]?(?:access[_\-]?)?key[^\n]{0,5}[:=][^\n]{0,5})[A-Za-z0-9/+=]{40}"),
     r"\1[REDACTED:aws-secret-key]"),
    # PEM private keys (任何变体)
    (re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
     "[REDACTED:private-key]"),
    # GitHub personal access token (ghp_/gho_/ghu_/ghr_/ghs_)
    (re.compile(r"gh[psoru]_[A-Za-z0-9]{36,}"), "[REDACTED:github-pat]"),
    # Anthropic API key
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"), "[REDACTED:anthropic-key]"),
    # OpenAI / DeepSeek sk- (legacy + new; DeepSeek = sk- + 32 hex)
    (re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"), "[REDACTED:openai-key]"),
    # Google API key
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "[REDACTED:google-api-key]"),
    # Slack token
    (re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}"), "[REDACTED:slack-token]"),
    # JWT (3 段 base64url, 用 . 分隔)
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
     "[REDACTED:jwt]"),
    # Bearer token header value
    (re.compile(r"(?i)(authorization:\s*Bearer\s+)[A-Za-z0-9_\-.=]{20,}"),
     r"\1[REDACTED:bearer]"),
]


def _scrub_secrets(text: str) -> str:
    """Replace known secret patterns with placeholders. Conservative — only known shapes."""
    if not text or not isinstance(text, str):
        return text if isinstance(text, str) else ""
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def redact_audit_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """P2: 就地 redact audit entry 里的字符串字段。返回原 entry 便于链式调用。

    影响范围 (穷举):
      - text_preview: 100% 走, 这是 P0 时 PII 命中后明文落 audit 的洞
      - l2.result / l2.explanation: L2 小模型有时会回显命中片段
      - rule / action / type / provider / model: 短, 不脱敏 (脱敏反而看不出)
      - session_id / client_ip / id: 短 ID, 不脱敏
    """
    if not isinstance(entry, dict):
        return entry
    for key in ("text_preview", "l2_result", "l2_explanation", "explanation", "reason", "note"):
        v = entry.get(key)
        if isinstance(v, str) and v:
            entry[key] = _scrub_secrets(v)
    if isinstance(entry.get("l2"), dict):
        # P1-d: raw = L2 小模型原始输出（docstring 已声明「有时会回显命中片段」），
        # reason / error 同属模型或上游返回文本。此前只脱敏 label/explanation/snippet，
        # 而 P1 把 l2_result 接进主流程高频审计前，必须先扩到这里。
        for k in ("label", "explanation", "snippet", "raw", "reason", "error"):
            v = entry["l2"].get(k)
            if isinstance(v, str) and v:
                entry["l2"][k] = _scrub_secrets(v)
        # P2：probe 是逐 scope 的结果列表，与 l2 同源（都可能带模型回显），
        # 只要它进了审计就同口径脱敏 —— 新增字段不能顺手开出新的泄漏面。
        _probe = entry["l2"].get("probe")
        if isinstance(_probe, list):
            for _item in _probe:
                if not isinstance(_item, dict):
                    continue
                for k in ("label", "reason", "snippet", "raw"):
                    v = _item.get(k)
                    if isinstance(v, str) and v:
                        _item[k] = _scrub_secrets(v)
    return entry
