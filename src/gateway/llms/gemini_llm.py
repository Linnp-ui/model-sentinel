"""Google Gemini generateContent 线协议 handler（api_mode = "gemini"）"""
from __future__ import annotations
import json
from typing import Any, Dict, Optional

from .base import BaseLLM


class GeminiLLM(BaseLLM):
    mode = "gemini"

    def get_complete_url(self, base_url, kind="chat", custom_path="", *, model="", stream=False):
        base = (base_url or "").rstrip("/")
        if not base:
            return ""
        method = "streamGenerateContent" if stream else "generateContent"
        if kind == "proxy":
            path = "/" + custom_path.lstrip("/")
            if base.endswith("/v1") or base.endswith("/v1beta"):
                return base + path
            return f"{base}/v1beta{path}"
        if not model:
            model = "gemini-1.5-flash"
        return f"{base}/models/{model}:{method}"

    def get_headers(self, api_key, extra_headers=None):
        h = {"content-type": "application/json"}
        if api_key:
            h["x-goog-api-key"] = api_key
        h.update(extra_headers or {})
        return h

    def _to_gemini(self, body, model, stream=False):
        contents = []
        for m in body.get("messages", []) or []:
            role = m.get("role", "user")
            text = m.get("content", "")
            if not isinstance(text, str):
                text = json.dumps(text, ensure_ascii=False)
            grole = "model" if role in ("assistant", "system") else "user"
            contents.append({"role": grole, "parts": [{"text": text}]})
        out = {"contents": contents}
        gen = {}
        if body.get("max_tokens") is not None:
            gen["maxOutputTokens"] = body["max_tokens"]
        if body.get("temperature") is not None:
            gen["temperature"] = body["temperature"]
        if gen:
            out["generationConfig"] = gen
        return out

    def transform_request(self, body, model, *, stream=False):
        return self._to_gemini(body, model, stream=stream)

    def transform_response(self, data, model):
        text = ""
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            text = ""
        return {
            "id": f"gemini-{model}",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    @property
    def stream_passthrough(self):
        return False

    def transform_stream_line(self, line, model):
        line = (line or "").strip()
        if not line.startswith("data:"):
            return None
        try:
            data = json.loads(line[5:].strip())
        except Exception:
            return None
        text = ""
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            return None
        if text == "":
            return None
        chunk = {
            "id": f"gemini-{model}",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        }
        return ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8")
