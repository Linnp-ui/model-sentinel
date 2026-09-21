"""
Routing — 通用 provider 转发层
src/gateway/routing.py

安全审查（policy.yaml）决定"外网 vs 本地"，本层只负责"怎么转发到具体端点"。

核心规则：安全决策优先于客户端意愿。
  - action = route_local / block 时，强制走 local provider，
    客户端用 model 前缀或 X-Gateway-Provider 指定外网 provider 一律拒绝，
    记录到 notes["override_denied"]，避免出现绕过审查的外发通道。
  - action = allow 时，才允许客户端在外网 provider 之间切换。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any, AsyncIterator, Dict, Mapping, Tuple, Optional

import httpx

from .providers import Provider, RoutingConfig, load_routing
from .identity import current_identity as _current_identity
from .provider_keys import (
    is_gateway_only,
    gateway_key as _gw_pool_key,
    may_use_gateway_key as _may_use_gw_key,
    note_passthrough as _note_passthrough,
    note_rejected as _note_rejected,
    note_exhausted as _note_exhausted,
)
from .circuit_breaker import get_breaker, CircuitOpenError
from .user_models import find_provider
from .llms.registry import get_llm_handler
from .llms.bridges import (
    anthropic_text,
    responses_text,
    sse as _sse,
    anthropic_to_openai as _anthropic_to_openai,
    openai_to_anthropic as _openai_to_anthropic,
    anthropic_stream_events as _anthropic_stream_events,
    parse_openai_sse_deltas as _parse_openai_sse_deltas,
    responses_to_chat as _responses_to_chat,
    chat_to_responses as _chat_to_responses,
    responses_stream_events as _responses_stream_events,
    ResponsesSSEAssembler as _ResponsesSSEAssembler,
)

# 连接池按 (事件循环, 是否内网, 代理) 分组：
#   - 事件循环维度：httpx 连接绑定在创建它的 loop 上，TestClient/多 loop 场景复用会报
#     "Event loop is closed"，所以按 loop 隔离并在 loop 关闭后丢弃。
#   - 代理维度：网关一律 trust_env=False。httpx 的 trust_env=True 会读
#     urllib.getproxies()（Windows 上连带 WinINET/系统代理），开发机与 CI 常被注入
#     本地代理，于是发往 127.0.0.1 / 192.168.x.x 的请求也会被转发出去（既慢又可能
#     出网），上游连接失败还会被代理自己的 502 text/plain 顶掉，掩盖真实错误。
#     确需经代理出网的部署用 AI_GATEWAY_UPSTREAM_PROXY 显式声明（仅外网 provider 生效）。
_CLIENTS: Dict[Tuple[Optional[asyncio.AbstractEventLoop], bool, Optional[str]], httpx.AsyncClient] = {}

_UPSTREAM_PROXY_ENV = "AI_GATEWAY_UPSTREAM_PROXY"

# 默认 max_tokens：本地 thinking 模型（qwen2.5:7b）的思考链会长到吃光 300 token，
# 导致 finish_reason=length 且 content=None（客户端显示"无返回内容"）。
# 取 32768 与 vLLM 侧同量级（`--max-model-len 1010000` 是 prompt+输出**总预算**，
# 不能把 max_tokens 设成它 —— 实测会被 vLLM 直接 400 拒绝）。
# vLLM 按实际生成长度分配 KV cache，声明大 max_tokens 不会预占显存；
# 实测 8 并发（= `--max-num-seqs 8`）全部 200，无 KV 压力。
# 仍可由环境变量覆盖，或由客户端显式传 max_tokens 顶掉。
_DEFAULT_MAX_TOKENS = int(os.getenv("AI_GATEWAY_DEFAULT_MAX_TOKENS", "32768"))

# 本地（内网）路径的输出上限缺省值。
# 2026-09-15 两次教训的平衡点：
#   - 太大（32768）：单请求可跑到 100s+，长尾拖高 P95；
#   - 太小（8192）：**长回答被 section 截断**，codex 表现为"回复中断"
#     （实测 finish_reason=length / completion_tokens=8192 / 正文砍在半句话上）。
# 结论：缺省回到与外网同量级的 32768（不截断优先），延迟主要靠
# `_wants_thinking()` 关思考链来压（实测 28.8s → 4.5s）；
# 需要更紧的长尾控制时用本 env 旋钮下调，不要改代码默认。
_LOCAL_DEFAULT_MAX_TOKENS = int(os.getenv("AI_GATEWAY_LOCAL_MAX_TOKENS", "65536"))

# 内网 thinking 模型的关闭开关：vLLM 认 chat_template_kwargs.enable_thinking=false。
# 客户端若**显式**表达 thinking 意图，则尊重之（不注入）。
#
# 注意（2026-09-15 延迟修复）：`reasoning` 不能整体算作"意图"——codex 类客户端
# **每次请求**都带 `reasoning: {effort: medium, summary: auto}`，那是默认载荷而非
# 用户诉求。实测同一 prompt：带 reasoning 28.8s（837 思考 delta）vs 不带 4.5s。
# 因此只在 effort ∈ _THINKING_EFFORTS（默认 high/xhigh/max）时才开思考。
_THINKING_HINT_KEYS = ("enable_thinking", "thinking", "include_reasoning",
                       "extended_thinking")
_THINKING_EFFORTS = tuple(
    x.strip().lower() for x in
    (os.getenv("AI_GATEWAY_LOCAL_THINKING_EFFORTS", "high,xhigh,max") or "").split(",")
    if x.strip()
)
_FALSY = ("false", "0", "off", "no")

def _chat_usage_enabled() -> bool:
    """chat 流式是否向上游要 usage（stream_options.include_usage）。

    chat.completions 流式默认**不返回** usage ⇒桥接出的 response.completed
    里 usage 恒为 0，客户端 token 统计失效（2026-09-16 补）。
    env AI_GATEWAY_CHAT_INCLUDE_USAGE=false 可回滚（上游不认该字段时用）。
    """
    return os.getenv("AI_GATEWAY_CHAT_INCLUDE_USAGE", "true").strip().lower() \
        not in ("0", "false", "no", "off")


def _wants_thinking(body: Dict[str, Any]) -> bool:
    """客户端是否**显式**要求思考链（决定内网 provider 是否关 thinking）。

    - 显式开关：enable_thinking / thinking / include_reasoning / extended_thinking 为真；
    - reasoning：仅当是 bool True、字符串或 dict 的 effort 命中 _THINKING_EFFORTS。
      codex 的 `reasoning:{effort:medium,summary:auto}` 返回 False（默认关思考）。
    """
    for k in _THINKING_HINT_KEYS:
        v = body.get(k)
        if v is True:
            return True
        if isinstance(v, str) and v.strip() and v.strip().lower() not in _FALSY:
            return True
    r = body.get("reasoning")
    if r is True:
        return True
    if isinstance(r, str):
        return r.strip().lower() in _THINKING_EFFORTS
    if isinstance(r, dict):
        return str(r.get("effort") or "").strip().lower() in _THINKING_EFFORTS
    return False


def _apply_local_model_defaults(body: Dict[str, Any], prov: Provider) -> Dict[str, Any]:
    """内网 thinking 模型的网关侧兜底（原地改 body 并返回，便于链式调用）。

    背景：本地模型是"内容不出境"的兜底通道，职责是正常作答而非长链推理。
    vLLM 的 `--reasoning-parser qwen3` 会把思考段切进 `reasoning` 字段、正文留 `content`；
    思考未闭合/过长时 `content` 会是 None，客户端表现为"无返回内容"。

    两条兜底：
      1. max_tokens 缺省（或等于网关兜底默认）时给 `_LOCAL_DEFAULT_MAX_TOKENS`；
      2. 若客户端没**显式**表达 thinking 意愿，注入 `chat_template_kwargs.enable_thinking=false`，
         让输出直接进 content（codex 默认载荷里的 reasoning 不算显式意愿）。
    """
    if not prov.local:
        return body
    try:
        _mt = int(body.get("max_tokens") or 0)
    except (TypeError, ValueError):
        _mt = 0
    if _mt == 0 or _mt == _DEFAULT_MAX_TOKENS:
        # 缺省值，或等于网关层兜底填的默认值（main._apply_token_cap / bridges）
        # ⇒ 本地路径改用 _LOCAL_DEFAULT_MAX_TOKENS（客户端显式给的其他值不动）
        body["max_tokens"] = _LOCAL_DEFAULT_MAX_TOKENS
    # 客户端已经通过 chat_template_kwargs 表达过意图 ⇒ 不覆盖
    ctk = body.get("chat_template_kwargs")
    if isinstance(ctk, dict) and "enable_thinking" in ctk:
        return body
    # 客户端显式要求思考 ⇒ 尊重（本地默认关思考，见 _wants_thinking）
    if _wants_thinking(body):
        return body
    ctk = dict(ctk) if isinstance(ctk, dict) else {}
    ctk["enable_thinking"] = False
    body["chat_template_kwargs"] = ctk
    return body


def upstream_proxy(local: bool = False) -> Optional[str]:
    """外网 provider 的显式代理地址；内网 provider 恒为 None（永远直连）。"""
    if local:
        return None
    return (os.getenv(_UPSTREAM_PROXY_ENV, "") or "").strip() or None


def _get_client(local: bool = False) -> httpx.AsyncClient:
    """按 (事件循环, 是否内网, 代理) 复用连接池。

    local=True（内网 provider）永远直连；外网 provider 也只在显式配置
    AI_GATEWAY_UPSTREAM_PROXY 时才走代理 —— 不读系统/环境代理变量。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # 无事件循环（同步上下文）时退化成单连接池
        loop = None  # type: ignore[assignment]

    for l in {k[0] for k in _CLIENTS}:
        if l is not None and l.is_closed():
            for k in [k for k in _CLIENTS if k[0] is l]:
                _CLIENTS.pop(k, None)

    proxy = upstream_proxy(local)
    key = (loop, local, proxy)
    client = _CLIENTS.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=60,
            trust_env=False,  # 不读系统/环境代理，避免被开发机本地代理劫持
            proxy=proxy,  # 需要代理时由 AI_GATEWAY_UPSTREAM_PROXY 显式指定
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
        )
        if loop is not None:
            _CLIENTS[key] = client
    return client


def get_proxy_client(local: bool = False):
    """/v1/* 兜底转发用的连接池（对外暴露给 main.py）"""
    return _get_client(local)


async def aclose_clients() -> None:
    """优雅退出时关闭连接池（uvicorn shutdown 钩子调用）"""
    for c in list(_CLIENTS.values()):
        try:
            await c.aclose()
        except Exception:
            pass
    _CLIENTS.clear()


class RoutingError(Exception):
    """路由层错误，由 main.py 转成 4xx/5xx 响应"""

    def __init__(self, message: str, status_code: int = 502, type_: str = ""):
        super().__init__(message)
        self.status_code = status_code
        # 供 main.py 生成 detail.type（缺省回落 routing_error，保持旧报文兼容）
        self.type_ = type_ or ""


class _ResponsesUnsupported(Exception):
    """上游不提供 /v1/responses ⇒ 调用方应回退 chat.completions 桥接。"""


def _responses_direct_enabled() -> bool:
    """本地/route_local 是否优先直通 /v1/responses（默认开；env 可回滚）。

    取证（2026-09-16）：本地 vLLM 原生支持 /v1/responses，且能完整接受 Codex 的
    真实请求体（client_metadata / prompt_cache_key / namespace 工具），事件齐全
    （response.in_progress / reasoning_part.* / function_call item 带 id）。
    """
    return os.getenv("AI_GATEWAY_LOCAL_RESPONSES_DIRECT", "true").strip().lower() \
        not in ("0", "false", "no", "off")


async def _responses_direct_stream(
    prov,
    body: Dict[str, Any],
    ukey: str,
    fallback_codes: Tuple[int, ...] = (404, 405, 501),
) -> AsyncIterator[bytes]:
    """直通上游 /v1/responses 的 SSE 流（原直通块抽出，本地/外网共用）。

    首字节前命中 fallback_codes 或连接失败 ⇒ raise _ResponsesUnsupported，
    由调用方回退 chat 桥接；其余 >=400 抛 RoutingError（不静默吞掉）。
    """
    if not prov.local and not _has_key(prov, ukey):
        raise RoutingError(f"provider {prov.name} 未配置密钥", 503,
                            type_="provider_not_configured")
    client = _get_client(prov.local)
    status = 0
    detail = ""
    try:
        async with client.stream("POST", prov.endpoint("proxy", custom_path="responses"),
                                 headers=prov.headers(ukey), json=body,
                                 timeout=STREAM_TIMEOUT) as r:
            status = r.status_code
            if status >= 400:
                # 流式上下文里 body 未 read，直接访问 r.text 会抛 ResponseNotRead
                raw = await r.aread()
                detail = raw.decode("utf-8", errors="replace")[:300]
            else:
                async for chunk in r.aiter_bytes():
                    yield chunk
                return
    except httpx.HTTPError as exc:
        # 连不上：交回调用方决定（外网会再试同源 chat，最终仍失败则 502→首块降级）
        raise _ResponsesUnsupported(f"{prov.name} unreachable: {exc}") from exc
    if status in tuple(fallback_codes):
        raise _ResponsesUnsupported(f"{prov.name} {status}: {detail}")
    raise RoutingError(f"{prov.name} {status}: {detail}", status)


# ---------------------------------------------------------------- 解析

def _headers_get(headers: Mapping[str, str] | None, key: str) -> str | None:
    if not headers:
        return None
    return headers.get(key) or headers.get(key.title())


def _request_credential(headers: Mapping[str, str] | None) -> str:
    """**客户端自带**的上游凭据（BYOK）；没带 ⇒ 空串。

    - ``gateway_only``：携带 ``X-Upstream-Api-Key`` ⇒ 400 ``upstream_key_not_accepted``；
      ``Authorization`` 只承担鉴权/身份，**不再**当上游凭据。
    - ``passthrough``（默认，灰度期）：``X-Upstream-Api-Key`` > ``Authorization Bearer`` /
      ``x-api-key``；网关自发的进门 key（``pk_live_`` / ``gwk_``）与 dev key 过滤掉。

    **每请求只应调用一次** —— ``gateway_upstream_key_passthrough_total`` 是切
    ``gateway_only`` 的判据（方案 §D4），重复解析会让它永远归不了零。
    """
    explicit = (_headers_get(headers, "x-upstream-api-key") or "").strip()
    if is_gateway_only():
        if explicit:
            _note_rejected()
            raise RoutingError(
                "X-Upstream-Api-Key 不再被接受：上游凭据由网关统一持有"
                "（upstream_key_not_accepted）",
                400,
                type_="upstream_key_not_accepted",
            )
        return ""
    if explicit:
        _note_passthrough()
        return explicit
    dev_key = os.getenv("AI_GATEWAY_DEV_API_KEY", "pk_live_dev_changeme")
    for name in ("authorization", "x-api-key"):
        raw = (_headers_get(headers, name) or "").strip()
        if not raw:
            continue
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
        if raw and raw != dev_key and not _looks_like_gateway_key(raw):
            _note_passthrough()
            return raw
    return ""


def upstream_key(
    headers: Mapping[str, str] | None,
    identity=None,
    prov: Provider | None = None,
) -> str:
    """本次请求**对该 provider 实际使用**的上游密钥（P1 起：BYOK → 网关持凭据）。

    ``prov`` = 本次实际要用的 provider；省略 ⇒ 回落 ``default_external``。

    ⚠️ 网关持的凭据是 **per-provider** 的（``provider_keys.gateway_key(prov)``）：把 A 家的
    key 打到 B 家 ⇒ 401，且审计里看不出来源。已知限制（P2 待办）：别名组 / 多候选路径在
    循环外只解析一次凭据 ⇒ 回落恒取 ``default_external``。生产别名组成员同属 ``deepseek``
    （2026-09-17 实测 ext-flash / ext-pro 各 1 名成员），故当前无实际影响；**跨 provider 混排
    的别名组必须先改成 per-candidate 解析**。

    模式由 ``AI_GATEWAY_UPSTREAM_KEY_MODE`` 决定（见 ``provider_keys``）。回落准入 =
    「具名登记身份」（``provider_keys.may_use_gateway_key``）：未登记身份（env 共享 dev key /
    上游验签放行 / 匿名）一律拿不到 ⇒ 空串，由 ``validate_environment()`` 给 503
    （``on_missing_key: reject``），**绝不 mock**。

    ``identity`` 省略时读 ``identity.current_identity()``（main._bind_identity 写入）。
    """
    ident = identity if identity is not None else _current_identity()
    return _request_credential(headers) or _gateway_upstream_key(ident, prov)


def _looks_like_gateway_key(token: str) -> bool:
    """网关自己发的进门 key（不可当上游凭据用）。

    P1 顺带补上 ``gwk_``：原实现只过滤 ``pk_live_``，而 P2 生成的客户端 key 是
    ``AI_GATEWAY_KEY_PREFIX``（默认 ``gwk_``）⇒ 不改的话，客户端的网关 key 会被
    当成上游凭据原样打到厂商（吃 401，且审计里看不出来源）。
    """
    prefix = (os.getenv("AI_GATEWAY_KEY_PREFIX") or "gwk_").strip() or "gwk_"
    return token.startswith("pk_live_") or token.startswith(prefix)


def _gateway_upstream_key(identity, prov: Provider | None = None) -> str:
    """回落路径：取网关持有的上游凭据（P1 只读 env；P2 起池 → env）。

    ``prov`` 必须传**本次实际要用的 provider** —— 凭据池是 per-provider 的。省略时回落
    ``default_external``，只留给「别名组候选共用一把凭据」的路径（见 ``upstream_key``）。

    未登记身份 / 无可用凭据 ⇒ 空串（fail-closed）。**不抛错** —— 让
    ``validate_environment()`` 统一给 503 报文，避免错误在多层重复构造。
    """
    if not _may_use_gw_key(identity):
        return ""
    if prov is None:
        prov = load_routing().external_provider()
    if prov is None or prov.local:
        return ""
    key = _gw_pool_key(prov)
    if not key:
        _note_exhausted(prov.name)
    return key


def _has_key(prov: Provider, override: str) -> bool:
    """外网 provider 本次请求是否具备真实转发条件。

    **只看本次解析出的凭据**（``upstream_key()`` 的返回值）。``prov.configured``
    是「网关侧有库存」，不是「本请求有权用」；混用会让未登记身份（env 共享
    dev key / 验签放行 / 匿名）也白吃公司额度（T44 生产实测 2026-09-17）。
    """
    return bool(override)


def resolve(
    decision: Dict[str, Any],
    payload: Dict[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    kind: str = "chat",
) -> Tuple[Provider, str, Dict[str, Any]]:
    """
    决策 -> (provider, model, notes)
    覆盖优先级：policy target  <  model 前缀  <  X-Gateway-Provider 头
    """
    cfg: RoutingConfig = load_routing()
    payload = payload or {}
    action = (decision or {}).get("action", "allow")
    target = (decision or {}).get("target") or {}
    notes: Dict[str, Any] = {}

    # 审查不通过（route_local / block）一律强制本地，这是安全底线
    force_local = action in ("route_local", "block")

    if kind == "embedding":
        name = target.get("provider") or cfg.default_embedding
    elif force_local:
        name = target.get("provider") or cfg.default_local
    else:
        name = target.get("provider") or cfg.default_external
    model = target.get("model") or ""

    # ① model 前缀：deepseek/deepseek-chat（含用户自定义模型）
    req_model = str(payload.get("model") or "")
    if "/" in req_model:
        prefix, _, rest = req_model.partition("/")
        cand = cfg.get(prefix) or find_provider(prefix)
        if cand is not None:
            if force_local and not cand.local:
                notes["override_denied"] = prefix
            else:
                # 特殊：deepseek 是 openrouter 的别名，需保留完整模型名 deepseek/deepseek-chat 供 openrouter 使用
                if prefix == "deepseek" and "openrouter" in (cand.base_url or ""):
                    name, model = prefix, req_model
                else:
                    name, model = prefix, rest or ""
        else:
            # 未知前缀（如 minimax/minimax-m3:free）视为 openrouter 的完整模型名，保留原始 req_model
            if not force_local and not model:
                model = req_model

    # ② 请求头覆盖
    hdr_provider = _headers_get(headers, "x-gateway-provider")
    if hdr_provider:
        cand = cfg.get(hdr_provider) or find_provider(hdr_provider)
        if cand is None:
            notes["override_unknown"] = hdr_provider
        elif force_local and not cand.local:
            notes["override_denied"] = hdr_provider
        else:
            name = hdr_provider
    hdr_model = _headers_get(headers, "x-gateway-model")
    if hdr_model and not force_local:
        model = hdr_model

    # ③ 精确模型名 = 用户自定义模型名（注册后可直接当 model 用）
    if not force_local and "/" not in req_model and req_model:
        cand = find_provider(req_model)
        if cand is not None:
            name, model = req_model, cand.default_model
        elif not model:
            model = req_model  # 裸模型名保留

    prov = cfg.get(name) or find_provider(name)

    # 嵌入请求必须落在 kind=embedding 的 provider 上
    # （策略里的 ollama/openai 都是 chat provider，不能拿来做向量化）
    # 纠正 provider 后原 model 名（如 gpt-4o-mini）不再有效，必须重算为该 provider 的 embedding 模型
    if kind == "embedding" and prov is not None and prov.kind != "embedding":
        prov = cfg.embedding_provider()
        model = ""

    # 决策要求本地，但解析到外网 provider（配置写错或 target 指错）-> 拉回本地
    if prov is not None and force_local and not prov.local:
        notes["override_denied"] = prov.name
        prov = None

    if prov is None:
        if force_local:
            prov = cfg.embedding_provider() if kind == "embedding" else cfg.local_provider()
        else:
            prov = cfg.external_provider() or cfg.local_provider()
        notes["resolved_fallback"] = True

    if prov is None:
        raise RoutingError("no provider available in routing.yaml", 503)

    final_model = model or prov.model_for(kind)

    # 模型限流（model_policy.model_limits）：最终目标的全局 RPM，超限 429。
    # 放在 resolve 出口 = 所有端点（chat/messages/responses/embeddings/兜底转发）
    # 与 fallback 换候选路径同一卡口；按「最终落到的模型」计，别名/前缀重写后不逃限。
    try:
        from .model_policy import model_rpm
        from .rate_limit import get_limiter
        rpm = model_rpm(prov.name, final_model)
        if rpm > 0:
            ok, retry = get_limiter().check_model(f"{prov.name}/{final_model}", rpm)
            if not ok:
                raise RoutingError(
                    f"模型 {final_model} 触发限流（全局 {rpm} 次/分钟），约 {retry}s 后重试",
                    429, type_="model_rate_limited")
    except RoutingError:
        raise
    except Exception:
        # 限流配置读取失败不阻断业务（fail-open，与 policy 加载容错一致）
        pass

    return prov, final_model, notes


# ---------------------------------------------------------------- 转发

def _mock_enabled(cfg: RoutingConfig, prov: Provider, has_override: bool = False) -> bool:
    """
    是否返回模拟响应（演示/联调用）。
      - 内网 provider：OLLAMA_MOCK=true 时返回 mock，方便无 GPU 环境联调
      - 外网 provider：**本次请求没解析到凭据**（``upstream_key()`` 返回空）时按
        routing.on_missing_key 决定是否 mock；解析到凭据就一定真实转发。
        判据**不**看 ``prov.configured`` —— 那是「网关有库存」，不代表本请求
        有权用（T44 修），否则会把「没资格」的调用方 mock 成 200 假答案。
    """
    if prov.local:
        return os.getenv("OLLAMA_MOCK", "true").lower() == "true"
    if has_override:
        return False
    if is_gateway_only():
        # C5：gateway_only 下网关侧无可用凭据 ⇒ **恒** False。宁可 503，
        # 也不回一个 HTTP 200 的假答案（生产 ``on_missing_key: reject`` 本就
        # 到不了这里，但配置被改回 mock 时不能再有静默伪造答案的通道）。
        return False
    return cfg.on_missing_key == "mock"


def _mock_text(prov: Provider, model: str, decision: Dict[str, Any]) -> str:
    if prov.local:
        return (
            f"[内网模型 {model} 已处理 — 内容命中策略 {decision.get('name')}，"
            f"已转本地模型，未出境。]"
        )
    return "[外网模型 mock — 本请求未解析到上游凭据（on_missing_key=mock），未真实出境。]"


def _mock_chat(prov: Provider, model: str, decision: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": _mock_text(prov, model, decision)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def _post(prov: Provider, url: str, body: Dict[str, Any], api_key_override: str = "") -> httpx.Response:
    client = _get_client(prov.local)
    breaker = get_breaker()
    if not breaker.allow(prov.name):
        from .metrics import get_metrics
        get_metrics().inc_circuit_short()
        s = breaker.snapshot().get(prov.name, {})
        # compute retry_after
        recovery = int(os.getenv("AI_GATEWAY_CB_RECOVERY_SECONDS", "30"))
        retry_after = max(0.0, recovery - (time.time() - (s.get("opened_at") or 0)))
        raise RoutingError(
            f"circuit open for {prov.name} (failures={s.get('failure_count_window', 0)}, retry after {retry_after:.0f}s)",
            503,
        )
    try:
        resp = await client.post(url, headers=prov.headers(api_key_override), json=body,
                             timeout=_upstream_timeout(prov))
    except httpx.HTTPError as exc:
        breaker.record_failure(prov.name)
        raise RoutingError(f"{prov.name} unreachable: {exc}", 502) from exc
    if resp.status_code >= 500:
        breaker.record_failure(prov.name)
    else:
        breaker.record_success(prov.name)
    return resp


def _alias_ctx_failover(from_ref: str) -> None:
    from .alias_router import ctx_record_failover
    ctx_record_failover(from_ref)


def _alias_ctx_start(name: str) -> None:
    from .alias_router import ctx_start
    ctx_start(name)


def _alias_ctx_now():
    from .alias_router import ctx_get
    return ctx_get()


def _alias_context(payload: Dict[str, Any], decision: Dict[str, Any],
                   headers: Mapping[str, str] | None) -> Optional[Dict[str, Any]]:
    """model=对外别名 时返回别名组；仅 allow 动作且无 X-Gateway-Provider 覆盖时生效。"""
    if (decision or {}).get("action", "allow") != "allow":
        return None
    if _headers_get(headers, "x-gateway-provider"):
        return None
    from .alias_router import find_alias
    return find_alias(str((payload or {}).get("model") or ""))


def _alias_resolve(payload: Dict[str, Any], decision: Dict[str, Any],
                   headers: Mapping[str, str] | None):
    """别名组 -> (prov, model, alias)；非别名/不允许别名时返回 None。

    抽成公共函数，是为了让**所有**端点共用同一份逻辑。
    2026-09-16 生产事故：`route_responses` / `route_responses_stream` 直接调
    `resolve()`、漏接别名解析。`resolve()` 对未知裸模型名是「原样保留」，
    于是客户端的 `model="ext-flash"` 被原样发给上游 ⇒ 上游按「模型不存在」4xx。
    Codex 的 `wire_api = "responses"` 正好命中 ⇒ 表现为「对话无返回内容」。
    chat / chat_stream / messages / messages_stream 四处一直是对的。

    注意：这里**不做**候选故障切换（chat 路径在转发循环里做）。别名组当前都是
    单成员，失败即 fail-loud，避免把原文静默转投到未预期的 provider。
    """
    alias = _alias_context(payload, decision, headers)
    if alias is None:
        return None
    from .alias_router import alias_candidates as _alias_candidates
    _c0 = _alias_candidates(alias)
    if not _c0:
        raise RoutingError(f"alias {alias['name']}: no available candidate (circuit open)", 503)
    _c = _c0[0]
    cfg = load_routing()
    prov = cfg.get(_c["provider"]) or find_provider(_c["provider"])
    if prov is None:
        raise RoutingError(f"alias {alias['name']}: unknown provider {_c['provider']}", 503)
    _alias_ctx_start(alias["name"])
    from .alias_router import ctx_set_current as _alias_setcur
    _alias_setcur(prov.name, _c["model"], prov.local)
    return prov, _c["model"], alias


def _mp_fallback_enabled() -> bool:
    return os.getenv("AI_GATEWAY_MODEL_FALLBACK", "true").lower() not in ("0", "false", "no", "off")


def _mp_initial_ctx(prov_name: str, model: str) -> int:
    from .model_policy import external_candidates
    for c in external_candidates():
        if c["provider"] == prov_name and c["model"] == model:
            return c.get("context_length", 0)
    return 0


def _mp_next(tried: list, reason: str, cur_ctx: int, cfg, ukey, kind: str = "chat"):
    """Next external candidate (prov, model, ctx) or None."""
    from .model_policy import pick_fallback_candidates
    for cand in pick_fallback_candidates(tried, reason, cur_ctx):
        pcfg = cfg.get(cand["provider"]) or find_provider(cand["provider"])
        if pcfg is None or pcfg.local or pcfg.kind != kind:
            continue
        if not (ukey or pcfg.local):
            continue
        return (pcfg, cand["model"], cand.get("context_length", 0))
    return None


async def route_chat(
    payload: Dict[str, Any],
    decision: Dict[str, Any],
    headers: Mapping[str, str] | None = None,
) -> Dict[str, Any]:
    cfg = load_routing()
    # 安全底线（与 resolve() 的 force_local 同一语义）：审查不通过（route_local / block）
    # 的请求只允许落在本地 provider 上。下方 fallback 候选来自 model_policy.yaml 的
    # external_candidates()——全是外网 provider——本地故障时切过去等于把敏感原文转发出境，
    # 所以这里显式禁用切换，失败即 fail-loud。
    _pinned_local = (decision or {}).get("action", "allow") in ("route_local", "block")
    alias = _alias_context(payload, decision, headers)
    if alias is not None:
        from .alias_router import alias_candidates as _alias_candidates
        _c0 = _alias_candidates(alias)
        if not _c0:
            raise RoutingError(f"alias {alias['name']}: no available candidate (circuit open)", 503)
        _c = _c0[0]
        prov = cfg.get(_c["provider"]) or find_provider(_c["provider"])
        if prov is None:
            raise RoutingError(f"alias {alias['name']}: unknown provider {_c['provider']}", 503)
        model = _c["model"]
        _alias_ctx_start(alias["name"])
        from .alias_router import ctx_set_current as _alias_setcur
        _alias_setcur(prov.name, model, prov.local)
        _alias_next = _c["idx"] + 1
    else:
        prov, model, _ = resolve(decision, payload, headers, kind="chat")

    # 凭据解析必须在 prov 定了之后：网关回落是 **per-provider** 的。原实现恒取
    # default_external ⇒ pin 别的 provider / 用户自配模型时会拿到**别家**的 key（401）。
    ukey = upstream_key(headers, prov=prov)

    if _mock_enabled(cfg, prov, bool(ukey)):
        return _mock_chat(prov, model, decision)

    # --- 1.2 多模型策略（状态驱动切换）：仅非流式 chat ---
    # 报错/异常中断 -> 换下一个供应商模型（rank/价格序）；上下文溢出 -> 换更长上下文模型。
    # 候选来自 model_policy.yaml（mtime 热重载）；AI_GATEWAY_MODEL_FALLBACK=false 关闭。
    from .model_policy import classify_failure, push_fallback

    mp_on = _mp_fallback_enabled()
    cur_ctx = _mp_initial_ctx(prov.name, model) if mp_on else 0

    tried: list = []
    while True:
        _err = get_llm_handler(prov.api_mode).validate_environment(prov, ukey)
        exc: RoutingError | None = None
        r = None
        if not _err:
            handler = get_llm_handler(prov.api_mode)
            body = {k: v for k, v in payload.items() if k != "model"}
            body["model"] = model
            body = _apply_local_model_defaults(body, prov)
            try:
                r = await _post(prov, prov.endpoint("chat", model=model), handler.transform_request(body, model), ukey)
                if r.status_code >= 400:
                    raise RoutingError(f"{prov.name} {r.status_code}: {r.text[:300]}", r.status_code)
            except RoutingError as e2:
                exc = e2
        else:
            exc = RoutingError(_err, 503, type_="provider_not_configured")
        if exc is None:
            if alias is not None:
                from .alias_router import record_alias_result
                record_alias_result(prov.name, model, True, local=prov.local)
            return handler.transform_response(r.json(), model)
        tried.append(f"{prov.name}/{model}")
        if alias is not None:
            from .alias_router import record_alias_result
            record_alias_result(prov.name, model, False)
            _alias_ctx_failover(f"{prov.name}/{model}")
        reason = classify_failure(exc.status_code, str(exc))
        nxt = None
        if alias is not None:
            from .alias_router import alias_candidates as _alias_candidates
            for cand in _alias_candidates(alias, skip=_alias_next):
                pcfg = cfg.get(cand["provider"]) or find_provider(cand["provider"])
                if pcfg is None or pcfg.local or pcfg.kind != "chat":
                    continue
                if not (ukey or pcfg.local):
                    continue
                nxt = (pcfg, cand["model"], 0)
                _alias_next = cand["idx"] + 1
                break
        else:
            # _pinned_local 时不给任何候选 -> 本地失败直接 fail-loud，原文不出境
            _nxt = (None if (not mp_on or _pinned_local) else
                    _mp_next(tried, reason, cur_ctx, cfg,
                             upstream_key(headers, prov=prov)))
            if _nxt is not None:
                nxt = _nxt
        if nxt is None:
            raise exc
        push_fallback({
            "from": tried[-1],
            "to": f"{nxt[0].name}/{nxt[1]}",
            "reason": reason,
            "error": str(exc)[:200],
        })
        prov, model, cur_ctx = nxt[0], nxt[1], nxt[2]


# 流式请求专用超时：read 是"相邻 chunk 的最大间隔"，推理模型思考阶段可远超 60s。
# 可用环境变量覆盖；非流式请求仍用 provider 自己的 timeout。
STREAM_TIMEOUT = httpx.Timeout(
    connect=float(os.getenv("AI_GATEWAY_STREAM_CONNECT_TIMEOUT", "10")),
    read=float(os.getenv("AI_GATEWAY_STREAM_READ_TIMEOUT", "300")),
    write=float(os.getenv("AI_GATEWAY_STREAM_WRITE_TIMEOUT", "60")),
    pool=float(os.getenv("AI_GATEWAY_STREAM_POOL_TIMEOUT", "10")),
)

# 非流式非本地请求沿用 provider 自身 timeout；**本地** provider 放开到本旋钮：
# 长回答（缺省 32768 tokens @~120 tok/s ≈ 4.5min）远超 routing.yaml 的 120s，
# 会以 httpx 超时收场（502），此前还被误报成 prompt too long（见 2026-09-15 取证）。
_LOCAL_UPSTREAM_TIMEOUT = float(os.getenv("AI_GATEWAY_LOCAL_UPSTREAM_TIMEOUT", "300"))


def _upstream_timeout(prov) -> float:
    """内网 provider 用放宽的预算，外网沿用 provider 自身 timeout。"""
    return _LOCAL_UPSTREAM_TIMEOUT if getattr(prov, "local", False) else float(prov.timeout)


async def route_chat_stream(
    payload: Dict[str, Any],
    decision: Dict[str, Any],
    headers: Mapping[str, str] | None = None,
) -> AsyncIterator[bytes]:
    """chat 流式转发（对外别名路由包装）：首字节前失败可切组内下一候选。"""
    alias = _alias_context(payload, decision, headers)
    if alias is None:
        from .model_policy import classify_failure, push_fallback
        cfg = load_routing()
        _pinned_local = (decision or {}).get("action", "allow") in ("route_local", "block")
        mp_on = _mp_fallback_enabled()
        _prov0, _model0, _ = resolve(decision, payload, headers, kind="chat")
        cur_ctx = _mp_initial_ctx(_prov0.name, _model0) if mp_on else 0
        tried: list = []
        prov, model = _prov0, _model0
        while True:
            sent = False
            try:
                async for chunk in _chat_stream_once(payload, decision, headers, prov=prov, model=model):
                    sent = True
                    yield chunk
                return
            except RoutingError as exc:
                if sent:
                    raise
                tried.append(f"{prov.name}/{model}")
                reason = classify_failure(exc.status_code, str(exc))
                nxt = (None if (not mp_on or _pinned_local) else
                       _mp_next(tried, reason, cur_ctx, cfg, upstream_key(headers, prov=prov)))
                if nxt is None:
                    raise
                push_fallback({"from": tried[-1], "to": f"{nxt[0].name}/{nxt[1]}",
                               "reason": reason, "error": str(exc)[:200]})
                prov, model, cur_ctx = nxt[0], nxt[1], nxt[2]
                continue
    from .alias_router import alias_candidates
    _alias_ctx_start(alias["name"])
    cands = alias_candidates(alias)
    if not cands:
        raise RoutingError(f"alias {alias['name']}: no available candidate (circuit open)", 503)
    last_exc: RoutingError | None = None
    cfg = load_routing()
    ukey = upstream_key(headers)
    for cand in cands:
        prov = cfg.get(cand["provider"]) or find_provider(cand["provider"])
        if prov is None or prov.kind != "chat":
            continue
        if not (ukey or prov.local):
            continue
        sent = False
        try:
            async for chunk in _chat_stream_once(payload, decision, headers, prov=prov, model=cand["model"]):
                sent = True
                yield chunk
            return
        except RoutingError as exc:
            _alias_ctx_failover(f"{prov.name}/{cand['model']}")
            if sent:
                raise
            last_exc = exc
            continue
    if last_exc is not None:
        raise last_exc
    raise RoutingError(f"alias {alias['name']}: no usable candidate", 503)


async def _chat_stream_once(
    payload: Dict[str, Any],
    decision: Dict[str, Any],
    headers: Mapping[str, str] | None = None,
    prov=None,
    model=None,
) -> AsyncIterator[bytes]:
    """chat 流式转发：handler 决定线协议变换（openai 直通 / anthropic 逐行转换）"""
    cfg = load_routing()
    if prov is None:
        prov, model, _ = resolve(decision, payload, headers, kind="chat")
    ukey = upstream_key(headers, prov=prov)
    body = {k: v for k, v in payload.items() if k != "model"}
    body["model"] = model
    body = _apply_local_model_defaults(body, prov)

    handler = get_llm_handler(prov.api_mode)

    if _mock_enabled(cfg, prov, bool(ukey)):
        chunk = {
            "id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"content": _mock_chat(prov, model, decision)["choices"][0]["message"]["content"]}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"
        return

    _err = handler.validate_environment(prov, ukey)
    if _err:
        raise RoutingError(_err, 503, type_="provider_not_configured")

    breaker = get_breaker()
    if not breaker.allow(prov.name):
        from .metrics import get_metrics
        get_metrics().inc_circuit_short()
        s = breaker.snapshot().get(prov.name, {})
        recovery = int(os.getenv("AI_GATEWAY_CB_RECOVERY_SECONDS", "30"))
        retry_after = max(0.0, recovery - (time.time() - (s.get("opened_at") or 0)))
        raise RoutingError(
            f"circuit open for {prov.name} (failures={s.get('failure_count_window', 0)}, retry after {retry_after:.0f}s)",
            503,
        )

    client = _get_client(prov.local)
    treq = handler.transform_request(body, model, stream=True)
    try:
        async with client.stream("POST", prov.endpoint("chat", model=model), headers=prov.headers(ukey),
                                 json=treq, timeout=STREAM_TIMEOUT) as r:
            if r.status_code >= 400:
                # 流式上下文里 body 未 read，直接访问 r.text 会抛 ResponseNotRead
                if r.status_code >= 500:
                    breaker.record_failure(prov.name)
                    if (_alias_ctx_now() or {}).get("name"):
                        from .alias_router import record_alias_result as _rar
                        _rar(prov.name, model, False)
                raw = await r.aread()
                detail = raw.decode("utf-8", errors="replace")[:300]
                raise RoutingError(f"{prov.name} {r.status_code}: {detail}", r.status_code)
            breaker.record_success(prov.name)
            if (_alias_ctx_now() or {}).get("name"):
                from .alias_router import record_alias_result as _rar
                _rar(prov.name, model, True, local=prov.local)
            if handler.stream_passthrough:
                async for chunk in r.aiter_bytes():
                    yield chunk
            else:
                async for line in r.aiter_lines():
                    out = handler.transform_stream_line(line, model)
                    if out:
                        yield out
    except httpx.HTTPError as exc:
        breaker.record_failure(prov.name)
        if (_alias_ctx_now() or {}).get("name"):
            from .alias_router import record_alias_result as _rar
            _rar(prov.name, model, False)
        raise RoutingError(f"{prov.name} unreachable: {exc}", 502) from exc


async def route_embeddings(
    payload: Dict[str, Any],
    decision: Dict[str, Any],
    headers: Mapping[str, str] | None = None,
) -> Dict[str, Any]:
    cfg = load_routing()
    prov, model, _ = resolve(decision, payload, headers, kind="embedding")
    ukey = upstream_key(headers, prov=prov)

    if _mock_enabled(cfg, prov, bool(ukey)):
        n = len(payload.get("input") or []) if isinstance(payload.get("input"), list) else 1
        return {
            "object": "list", "model": model,
            "data": [{"object": "embedding", "index": i, "embedding": [0.0] * 8} for i in range(max(n, 1))],
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }
    _err = get_llm_handler(prov.api_mode).validate_environment(prov, ukey)
    if _err:
        raise RoutingError(f"provider {prov.name} 未配置密钥", 503,
                            type_="provider_not_configured")

    body = {k: v for k, v in payload.items() if k != "model"}
    body["model"] = model
    r = await _post(prov, prov.endpoint("embedding", model=model), body, ukey)
    if r.status_code >= 400:
        raise RoutingError(f"{prov.name} {r.status_code}: {r.text[:300]}", r.status_code)
    return r.json()


# ================================================================
# Anthropic Messages /v1/messages 与 OpenAI Responses /v1/responses
# ccswitch/Claude Code 走 Messages 协议，Codex CLI 走 Responses 协议。
# 入站格式不同，审查与路由复用同一套逻辑；这里只做协议转换。
# ================================================================

async def route_messages(
    body: Dict[str, Any],
    decision: Dict[str, Any],
    headers: Mapping[str, str] | None = None,
) -> Dict[str, Any]:
    """Anthropic /v1/messages 非流式：anthropic 上游直通，其余转换"""
    cfg = load_routing()
    alias = _alias_context(body, decision, headers)
    if alias is not None:
        # 别名组：候选共用一次解析（见 upstream_key 的已知限制）
        ukey = upstream_key(headers)
        from .alias_router import alias_candidates, record_alias_result
        _alias_ctx_start(alias["name"])
        cands = alias_candidates(alias)
        if not cands:
            raise RoutingError(f"alias {alias['name']}: no available candidate (circuit open)", 503)
        last_exc: RoutingError | None = None
        for cand in cands:
            pcfg = cfg.get(cand["provider"]) or find_provider(cand["provider"])
            if pcfg is None or pcfg.local or pcfg.kind != "chat":
                continue
            if not (ukey or pcfg.local):
                continue
            try:
                out = await _messages_once(pcfg, cand["model"], body, ukey)
                record_alias_result(pcfg.name, cand["model"], True, local=pcfg.local)
                return out
            except RoutingError as e:
                record_alias_result(pcfg.name, cand["model"], False)
                _alias_ctx_failover(f"{pcfg.name}/{cand['model']}")
                last_exc = e
                continue
        if last_exc is not None:
            raise last_exc
        raise RoutingError(f"alias {alias['name']}: no usable candidate", 503)

    prov, model, _ = resolve(decision, {"model": str(body.get("model") or "")}, headers, kind="chat")
    ukey = upstream_key(headers, prov=prov)
    if _mock_enabled(cfg, prov, bool(ukey)):
        return {
            "id": "msg_gateway", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": _mock_text(prov, model, decision)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    from .model_policy import classify_failure, push_fallback
    _pinned_local = (decision or {}).get("action", "allow") in ("route_local", "block")
    mp_on = _mp_fallback_enabled()
    cur_ctx = _mp_initial_ctx(prov.name, model) if mp_on else 0
    tried: list = []
    while True:
        try:
            return await _messages_once(prov, model, body, ukey)
        except RoutingError as exc:
            tried.append(f"{prov.name}/{model}")
            reason = classify_failure(exc.status_code, str(exc))
            nxt = (None if (not mp_on or _pinned_local) else
                   _mp_next(tried, reason, cur_ctx, cfg,
                            upstream_key(headers, prov=prov)))
            if nxt is None:
                raise
            push_fallback({"from": tried[-1], "to": f"{nxt[0].name}/{nxt[1]}",
                           "reason": reason, "error": str(exc)[:200]})
            prov, model, cur_ctx = nxt[0], nxt[1], nxt[2]
            ukey = upstream_key(headers, prov=prov)


async def _messages_once(prov, model: str, body: Dict[str, Any], ukey: str | None) -> Dict[str, Any]:
    """单次上游尝试（/v1/messages 非流式）"""
    _err = get_llm_handler(prov.api_mode).validate_environment(prov, ukey)
    if _err:
        raise RoutingError(_err, 503, type_="provider_not_configured")

    if prov.api_mode == "anthropic":
        out = {k: v for k, v in body.items() if k != "model"}
        out["model"] = model
        r = await _post(prov, prov.endpoint("chat"), out, ukey)
        if r.status_code >= 400:
            raise RoutingError(f"{prov.name} {r.status_code}: {r.text[:300]}", r.status_code)
        data = r.json()
        data["model"] = model
        return data

    r = await _post(prov, prov.endpoint("chat"), _anthropic_to_openai(body, model), ukey)
    if r.status_code >= 400:
        raise RoutingError(f"{prov.name} {r.status_code}: {r.text[:300]}", r.status_code)
    return _openai_to_anthropic(r.json(), model)


async def route_messages_stream(
    body: Dict[str, Any],
    decision: Dict[str, Any],
    headers: Mapping[str, str] | None = None,
) -> AsyncIterator[bytes]:
    """Anthropic /v1/messages 流式（对外别名路由包装）：首字节前失败可切组内下一候选"""
    alias = _alias_context(body, decision, headers)
    if alias is None:
        from .model_policy import classify_failure, push_fallback
        cfg = load_routing()
        _pinned_local = (decision or {}).get("action", "allow") in ("route_local", "block")
        mp_on = _mp_fallback_enabled()
        _prov0, _model0, _ = resolve(decision, {"model": str(body.get("model") or "")}, headers, kind="chat")
        cur_ctx = _mp_initial_ctx(_prov0.name, _model0) if mp_on else 0
        tried: list = []
        prov, model = _prov0, _model0
        while True:
            sent = False
            try:
                async for chunk in _messages_stream_once(body, decision, headers, prov=prov, model=model):
                    sent = True
                    yield chunk
                return
            except RoutingError as exc:
                if sent:
                    raise
                tried.append(f"{prov.name}/{model}")
                reason = classify_failure(exc.status_code, str(exc))
                nxt = (None if (not mp_on or _pinned_local) else
                       _mp_next(tried, reason, cur_ctx, cfg, upstream_key(headers, prov=prov)))
                if nxt is None:
                    raise
                push_fallback({"from": tried[-1], "to": f"{nxt[0].name}/{nxt[1]}",
                               "reason": reason, "error": str(exc)[:200]})
                prov, model, cur_ctx = nxt[0], nxt[1], nxt[2]
                continue
    from .alias_router import alias_candidates
    _alias_ctx_start(alias["name"])
    cands = alias_candidates(alias)
    if not cands:
        raise RoutingError(f"alias {alias['name']}: no available candidate (circuit open)", 503)
    last_exc: RoutingError | None = None
    cfg = load_routing()
    ukey = upstream_key(headers)
    for cand in cands:
        prov = cfg.get(cand["provider"]) or find_provider(cand["provider"])
        if prov is None or prov.kind != "chat":
            continue
        if not (ukey or prov.local):
            continue
        sent = False
        try:
            async for chunk in _messages_stream_once(body, decision, headers, prov=prov, model=cand["model"]):
                sent = True
                yield chunk
            return
        except RoutingError as exc:
            _alias_ctx_failover(f"{prov.name}/{cand['model']}")
            if sent:
                raise
            last_exc = exc
            continue
    if last_exc is not None:
        raise last_exc
    raise RoutingError(f"alias {alias['name']}: no usable candidate", 503)


async def _messages_stream_once(
    body: Dict[str, Any],
    decision: Dict[str, Any],
    headers: Mapping[str, str] | None = None,
    prov=None,
    model=None,
) -> AsyncIterator[bytes]:
    """Anthropic /v1/messages 流式：anthropic 上游 SSE 直通，openai 上游逐块转成 Anthropic 事件"""
    cfg = load_routing()
    if prov is None:
        prov, model, _ = resolve(decision, {"model": str(body.get("model") or "")}, headers, kind="chat")
    ukey = upstream_key(headers, prov=prov)

    if _mock_enabled(cfg, prov, bool(ukey)):
        for evt in _anthropic_stream_events(_mock_text(prov, model, decision), model):
            yield evt
        return
    _err = get_llm_handler(prov.api_mode).validate_environment(prov, ukey)
    if _err:
        raise RoutingError(f"provider {prov.name} 未配置密钥", 503,
                            type_="provider_not_configured")

    if prov.api_mode == "anthropic":
        out = {k: v for k, v in body.items() if k != "model"}
        out["model"] = model
        out["stream"] = True
        client = _get_client(prov.local)
        try:
            async with client.stream("POST", prov.endpoint("chat", model=model), headers=prov.headers(ukey),
                                     json=out, timeout=STREAM_TIMEOUT) as r:
                if r.status_code >= 400:
                    raise RoutingError(f"{prov.name} {r.status_code}", r.status_code)
                async for chunk in r.aiter_bytes():
                    yield chunk
        except httpx.HTTPError as exc:
            raise RoutingError(f"{prov.name} unreachable: {exc}", 502) from exc
        return

    client = _get_client(prov.local)
    obody = _anthropic_to_openai(body, model)
    obody["stream"] = True
    # If caller asked for thinking and upstream supports it, opt in
    if _wants_thinking(body):
        obody["include_reasoning"] = True
    text_acc: list[str] = []
    thinking_block_open = False
    text_block_open = False
    try:
        async with client.stream("POST", prov.endpoint("chat", model=model), headers=prov.headers(ukey),
                                 json=obody, timeout=STREAM_TIMEOUT) as r:
            if r.status_code >= 400:
                raise RoutingError(f"{prov.name} {r.status_code}: {r.text[:300]}", r.status_code)
            yield _sse({"type": "message_start", "message": {
                "id": "msg_gateway", "type": "message", "role": "assistant", "model": model,
                "content": [], "usage": {"input_tokens": 0, "output_tokens": 0}}})
            # don't pre-open text block - we'll open on first content delta
            # OR open immediately for clients that expect it
            yield _sse({"type": "content_block_start", "index": 0,
                        "content_block": {"type": "text", "text": ""}})
            text_block_open = True
            async for chunk in r.aiter_bytes():
                for delta in _parse_openai_sse_deltas(chunk):
                    if delta.get("reasoning"):
                        # open thinking block on first reasoning chunk
                        if not thinking_block_open:
                            yield _sse({"type": "content_block_start", "index": 1,
                                        "content_block": {"type": "thinking", "thinking": ""}})
                            thinking_block_open = True
                        # close text block if it was open at index 0
                        if text_block_open:
                            yield _sse({"type": "content_block_stop", "index": 0})
                            text_block_open = False
                        yield _sse({"type": "content_block_delta", "index": 1,
                                    "delta": {"type": "thinking_delta",
                                              "thinking": delta["reasoning"]}})
                    if delta.get("content"):
                        # ensure text block open
                        if not text_block_open:
                            # find next free index
                            yield _sse({"type": "content_block_start", "index": 0,
                                        "content_block": {"type": "text", "text": ""}})
                            text_block_open = True
                        text_acc.append(delta["content"])
                        yield _sse({"type": "content_block_delta", "index": 0,
                                    "delta": {"type": "text_delta", "text": delta["content"]}})
            full = "".join(text_acc)
            if text_block_open:
                yield _sse({"type": "content_block_stop", "index": 0})
            if thinking_block_open:
                yield _sse({"type": "content_block_stop", "index": 1})
            yield _sse({"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": max(1, len(full) // 3)}})
            yield _sse({"type": "message_stop"})
    except httpx.HTTPError as exc:
        raise RoutingError(f"{prov.name} unreachable: {exc}", 502) from exc


# ---------------- Responses API（Codex CLI / ccswitch wire_api="responses"）

async def route_responses(
    body: Dict[str, Any],
    decision: Dict[str, Any],
    headers: Mapping[str, str] | None = None,
) -> Dict[str, Any]:
    """
    Responses 非流式：
      - mock / 降级本地 -> 转成 chat.completions 调用再转回 Responses 格式
      - allow + 外网    -> 原样直通上游 /v1/responses，上游不支持(404)时回退 chat 转换
    """
    cfg = load_routing()
    _ar = _alias_resolve(body, decision, headers)
    if _ar is not None:
        prov, model, _alias = _ar
    else:
        prov, model, _ = resolve(decision, {"model": str(body.get("model") or "")}, headers, kind="chat")
    ukey = upstream_key(headers, prov=prov)
    # 上游只应看到**解析后**的模型名；原实现把客户端原始 body 原样转发，
    # 别名/前缀/用户自定义模型名会被原样送出（见模块下方 responses 端点注释）。
    body = {**body, "model": model}

    if _mock_enabled(cfg, prov, bool(ukey)):
        return _chat_to_responses(_mock_chat(prov, model, decision), model)
    _err = get_llm_handler(prov.api_mode).validate_environment(prov, ukey)
    if _err:
        raise RoutingError(_err, 503, type_="provider_not_configured")

    if prov.local or decision.get("action") == "route_local":
        # 本地/内部 provider 优先直通 /v1/responses（vLLM 原生支持，契约与 Codex 一致）；
        # 上游确实没有该端点（4xx/501）或不可达时才回退 chat 桥接。
        if _responses_direct_enabled():
            _r = None
            try:
                _r = await _post(prov, prov.endpoint("proxy", custom_path="responses"),
                                 body, "" if prov.local else ukey)
            except RoutingError:
                _r = None
            if _r is not None:
                if _r.status_code < 400:
                    return _r.json()
                if _r.status_code not in (400, 404, 405, 501):
                    raise RoutingError(
                        f"{prov.name} {_r.status_code}: {_r.text[:300]}", _r.status_code)
        obody = _responses_to_chat(body, model)
        return _chat_to_responses(await route_chat(obody, decision, headers), model)

    from .model_policy import classify_failure, push_fallback
    _pinned_local = (decision or {}).get("action", "allow") in ("route_local", "block")
    mp_on = _mp_fallback_enabled()
    cur_ctx = _mp_initial_ctx(prov.name, model) if mp_on else 0
    tried: list = []
    while True:
        ukey = upstream_key(headers, prov=prov)
        body = {**body, "model": model}
        r = await _post(prov, prov.endpoint("proxy", custom_path="responses"), body, ukey)
        if r.status_code in (404, 405, 501):
            obody = _responses_to_chat(body, model)
            return _chat_to_responses(await route_chat(obody, decision, headers), model)
        if r.status_code < 400:
            return r.json()
        exc = RoutingError(f"{prov.name} {r.status_code}: {r.text[:300]}", r.status_code)
        tried.append(f"{prov.name}/{model}")
        reason = classify_failure(exc.status_code, str(exc))
        nxt = (None if (not mp_on or _pinned_local) else
               _mp_next(tried, reason, cur_ctx, cfg, ukey))
        if nxt is None:
            raise exc
        push_fallback({"from": tried[-1], "to": f"{nxt[0].name}/{nxt[1]}",
                       "reason": reason, "error": str(exc)[:200]})
        prov, model, cur_ctx = nxt[0], nxt[1], nxt[2]


async def _responses_from_chat_stream(
    body: Dict[str, Any],
    model: str,
    decision: Dict[str, Any],
    headers: Mapping[str, str] | None = None,
    prov=None,
) -> AsyncIterator[bytes]:
    """chat.completions 流 -> Responses SSE 完整事件序列（本地模型与外网回退共用）。

    prov 非空时钉住该 provider（外网 /v1/responses 不被支持时的同源回退），
    否则按别名/决策正常路由。
    """
    obody = _responses_to_chat(body, model)
    # codex-rs 按 response id 区分会话轮次：静态 id 会让同会话所有轮次共用一个
    # id，UI 无法收尾/复制/分支（表现为"像被中断"）。每次请求生成唯一 id。
    asm = _ResponsesSSEAssembler(model, resp_id="resp_" + uuid.uuid4().hex)
    yield asm.created()
    if _wants_thinking(body):
        obody["include_reasoning"] = True
    if _chat_usage_enabled():
        # usage 注入：chat 流式默认不给 usage，隔着 Assembler 的 response.completed
        # 里 usage 恒为 0（客户端 token 统计失效）。
        _so = obody.get("stream_options")
        if not isinstance(_so, dict):
            _so = {}
        _so["include_usage"] = True
        obody["stream_options"] = _so
    if prov is not None:
        source = _chat_stream_once(obody, decision, headers, prov=prov, model=model)
    else:
        source = route_chat_stream(obody, decision, headers)
    _usage: Dict[str, Any] = {}
    async for chunk in source:
        for delta in _parse_openai_sse_deltas(chunk):
            if delta.get("usage"):
                _usage = delta["usage"]
            if delta.get("reasoning"):
                for evt in asm.reasoning_delta(delta["reasoning"]):
                    yield evt
            if delta.get("content"):
                for evt in asm.text_delta(delta["content"]):
                    yield evt
            if delta.get("tool_calls"):
                for evt in asm.tool_call_delta(delta["tool_calls"]):
                    yield evt
    for evt in asm.finish(_usage or None):
        yield evt


async def route_responses_stream(
    body: Dict[str, Any],
    decision: Dict[str, Any],
    headers: Mapping[str, str] | None = None,
) -> AsyncIterator[bytes]:
    """Responses 流式：mock/降级 -> 合成事件；allow -> 上游 SSE 直通"""
    cfg = load_routing()
    _ar = _alias_resolve(body, decision, headers)
    if _ar is not None:
        prov, model, _alias = _ar
    else:
        prov, model, _ = resolve(decision, {"model": str(body.get("model") or "")}, headers, kind="chat")
    ukey = upstream_key(headers, prov=prov)
    body = {**body, "model": model}

    if _mock_enabled(cfg, prov, bool(ukey)):
        resp = _chat_to_responses(_mock_chat(prov, model, decision), model)
        for evt in _responses_stream_events(resp["output_text"], model, resp):
            yield evt
        return

    if prov.local or decision.get("action") == "route_local":
        # 同非流式：本地优先直通 /v1/responses；不支持的场景回退 chat 桥接（兜底不变）。
        if _responses_direct_enabled():
            try:
                async for chunk in _responses_direct_stream(
                        prov, body, "" if prov.local else ukey,
                        fallback_codes=(400, 404, 405, 501)):
                    yield chunk
                return
            except _ResponsesUnsupported:
                pass
        async for evt in _responses_from_chat_stream(body, model, decision, headers):
            yield evt
        return

    from .model_policy import classify_failure, push_fallback
    _pinned_local = (decision or {}).get("action", "allow") in ("route_local", "block")
    mp_on = _mp_fallback_enabled()
    cur_ctx = _mp_initial_ctx(prov.name, model) if mp_on else 0
    tried: list = []
    while True:
        ukey = upstream_key(headers, prov=prov)
        body = {**body, "model": model}
        sent = False
        try:
            try:
                async for chunk in _responses_direct_stream(
                        prov, body, ukey, fallback_codes=(400, 404, 405, 501)):
                    sent = True
                    yield chunk
                return
            except _ResponsesUnsupported:
                # 上游没有 /v1/responses：同 provider 转 chat.completions，再转回 responses 事件
                # （否则直接 4xx -> main.py 的首块降级会把该 provider 的流量全部甩给本地模型）
                async for evt in _responses_from_chat_stream(body, model, decision, headers, prov=prov):
                    yield evt
                return
        except RoutingError as exc:
            if sent:
                raise
            tried.append(f"{prov.name}/{model}")
            reason = classify_failure(exc.status_code, str(exc))
            nxt = (None if (not mp_on or _pinned_local) else
                   _mp_next(tried, reason, cur_ctx, cfg, ukey))
            if nxt is None:
                raise
            push_fallback({"from": tried[-1], "to": f"{nxt[0].name}/{nxt[1]}",
                           "reason": reason, "error": str(exc)[:200]})
            prov, model, cur_ctx = nxt[0], nxt[1], nxt[2]
            continue
