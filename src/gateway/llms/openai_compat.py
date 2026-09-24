"""
OpenAI 兼容线协议 handler（api_mode = "openai"）
src/gateway/llms/openai_compat.py

覆盖 OpenAI 及一切 OpenAI 兼容上游（vLLM / Ollama / DeepSeek / OpenRouter /
用户自定义模型等）——请求响应直通，无需变换。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .base import BaseLLM


class OpenAICompatLLM(BaseLLM):
    mode = "openai"
    def get_complete_url(self, base_url: str, kind: str = "chat", custom_path: str = "",
                         *, model: str = "", stream: bool = False) -> str:
        if not base_url:
            return ""
        base = base_url.rstrip("/")
        if kind == "embedding":
            path = "/embeddings"
        elif kind == "proxy":
            path = "/" + custom_path.lstrip("/")
        else:
            path = "/chat/completions"
        if base.endswith("/v1"):
            return base + path
        return f"{base}/v1{path}"

    def get_headers(self, api_key: str, extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h: Dict[str, str] = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        h["content-type"] = "application/json"
        h.update(extra_headers or {})
        return h

    def transform_request(self, body: Dict[str, Any], model: str, *, stream: bool = False) -> Dict[str, Any]:
        if stream:
            body = {**body, "stream": True}
        return body

    def transform_response(self, data: Dict[str, Any], model: str) -> Dict[str, Any]:
        return data

    @property
    def stream_passthrough(self) -> bool:
        return True
