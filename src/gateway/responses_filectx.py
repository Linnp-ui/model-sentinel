"""Responses \u9644\u4ef6\u4e0a\u4e0b\u6587\u63d0\u53d6\uff08\u65b0\u589e\uff0c\u65e0\u5386\u53f2\u51b2\u7a81\uff09

Codex \u8d70 /v1/responses \u4e0a\u4f20\u6587\u4ef6\u65f6\uff0c\u6587\u4ef6\u4ee5 input_file \u5757\u643a\u5e26
`filename` (+ \u53ef\u9009 `file_data` data-URL) \u5230\u8fbe\u3002main._review_text \u7ed9
decide() \u7684 ctx \u91cc file \u5b57\u6bb5\u6052\u4e3a\u7a7a\uff0c\u5bfc\u81f4\u4f9d\u8d56 file.*
\u7684\u89c4\u5219\uff08financial_local_only / drawing_local_only\uff09\u5728 Codex \u8def\u5f84\u4e0b
\u6c38\u8fdc\u6253\u4e0d\u4e2d\u3002\u672c\u6a21\u5757\u53ea\u505a\u7eaf\u63d0\u53d6\uff08\u65e0副\u4f5c\u7528\uff09\uff0c
\u63a5\u7ebf\u7531 wb \u5728 main.py \u843d\u4e24\u4e2a\u53f7\u53e3\uff08\u89c1\u672c\u6587\u4ef6\u5c3e\u90e8 PATCH \u8bf4\u660e\uff09\u3002
"""
from __future__ import annotations

import base64
import csv
import io
from typing import Any

_CSV_EXTS = (".csv", ".txt", ".tsv")
_MAX_HDR_COLS = 20
_MAX_CELL = 200


def _decode_data_url(url: str) -> bytes | None:
    try:
        _, _, data = url.partition(",")
        return base64.b64decode(data.strip())
    except Exception:
        return None


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
                raw = _decode_data_url(fd)
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
    "financial_local_only", "drawing_local_only",
    "ocr_empty_image_route_local", "AB\u6587\u4ef6",
})


def responses_should_mark(rule_name: str | None, action: str | None,
                           file_ctx: dict | None) -> bool:
    """responses \u547d\u4e2d\u540e\u662f\u5426\u6c61\u67d3 session+client\uff08\u7eaf\u51fd\u6570\uff0c\u53ef\u5355\u6d4b\uff09\u3002

    \u53ea\u6709\u9644\u4ef6/\u5185\u8054\u6587\u4ef6\u53c2\u4e0e\u65f6\u624d\u6807\u8bb0\uff1a\u7eaf\u6587\u672c\u89c4\u5219\u547d\u4e2d
    \uff08\u5982\u7eaf\u7c98\u8d34\u8eab\u4efd\u8bc1\uff09\u4fdd\u6301\u73b0\u72b6\u4e0d\u6807\u8bb0\u3002
    """
    if action not in ("route_local", "block"):
        return False
    if (file_ctx or {}).get("filename"):
        return True
    return rule_name in _FILE_MARK_RULES


# ---------------------------------------------------------------------------
# PATCH \u8bf4\u660e\uff08wb \u5728 main.py / file_inspector.py \u843d\uff0c\u4e0d\u52a8\u5176\u4ed6\u903b\u8f91\uff09
#
# Hunk 1 -- main._review_text \u589e\u53ef\u9009\u53c2\u6570\uff08\u517c\u5bb9 chat/messages \u8001\u8c03\u7528\uff09\u003a
#   async def _review_text(text, request=None, extra_findings=None,
#                          l2_text=None, l2_chunks=None, file_ctx=None):
#       ...
#       _file = {"ext": "", "filename": "", "headers": [], "sheet_names": [], "text": ""}
#       if file_ctx:
#           for _k in ("ext", "filename"):
#               if file_ctx.get(_k):
#                   _file[_k] = file_ctx[_k]
#           for _k in ("headers", "sheet_names"):
#               if file_ctx.get(_k):
#                   _file[_k] = list(file_ctx[_k])
#       ctx = {"text": text, "file": _file, "findings": text_findings,
#              "session": {"confidential": session_conf}}
#
# Hunk 2 -- main.responses_api\uff0cinline \u626b\u63cf\u540e\u63a5\u7ebf\u003a
#   from .responses_filectx import extract_responses_filectx
#   ...
#   file_ctx = extract_responses_filectx(body)
#   decision, text_findings, l2_result = await _review_text(
#       text, request, {"ocr_empty_and_image": True} if inline_bad else None,
#       l2_chunks=l2_blocks, file_ctx=file_ctx)
#
# \u9a8c\u6536\uff1atest_responses_path_gap_financial_needs_file_ctx \u6539\u4e3a\u4f20 file_ctx
# \u5373\u7eff\uff1bpolicy_rules \u6a21\u5757 11 \u4e2a\u5168\u7eff\u540e\u8ddf\u7740\u90e8\u7f72\u9a8c\u6536\u3002
# ---------------------------------------------------------------------------
