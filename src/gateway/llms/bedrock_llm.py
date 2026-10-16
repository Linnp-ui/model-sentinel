"""Amazon Bedrock Converse 线协议 handler（api_mode = "bedrock"）

ponytail: auth via api_key_env storing 'accessKey:secretKey:region' or extra_headers supplied;
Converse transform minimal; SigV4 signing not implemented, relies on gateway proxy/headers.
"""
from __future__ import annotations
import json
from typing import Any, Dict, Optional

from .base import BaseLLM


class BedrockLLM(BaseLLM):
    mode = "bedrock"

    def get_complete_url(self, base_url, kind="chat", custom_path="", *, model="", stream=False):
        base = (base_url or "").rstrip("/")
        if not base:
            return ""
        if kind == "proxy":
            path = "/" + custom_path.lstrip("/")
            return base + path
        model = model or "anthropic.claude-3-sonnet"
        suffix = "/converse-stream" if stream else "/converse"
        if "bedrock-runtime" in base or "amazonaws.com" in base:
            return f"{base}/model/{model}{suffix}"
        # 允许 base_url 直接为完整 InvokeModel/Converse 端点
        return f"{base}/model/{model}{suffix}"

    def get_headers(self, api_key, extra_headers=None):
        h = {"content-type": "application/json", "accept": "application/json"}
        h.update(extra_headers or {})
        return h

    def _to_bedrock(self, body, model):
        msgs = []
        for m in body.get("messages", []) or []:
            role = "assistant" if m.get("role") == "assistant" else "user"
            content = m.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            msgs.append({"role": role, "content": [{"text": content}]})
        out = {"messages": msgs}
        if body.get("max_tokens") is not None:
            out.setdefault("inferenceConfig", {})["maxTokens"] = body["max_tokens"]
        if body.get("temperature") is not None:
            out.setdefault("inferenceConfig", {})["temperature"] = body["temperature"]
        return out

    def transform_request(self, body, model, *, stream=False):
        return self._to_bedrock(body, model)

    def transform_response(self, data, model):
        text = ""
        try:
            for c in data.get("output", {}).get("message", {}).get("content", []):
                if "text" in c:
                    text += c["text"]
        except Exception:
            text = ""
        usage = data.get("usage", {})
        return {
            "id": f"bedrock-{model}",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": data.get("stopReason", "stop")}],
            "usage": {"prompt_tokens": usage.get("inputTokens", 0), "completion_tokens": usage.get("outputTokens", 0), "total_tokens": usage.get("totalTokens", 0)},
        }

    @property
    def stream_passthrough(self):
        return False

    def transform_stream_line(self, line, model):
        line = (line or "").strip()
        if not line:
            return None
        try:
            data = json.loads(line)
        except Exception:
            if line.startswith("data:"):
                try:
                    data = json.loads(line[5:].strip())
                except Exception:
                    return None
            else:
                return None
        text = ""
        try:
            for c in data.get("contentBlockDelta", {}).get("delta", {}).get("text", "") if False else []:
                text += c
        except Exception:
            pass
        # Bedrock event stream JSON may contain delta text directly
        if not text:
            try:
                text = data.get("delta", {}).get("text", "") or data.get("text", "")
            except Exception:
                text = ""
        if not text:
            return None
        chunk = {
            "id": f"bedrock-{model}",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        }
        return ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode("utf-8")
