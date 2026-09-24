"""
Anthropic Messages 线协议 handler（api_mode = "anthropic"）
src/gateway/llms/anthropic_llm.py

网关标准体（OpenAI chat）<-> Anthropic Messages 双向变换，以及
Anthropic SSE 事件流 -> OpenAI chunk 流的逐行转换。
转换逻辑自原 routing.py 迁移，行为保持一致。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from .base import BaseLLM


class AnthropicLLM(BaseLLM):
    mode = "anthropic"

    # ---------------------------------------------------------------- URL
    def get_complete_url(self, base_url: str, kind: str = "chat", custom_path: str = "",
                         *, model: str = "", stream: bool = False) -> str:
        # Anthropic 全部走 /v1/messages（保持与原 Provider.endpoint 行为一致）
        base = base_url.rstrip("/")
        suffix = "" if base.endswith("/messages") else "/messages"
        return base + suffix

    # ---------------------------------------------------------------- 请求头
    def get_headers(self, api_key: str, extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        h["content-type"] = "application/json"
        h.update(extra_headers or {})
        return h

    # ---------------------------------------------------------------- 请求/响应变换
    def transform_request(self, body: Dict[str, Any], model: str, *, stream: bool = False) -> Dict[str, Any]:
        out = _to_anthropic(body, model)
        if stream:
            out["stream"] = True
        return out

    def transform_response(self, data: Dict[str, Any], model: str) -> Dict[str, Any]:
        return _from_anthropic(data, model)

    # ---------------------------------------------------------------- 流式

    @property
    def stream_passthrough(self) -> bool:
        return False

    def transform_stream_line(self, line: str, model: str) -> Optional[bytes]:
        """Anthropic SSE 行 -> OpenAI chat.completion.chunk SSE 字节块"""
        if not line.startswith("data:"):
            return None
        try:
            evt = json.loads(line[5:].strip())
        except Exception:
            return None
        et = evt.get("type")
        if et == "content_block_delta":
            chunk = {
                "id": "chatcmpl-anthropic", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": model,
                "choices": [{"index": 0,
                             "delta": {"content": evt.get("delta", {}).get("text", "")},
                             "finish_reason": None}],
            }
            return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
        if et == "message_stop":
            return b"data: [DONE]\n\n"
        return None


# ---------------------------------------------------------------- 转换函数

def _to_anthropic(body: Dict[str, Any], model: str) -> Dict[str, Any]:
    """网关标准体（OpenAI chat）-> Anthropic Messages 请求"""
    system = "\n".join(
        m.get("content", "") for m in body.get("messages", []) if m.get("role") == "system"
    )
    messages = [m for m in body.get("messages", []) if m.get("role") != "system"]
    out: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": body.get("max_tokens", 300),
    }
    if system:
        out["system"] = system
    if "temperature" in body:
        out["temperature"] = body["temperature"]
    return out


def _from_anthropic(data: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Anthropic Messages 响应 -> 网关标准体（OpenAI chat）"""
    text = "".join(b.get("text", "") for b in data.get("content", []) if isinstance(b, dict))
    usage = data.get("usage") or {}
    p_tok, c_tok = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    return {
        "id": data.get("id", ""),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": data.get("stop_reason", "stop")}],
        "usage": {"prompt_tokens": p_tok, "completion_tokens": c_tok, "total_tokens": p_tok + c_tok},
    }
