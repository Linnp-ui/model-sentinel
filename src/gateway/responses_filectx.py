"""Responses \u9644\u4ef6\u4e0a\u4e0b\u6587\u63d0\u53d6\uff08\u65b0\u589e\uff0c\u65e0\u5386\u53f2\u51b2\u7a81\uff09

Codex \u8d70 /v1/responses \u4e0a\u4f20\u6587\u4ef6\u65f6\uff0c\u6587\u4ef6\u4ee5 input_file \u5757\u643a\u5e26
`filename` (+ \u53ef\u9009 `file_data` data-URL) \u5230\u8fbe\u3002main._review_text \u7ed9
decide() \u7684 ctx \u91cc file \u5b57\u6bb5\u6052\u4e3a\u7a7a\uff0c\u5bfc\u81f4\u4f9d\u8d56 file.*
\u7684\u89c4\u5219\uff08\u8d22\u52a1\u8868\u8f6c\u5185\u7f51 / \u56fe\u7eb8\u8f6c\u5185\u7f51\uff09\u5728 Codex \u8def\u5f84\u4e0b
\u6c38\u8fdc\u6253\u4e0d\u4e2d\u3002\u672c\u6a21\u5757\u53ea\u505a\u7eaf\u63d0\u53d6\uff08\u65e0副\u4f5c\u7528\uff09\uff0c
\u63a5\u7ebf\u7531 wb \u5728 main.py \u843d\u4e24\u4e2a\u53f7\u53e3\uff08\u89c1\u672c\u6587\u4ef6\u5c3e\u90e8 PATCH \u8bf4\u660e\uff09\u3002
"""
from __future__ import annotations

import csv
import io
from typing import Any

from .file_inspector import decode_inline_data_url

_CSV_EXTS = (".csv", ".txt", ".tsv")
_MAX_HDR_COLS = 20
_MAX_CELL = 200


def _csv_headers(raw: bytes) -> list:
    try:
        text = raw.decode("utf-8-sig", errors="replace")
    except Exception:
        return []
    for line in text.splitlines():
        if line.strip():
            try:
                row = next(csv.reader(io.StringIO(line)))
            except Exception:
                return []
            return [c.strip()[:_MAX_CELL] for c in row[:_MAX_HDR_COLS] if c.strip()]
    return []


def _iter_blocks(body: Any):
    if not isinstance(body, dict):
        return
    inp = body.get("input")
    if not isinstance(inp, list):
        return
    for item in inp:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict):
                    yield b
        else:
            yield item


def _mentioned_filenames(text: str) -> list:
    """Codex prompt \u91cc\u9644\u4ef6\u540d\u7684\u63d0\u53d6\u3002"""
    out: list = []
    in_section = False
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if "Files mentioned by the user" in line:
            in_section = True
            continue
        if not in_section:
            continue
        if not line:
            continue
        if not line.startswith("##"):
            in_section = False
            continue
        name = line[2:].strip()
        if ":" in name:
            name = name.split(":", 1)[0].strip()
        name = name.strip(",;")
        if name and "." in name and name not in out:
            out.append(name)
    return out


def _suffix_ext(name: str) -> str:
    low = name.lower()
    for e in (".csv", ".txt", ".tsv", ".xlsx", ".xls", ".pdf"):
        if low.endswith(e):
            return e.lstrip(".")
    return ""


def extract_responses_filectx(body: Any, text: str = "") -> dict:
    """\u4ece responses body \u63d0\u53d6 {filename, headers, sheet_names, ext} \u5168\u7a7a\u5b57\u5178\u3002"""
    filenames: list = []
    headers: list = []
    ext = ""
    for b in _iter_blocks(body):
        if b.get("type") not in ("input_file", "file"):
            continue
        name = (b.get("filename") or "").strip()
        if name:
            filenames.append(name)
            if not ext:
                low = name.lower()
                for e in (".csv", ".txt", ".tsv", ".xlsx", ".xls", ".pdf"):
                    if low.endswith(e):
                        ext = e.lstrip(".")
                        break
            fd = b.get("file_data")
            if isinstance(fd, str) and fd.startswith("data:") and low.endswith(_CSV_EXTS):
                raw = decode_inline_data_url(fd)
                if raw and len(raw) <= 1_000_000:
                    for h in _csv_headers(raw):
                        if h not in headers:
                            headers.append(h)
    for name in _mentioned_filenames(text):
        if name not in filenames:
            filenames.append(name)
            if not ext:
                ext = _suffix_ext(name)
    return {"filename": " ".join(filenames), "headers": headers,
            "sheet_names": [], "ext": ext}


_FILE_MARK_RULES = frozenset({
    "财务表转内网", "图纸转内网",
    "空图转内网", "AB文件",
})


# 可用性兜底规则：转本地不是因为内容判密，而是因为系统不可用/超预算。
# 这类命中**不标会话机密**——否则 L2 抖一次（或长文超预算），整个对话被锁本地 7 天。
NON_CONFIDENTIAL_RULES = frozenset({
    "l2_unavailable", "review_text_overflow",
    "first_chunk_fallback", "prompt_too_long_fallback",
})


def responses_should_mark(rule_name: str | None, action: str | None,
                           file_ctx: dict | None) -> bool:
    """responses 命中后是否污染 session+client（纯函数，可单测）。

    2026-10-26 起与 chat/messages 同口径：机密判定（含纯文本粘贴 / L2 判密）都标记
    ——第一轮判密 ⇒ 同会话后续轮粘本地。只排除可用性兜底（不是内容判定）。
    `file_ctx` 参数保留兼容旧调用方，不再参与判断。
    """
    if action not in ("route_local", "block"):
        return False
    return rule_name not in NON_CONFIDENTIAL_RULES

