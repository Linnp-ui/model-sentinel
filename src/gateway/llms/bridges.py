"""
协议桥（Protocol Bridges）— 入站协议 <-> 网关标准体（OpenAI chat）的双向转换
src/gateway/llms/bridges.py

自原 routing.py 迁移，逻辑不变：
  - Anthropic Messages /v1/messages：ccswitch / Claude Code 入站
  - OpenAI Responses  /v1/responses：Codex CLI / ccswitch wire_api="responses" 入站

与 BaseLLM 的分工：BaseLLM 解决"上游线协议差异"，本模块解决"入站协议差异"；
审查与路由在 main.py 中对三种入站协议复用同一套逻辑。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List

# 缺省 max_tokens 与 routing._DEFAULT_MAX_TOKENS 同源（env 可覆盖）；
# 1 024 时代的长回答截断已由 T27/P0-7 修正，这里不能再钉小值。
_DEFAULT_MAX_TOKENS = int(os.getenv("AI_GATEWAY_DEFAULT_MAX_TOKENS", "32768"))


def sse(obj: Dict[str, Any], event: str | None = None) -> bytes:
    prefix = f"event: {event}\n".encode() if event else b""
    return prefix + f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


# ================================================================
# Anthropic Messages 入站
# ================================================================

def _clip(s: Any, n: int) -> str:
    """送审截断：防超大字段(base64/长文档)拖慢 L1/L2。"""
    if s is None:
        return ""
    return str(s)[:n]


def _clip_url(url: Any) -> str:
    """URL 送审：http(s) 全收(限 2000)；data: 只留前 200 字符作存在标记，不解码。"""
    u = str(url or "")
    return u[:200] if u.startswith("data:") else u[:2000]


def _anthropic_content_ref(c: Any) -> str:
    if c is None:
        return ""
    if isinstance(c, str):
        return _clip(c, 4000)
    if isinstance(c, list):
        return " ".join(
            _anthropic_block_ref(x) if isinstance(x, dict) else _clip(x, 4000) for x in c)
    return ""


def _anthropic_block_ref(b: Dict[str, Any]) -> str:
    """非 text block 的可审查文本(P1)：image/document 来源、tool 输入输出、文件引用。

    未知 block 兜底收 filename/file_id/url，防新供应商字段绕过审查。
    """
    t = b.get("type", "")
    if t in ("text", "input_text"):
        return _clip(b.get("text", ""), 4000)
    if t in ("image", "document", "input_image"):
        src = b.get("source")
        if isinstance(src, dict):
            st = src.get("type", "")
            if st == "url":
                return _clip_url(src.get("url", ""))
            if st == "text":
                return _clip(src.get("data", ""), 4000)
            if st == "file":
                return _clip(src.get("file_id", ""), 500)
            if st == "base64":
                return _clip(str(src.get("data", ""))[:200], 200)
        for k in ("url", "image_url"):
            if isinstance(b.get(k), str) and b[k]:
                return _clip_url(b[k])
        return ""
    if t == "tool_use":
        parts = []
        if b.get("name"):
            parts.append(_clip(b["name"], 200))
        try:
            parts.append(_clip(json.dumps(b.get("input", ""), ensure_ascii=False), 4000))
        except Exception:
            parts.append(_clip(b.get("input", ""), 4000))
        return " ".join(x for x in parts if x)
    if t in ("tool_result", "tool_search_tool_result", "web_search_tool_result"):
        return _anthropic_content_ref(b.get("content"))
    if t == "container_upload":
        return _clip(b.get("file_id", ""), 500)
    for k in ("filename", "file_id", "url", "image_url"):
        if isinstance(b.get(k), str) and b[k]:
            return _clip_url(b[k]) if "url" in k else _clip(b[k], 500)
    return ""


def _responses_block_ref(b: Dict[str, Any]) -> str:
    """Responses 内容块的可审查文本(P1)：input_image/file、function_call/output。"""
    t = b.get("type", "")
    if t in ("input_text", "text", "output_text"):
        return _clip(b.get("text", ""), 4000)
    if t in ("input_image", "image", "image_url"):
        v = b.get("image_url", "")
        return _clip_url(v if isinstance(v, str) else "")
    if t in ("input_file", "file"):
        return " ".join(_clip(b.get(k, ""), 500) for k in ("filename", "file_id")).strip()
    if t == "function_call":
        parts = []
        if b.get("name"):
            parts.append(_clip(b["name"], 200))
        if b.get("arguments"):
            parts.append(_clip(b["arguments"], 4000))
        return " ".join(x for x in parts if x)
    if t in ("function_call_output", "output"):
        return _clip(b.get("output", b.get("text", "")), 4000)
    for k in ("filename", "file_id"):
        if isinstance(b.get(k), str) and b[k]:
            return _clip(b[k], 500)
    return ""


def _responses_item_ref(item: Dict[str, Any]) -> str:
    c = item.get("content")
    if isinstance(c, str):
        return _clip(c, 4000)
    if isinstance(c, list):
        return " ".join(
            _responses_block_ref(x) if isinstance(x, dict) else _clip(x, 4000) for x in c)
    if isinstance(item.get("text"), str):
        return _clip(item["text"], 4000)
    return _responses_block_ref(item)


def anthropic_text(body: Dict[str, Any]) -> str:
    """从 Anthropic messages 请求体提取待审查文本（system + messages 的文本块）"""
    parts: list[str] = []
    sys = body.get("system")
    if isinstance(sys, str):
        parts.append(sys)
    elif isinstance(sys, list):
        parts += [b.get("text", "") for b in sys if isinstance(b, dict)]
    for m in body.get("messages", []) or []:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts += [_anthropic_block_ref(b) if isinstance(b, dict) else "" for b in c]
    return " ".join(p for p in parts if p)


def anthropic_to_openai(body: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Anthropic messages 请求 -> OpenAI chat 请求（降级 openai 兼容上游用）"""
    msgs: list[dict] = []
    sys = body.get("system")
    if sys:
        text = sys if isinstance(sys, str) else "\n".join(
            b.get("text", "") for b in sys if isinstance(b, dict))
        msgs.append({"role": "system", "content": text})
    for m in body.get("messages", []) or []:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            msgs.append({"role": m.get("role", "user"), "content": c})
        elif isinstance(c, list):
            text = "\n".join(b.get("text", "") for b in c
                             if isinstance(b, dict) and isinstance(b.get("text"), str))
            msgs.append({"role": m.get("role", "user"), "content": text})
    out = {"model": model, "messages": msgs, "max_tokens": int(body.get("max_tokens") or 1024)}
    for k in ("temperature", "top_p"):
        if k in body:
            out[k] = body[k]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    # Reasoning passthrough: Anthropic extended thinking / openai reasoning
    # Anthropic uses body["thinking"]={"type":"enabled","budget_tokens":N}
    # OpenAI compat expects body["reasoning"]={"effort":"medium"} or include_reasoning=True
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") in ("enabled", "adaptive"):
        budget = int(thinking.get("budget_tokens") or 1000)
        effort = "low" if budget < 500 else ("medium" if budget < 2000 else "high")
        out["reasoning"] = {"effort": effort}
        out["include_reasoning"] = True
    elif body.get("include_reasoning") or body.get("reasoning"):
        out["reasoning"] = body.get("reasoning") or {"effort": "medium"}
        out["include_reasoning"] = True
    return out


def openai_to_anthropic(data: Dict[str, Any], model: str) -> Dict[str, Any]:
    """OpenAI chat 响应 -> Anthropic messages 响应"""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    usage = data.get("usage") or {}
    stop_map = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}
    return {
        "id": data.get("id", "msg_gateway"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": msg.get("content") or ""}],
        "stop_reason": stop_map.get(choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0)},
    }


def anthropic_stream_events(text: str, model: str, input_tokens: int = 0, reasoning: str = "") -> List[bytes]:
    """一次性文本 -> Anthropic SSE 事件序列（mock/降级场景用）"""
    events: list[bytes] = [
        sse({"type": "message_start", "message": {
            "id": "msg_gateway", "type": "message", "role": "assistant", "model": model,
            "content": [], "usage": {"input_tokens": input_tokens, "output_tokens": 0}}}),
    ]
    if reasoning:
        # Anthropic thinking 块 (Claude 3.7+) - index 0
        events.append(sse({"type": "content_block_start", "index": 0,
                           "content_block": {"type": "thinking", "thinking": ""}}))
        events.append(sse({"type": "content_block_delta", "index": 0,
                           "delta": {"type": "thinking_delta", "thinking": reasoning}}))
        events.append(sse({"type": "content_block_stop", "index": 0}))
        text_index = 1
    else:
        text_index = 0
    events.append(sse({"type": "content_block_start", "index": text_index,
                       "content_block": {"type": "text", "text": ""}}))
    if text:
        events.append(sse({"type": "content_block_delta", "index": text_index,
                           "delta": {"type": "text_delta", "text": text}}))
    events.append(sse({"type": "content_block_stop", "index": text_index}))
    events.append(sse({"type": "message_delta",
                       "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                       "usage": {"output_tokens": max(1, (len(text) + len(reasoning)) // 3)}}))
    events.append(sse({"type": "message_stop"}))
    return events


def parse_openai_sse_deltas(chunk: bytes) -> list[dict]:
    """从 openai 格式 SSE 字节流里抽出文本与推理内容
    返回 list of {"content": str, "reasoning": str}，按流顺序追加
    """
    out: list[dict] = []
    for line in chunk.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:") or line == b"data: [DONE]":
            continue
        try:
            evt = json.loads(line[5:].strip())
        except Exception:
            continue
        delta = ((evt.get("choices") or [{}])[0].get("delta") or {})
        c = delta.get("content")
        # openrouter uses 'reasoning' field (string); some providers use 'reasoning_content'
        r = delta.get("reasoning") or delta.get("reasoning_content") or ""
        tc = delta.get("tool_calls")
        if isinstance(tc, list) and tc:
            out.append({"content": c or "", "reasoning": r or "", "tool_calls": tc})
        elif (isinstance(c, str) and c) or (isinstance(r, str) and r):
            out.append({"content": c or "", "reasoning": r or ""})
        # usage chunk : choices 通常为空，原实现直接丢掉
        # ⇒ response.completed 的 usage 恒为 0（2026-09-16 补）
        u = evt.get("usage")
        if isinstance(u, dict) and u:
            out.append({"usage": u})
    return out


# ================================================================
# OpenAI Responses 入站
# ================================================================

def responses_text(body: Dict[str, Any]) -> str:
    """从 OpenAI Responses 请求体提取待审查文本（instructions + input）"""
    parts: list[str] = []
    ins = body.get("instructions")
    if isinstance(ins, str):
        parts.append(ins)
    inp = body.get("input")
    if isinstance(inp, str):
        parts.append(inp)
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                if isinstance(item, str):
                    parts.append(item)
                continue
            parts.append(_responses_item_ref(item))
    return " ".join(p for p in parts if p)


# ---- Responses 工具形状（2026-09-16 按 Codex 实测请求体取值） ----

# 宿主工具：由客户端本地执行（Codex 的 web_search / local_shell / computer_use 等）。
# 发给 chat 上游没有意义——上游执行不了，还可能因未知 type 直接 4xx。
_HOST_TOOL_TYPES = frozenset({
    "web_search", "web_search_preview", "local_shell", "shell",
    "computer_use_preview", "computer", "code_interpreter", "file_search",
    "image_generation", "mcp",
})

# 非对话内容 item：回传时必须丢弃。尤其是 reasoning —— 它带
# content:[{type:"reasoning_text",...}]，若走 content-list 分支就会被当成
# 一条 user 消息塞进对话（2026-09-16 实测 004.req.json input[6]，上下文污染）。
_SKIP_INPUT_ITEM_TYPES = frozenset({
    "reasoning", "item_reference",
    "web_search_call", "web_search_call_output",
    "local_shell_call", "local_shell_call_output",
    "computer_call", "computer_call_output",
    "image_generation_call", "file_search_call", "code_interpreter_call",
    "mcp_call", "mcp_list_tools", "mcp_approval_request", "mcp_approval_response",
})

# custom 工具（Codex 的 apply_patch 等自由文本工具）在 chat 协议里没有对应概念，
# 统一降级成「单字段 input」的 function；回程再由 Assembler 还原为 custom_tool_call。
_CUSTOM_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"input": {
        "type": "string",
        "description": "Raw tool input as free-form text (not JSON).",
    }},
    "required": ["input"],
}


def _chat_tools_from_responses(tools: Any) -> list:
    """Responses 工具定义 -> chat.completions 嵌套形状。

    Responses 实际种类（2026-09-16 Codex 实测）：
      - {type:"function", name, description, parameters, strict}
      - {type:"namespace", name, tools:[...]}   命名空间，需**递归展平**为 ns.tool
      - {type:"custom", name, description, format}  自由文本工具（apply_patch）
      - {type:"web_search"|"local_shell"|...}   宿主工具，客户端本地执行 ⇒ 丢弃
    chat: {type:"function", function:{name, description, parameters, strict}}
    """
    out: list = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if isinstance(t.get("function"), dict):  # 已是 chat 形状：原样透传
            out.append(t)
            continue
        ty = str(t.get("type") or "")
        if ty == "namespace":
            # 原实现只看 t["name"] ⇒ 整个命名空间被压成**一个无参数的 function**，
            # 子工具全部丢失（Codex 的 multi_agent / mcp repl 能力直接蒸发）。
            ns = str(t.get("name") or "")
            for sub in _chat_tools_from_responses(t.get("tools")):
                fn = dict(sub.get("function") or {})
                name = str(fn.get("name") or "")
                if not name:
                    continue
                fn["name"] = f"{ns}.{name}" if ns else name
                out.append({"type": "function", "function": fn})
            continue
        if ty in _HOST_TOOL_TYPES:
            continue
        name = str(t.get("name") or "")
        if not name:
            continue
        fn: Dict[str, Any] = {
            "name": name,
            "description": str(t.get("description") or ""),
            "parameters": _CUSTOM_INPUT_SCHEMA if ty == "custom"
            else (t.get("parameters") or {"type": "object", "properties": {}}),
        }
        if t.get("strict") is not None:
            fn["strict"] = t["strict"]
        out.append({"type": "function", "function": fn})
    return out


def _canonical_role(role: Any) -> str:
    """把 Responses/新式 role 归一到上游 chat 接口接受的枚举。

    Codex（wire_api="responses"）会发 role="developer" 的 message item ——
    OpenAI 新式 instructions role，语义等同 system。而外部 chat provider
    （如 deepseek）的 role 枚举是 {system,user,assistant,tool,latest_reminder}，
    原样透传会直接 400：

        deepseek 400: ... messages[0].role: unknown variant `developer` ...

    未知 role 一律兜底为 user，避免再次因枚举不匹配把整轮对话打断。
    """
    r = str(role or "user").strip().lower()
    if r in ("developer", "system"):
        return "system"
    if r in ("user", "assistant", "tool"):
        return r
    return "user"


def responses_to_chat(body: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Responses 请求 -> chat.completions 请求（降级本地模型用）"""
    msgs: list[dict] = []
    ins = body.get("instructions")
    if isinstance(ins, str) and ins:
        msgs.append({"role": "system", "content": ins})
    inp = body.get("input")
    if isinstance(inp, str):
        msgs.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                msgs.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue
            _itype = item.get("type")
            if _itype == "function_call":
                msgs.append({"role": "assistant", "content": None, "tool_calls": [{
                    "id": str(item.get("call_id") or item.get("id") or ""),
                    "type": "function",
                    "function": {"name": str(item.get("name") or ""),
                                 "arguments": str(item.get("arguments") or "{}")}}]})
                continue
            if _itype == "function_call_output":
                _out = item.get("output")
                msgs.append({"role": "tool",
                             "tool_call_id": str(item.get("call_id") or ""),
                             "content": _out if isinstance(_out, str)
                             else json.dumps(_out, ensure_ascii=False)})
                continue
            if _itype == "custom_tool_call":
                # apply_patch 等 custom 工具：请求侧已降级为 input 单字段 function，
                # 回程把 input 包回 {"input": ...} 与之一致，否则多轮上下文对不上。
                msgs.append({"role": "assistant", "content": None, "tool_calls": [{
                    "id": str(item.get("call_id") or item.get("id") or ""),
                    "type": "function",
                    "function": {"name": str(item.get("name") or ""),
                                 "arguments": json.dumps(
                                     {"input": item.get("input") or ""},
                                     ensure_ascii=False)}}]})
                continue
            if _itype == "custom_tool_call_output":
                _cout = item.get("output")
                msgs.append({"role": "tool",
                             "tool_call_id": str(item.get("call_id") or ""),
                             "content": _cout if isinstance(_cout, str)
                             else json.dumps(_cout, ensure_ascii=False)})
                continue
            if _itype in _SKIP_INPUT_ITEM_TYPES:
                # 不是对话内容（reasoning / 宿主 call / item_reference ...）：
                # 不丢就会落到下面的 content-list 分支，把思考文本当 user 消息。
                continue
            c = item.get("content")
            if isinstance(c, str):
                msgs.append({"role": _canonical_role(item.get("role")), "content": c})
            elif isinstance(c, list):
                text = "\n".join(b.get("text", "") for b in c
                                 if isinstance(b, dict) and isinstance(b.get("text"), str))
                msgs.append({"role": _canonical_role(item.get("role")), "content": text})
    # 上游 chat 模板要求 system 唯一且必须在开头（生产实测 vLLM：
    # "System message must be at the beginning."）。instructions 与
    # developer/system 类消息可能各生成一条 system ⇒ 合并成单条前置 system。
    _sys_parts = [str(m["content"]) for m in msgs
                  if m.get("role") == "system" and m.get("content")]
    if _sys_parts:
        msgs = [m for m in msgs if m.get("role") != "system"]
        msgs.insert(0, {"role": "system", "content": "\n\n".join(_sys_parts)})

    out: Dict[str, Any] = {"model": model, "messages": msgs,
                           "max_tokens": int(body.get("max_output_tokens") or _DEFAULT_MAX_TOKENS)}
    tools = _chat_tools_from_responses(body.get("tools"))
    if tools:
        out["tools"] = tools
        if body.get("tool_choice") is not None:
            out["tool_choice"] = body["tool_choice"]
        if body.get("parallel_tool_calls") is not None:
            out["parallel_tool_calls"] = body["parallel_tool_calls"]
    return out


def chat_to_responses(data: Dict[str, Any], model: str) -> Dict[str, Any]:
    """chat.completions 响应 -> Responses 响应"""
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    usage = data.get("usage") or {}
    rid = data.get("id") or "resp_gateway"
    return {
        "id": rid, "object": "response", "created_at": int(time.time()),
        "status": "completed", "model": model,
        "output": [{"type": "message", "id": rid + "_m", "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}],
        "output_text": text,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0),
                  "total_tokens": usage.get("total_tokens", 0)},
    }


def responses_stream_events(text: str, model: str, resp: Dict[str, Any]) -> List[bytes]:
    """完整文本 -> 最小 Responses SSE 事件序列（mock/降级场景用）"""
    asm = ResponsesSSEAssembler(model, resp_id=resp.get("id"), created_at=resp.get("created_at"))
    out = [asm.created()]
    if text:
        out.extend(asm.text_delta(text))
    out.extend(asm.finish(usage=resp.get("usage")))
    return out


class ResponsesSSEAssembler:
    """chat.completions 增量 -> 协议完整的 OpenAI Responses SSE 事件序列（兼容 codex-rs）。

    - 文本：created -> output_item.added -> content_part.added -> output_text.delta
      (item_id/output_index/content_index) -> output_text.done -> content_part.done ->
      output_item.done（codex 只从 output_item.done 提交 assistant 消息）
    - 思考链：reasoning item（delta 必须带 content_index，否则 codex 静默丢弃）
    - 工具调用：function_call item（{type,name,arguments,call_id}）——codex 只在
      output_item.done 处真正派发工具调用，缺了就是「工具调用中断」
    全事件带递增 sequence_number。
    """

    def __init__(self, model: str, resp_id: str | None = None, created_at: int | None = None):
        self.model = model
        self.resp_id = resp_id or "resp_gateway"
        self.created_at = int(time.time()) if created_at is None else int(created_at)
        self._seq = 0
        self._out_idx = 0
        self._reasoning_open = False
        self._reasoning_done = False
        self._reasoning_idx = 0
        self._message_open = False
        self._msg_idx = 0
        self._rs_id = self.resp_id + "_rs"
        self._m_id = self.resp_id + "_m"
        self._reasoning_parts: list[str] = []
        self._text_parts: list[str] = []
        self._tool_items: Dict[int, Dict[str, Any]] = {}

    def _next_idx(self) -> int:
        i = self._out_idx
        self._out_idx += 1
        return i

    def _ev(self, obj: Dict[str, Any]) -> bytes:
        ev = dict(obj)
        ev["sequence_number"] = self._seq
        self._seq += 1
        return sse(ev)

    def created(self) -> bytes:
        return self._ev({"type": "response.created", "response": {
            "id": self.resp_id, "object": "response", "created_at": self.created_at,
            "model": self.model, "status": "in_progress", "output": []}})

    # ---------- reasoning ----------
    def _open_reasoning(self) -> List[bytes]:
        if self._reasoning_open:
            return []
        self._reasoning_open = True
        self._reasoning_idx = self._next_idx()
        return [self._ev({"type": "response.output_item.added", "output_index": self._reasoning_idx,
                          "item": {"type": "reasoning", "id": self._rs_id,
                                   "summary": [], "content": []}})]

    def _reasoning_item(self) -> Dict[str, Any]:
        return {"type": "reasoning", "id": self._rs_id, "summary": [],
                "content": [{"type": "reasoning_text", "text": "".join(self._reasoning_parts)}]}

    def reasoning_delta(self, text: str) -> List[bytes]:
        if not text:
            return []
        out = self._open_reasoning()
        self._reasoning_parts.append(text)
        out.append(self._ev({"type": "response.reasoning_text.delta", "item_id": self._rs_id,
                             "output_index": self._reasoning_idx, "content_index": 0, "delta": text}))
        return out

    def _close_reasoning(self) -> List[bytes]:
        if not self._reasoning_open or self._reasoning_done:
            return []
        self._reasoning_done = True
        return [self._ev({"type": "response.output_item.done", "output_index": self._reasoning_idx,
                          "item": self._reasoning_item()})]

    # ---------- assistant message ----------
    def _open_message(self) -> List[bytes]:
        if self._message_open:
            return []
        out = self._close_reasoning()
        self._message_open = True
        self._msg_idx = self._next_idx()
        out.append(self._ev({"type": "response.output_item.added", "output_index": self._msg_idx,
                             "item": {"type": "message", "id": self._m_id, "role": "assistant",
                                      "status": "in_progress", "content": []}}))
        out.append(self._ev({"type": "response.content_part.added", "item_id": self._m_id,
                             "output_index": self._msg_idx, "content_index": 0,
                             "part": {"type": "output_text", "text": ""}}))
        return out

    def text_delta(self, text: str) -> List[bytes]:
        if not text:
            return []
        out = self._open_message()
        self._text_parts.append(text)
        out.append(self._ev({"type": "response.output_text.delta", "item_id": self._m_id,
                             "output_index": self._msg_idx, "content_index": 0, "delta": text}))
        return out

    def _message_item(self, full: str) -> Dict[str, Any]:
        return {"type": "message", "id": self._m_id, "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": full, "annotations": []}]}

    # ---------- function call ----------
    def _function_call_item(self, st: Dict[str, Any], status: str = "completed") -> Dict[str, Any]:
        item = {"type": "function_call", "name": st["name"],
                "arguments": st["args"], "call_id": st["call_id"]}
        if status != "completed":
            item["status"] = status
        return item

    def tool_call_delta(self, tool_calls: list) -> List[bytes]:
        """openai 增量 [{index,id,function:{name,arguments}}] -> function_call item 事件"""
        out: List[bytes] = []
        for tc in tool_calls or []:
            if not isinstance(tc, dict):
                continue
            try:
                idx = int(tc.get("index") or 0)
            except (TypeError, ValueError):
                idx = 0
            st = self._tool_items.get(idx)
            fn = tc.get("function") or {}
            if st is None:
                st = {"call_id": str(tc.get("id") or tc.get("call_id") or ("call_%d" % idx)),
                      "name": "", "args": "", "item_id": self.resp_id + "_fc%d" % idx, "idx": None}
                self._tool_items[idx] = st
            if isinstance(fn, dict) and fn.get("name"):
                st["name"] += str(fn["name"])
            if st["idx"] is None:
                out += self._close_reasoning()
                st["idx"] = self._next_idx()
                out.append(self._ev({"type": "response.output_item.added", "output_index": st["idx"],
                                     "item": self._function_call_item(st, "in_progress")}))
            args = fn.get("arguments") if isinstance(fn, dict) else None
            if args:
                st["args"] += str(args)
                out.append(self._ev({"type": "response.function_call_arguments.delta",
                                     "item_id": st["item_id"], "output_index": st["idx"],
                                     "delta": str(args)}))
        return out

    # ---------- 收尾 ----------
    def finish(self, usage: Optional[Dict[str, Any]] = None) -> List[bytes]:
        out: List[bytes] = []
        full = "".join(self._text_parts)
        if self._text_parts or not self._tool_items:
            out += self._open_message()
            out.append(self._ev({"type": "response.output_text.done", "item_id": self._m_id,
                                 "output_index": self._msg_idx, "content_index": 0, "text": full}))
            out.append(self._ev({"type": "response.content_part.done", "item_id": self._m_id,
                                 "output_index": self._msg_idx, "content_index": 0,
                                 "part": {"type": "output_text", "text": full}}))
            out.append(self._ev({"type": "response.output_item.done", "output_index": self._msg_idx,
                                 "item": self._message_item(full)}))
        else:
            out += self._close_reasoning()

        output: List[Dict[str, Any]] = []
        if self._reasoning_open:
            output.append(self._reasoning_item())
        if self._message_open:
            output.append(self._message_item(full))
        for st in sorted(self._tool_items.values(), key=lambda x: (x["idx"] is None, x["idx"] or 0)):
            if st["idx"] is None:
                st["idx"] = self._next_idx()
                out.append(self._ev({"type": "response.output_item.added", "output_index": st["idx"],
                                     "item": self._function_call_item(st, "in_progress")}))
            out.append(self._ev({"type": "response.function_call_arguments.done",
                                 "item_id": st["item_id"], "output_index": st["idx"],
                                 "arguments": st["args"]}))
            out.append(self._ev({"type": "response.output_item.done", "output_index": st["idx"],
                                 "item": self._function_call_item(st)}))
            output.append(self._function_call_item(st))

        u = usage or {}
        # chat.completions 用 prompt_tokens/completion_tokens，
        # Responses 用 input_tokens/output_tokens —— 两种命名都要认
        # （2026-09-16 补齐 usage 链路）
        usage_out = {
            "input_tokens": int(u.get("input_tokens", u.get("prompt_tokens", 0)) or 0),
            "output_tokens": int(u.get("output_tokens",
                                     u.get("completion_tokens", 0)) or 0),
            "total_tokens": int(u.get("total_tokens", 0) or 0),
        }
        out.append(self._ev({"type": "response.completed", "response": {
            "id": self.resp_id, "object": "response", "created_at": self.created_at,
            "model": self.model, "status": "completed", "output": output,
            "output_text": full, "usage": usage_out}}))
        return out
