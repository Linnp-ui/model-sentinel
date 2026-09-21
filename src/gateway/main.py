"""

Gateway 主入口 — 公司内网大模型请求的统一出口

src/gateway/main.py



请求链路：审查（L1 规则 -> L2 小模型） -> 通过则发外网 provider

        不通过则自动降级到本地模型（内容不出境，调用方无感知）

"""

from __future__ import annotations



import hashlib

import hmac

import html

import json

import re

import os

import asyncio

import time

import random

import uuid

from datetime import datetime, timedelta

from urllib.parse import parse_qs, quote

from contextvars import ContextVar
from typing import Any, Optional



import httpx

from fastapi import FastAPI, Header, UploadFile, File, Depends, HTTPException, Request

from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse, Response, StreamingResponse

from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, ConfigDict

from starlette.concurrency import run_in_threadpool



from .inspection import inspect_text

from .file_inspector import inspect_file, inspect_inline_bytes, collect_inline_blobs_chat, collect_inline_blobs_anthropic, collect_inline_blobs_responses

from .policy import load_policy, decide

from .providers import load_routing

from .routing import (

    aclose_clients,

    anthropic_text,

    get_proxy_client,

    responses_text,

    resolve,

    route_chat,

    route_chat_stream,

    route_embeddings,

    route_messages,

    route_messages_stream,

    route_responses,

    route_responses_stream,

    RoutingError,
    upstream_key,
    _DEFAULT_MAX_TOKENS,

)

from .small_model import classify as small_classify, classify_chunks, is_confidential, is_degraded

from .user_models import UserModel, get_store, validate

from .audit_store import get_audit_store, AuditStore

from .session_store import get_session_store, extract_session_id

from .metrics import get_current_status, get_metrics, set_current_status, GATEWAY_VERSION
from .redaction import redact_audit_entry
from .responses_filectx import extract_responses_filectx, responses_should_mark

from .stat_scope import is_blocked, is_local_route, resolve_action, to_local_naive

from .rate_limit import get_limiter, extract_client_key as _rl_key
from .identity import (
    set_current_identity as _set_current_identity,
    Identity,
    identity_v2_enabled as _identity_v2,
    local_identity as _local_identity,
    identity_from_entry as _identity_from_entry,
)


# 审计归因：当前请求的调用方 key（由 _admin_ip_filter_mw 在 /v1/* 写入）。
# 中间件是 async，set 后在同一请求 task 的 handler/log_entry 内可见。
_CURRENT_CLIENT_KEY: ContextVar[str] = ContextVar("gw_client_key", default="")
# 审计归因：当前请求文本的风险分（_review_text / 各 inspect_text 调用点写入，log_entry 自动补 risk_score）。
_CURRENT_RISK: ContextVar = ContextVar("gw_risk_score", default=None)
_CURRENT_PII_SUMMARY: ContextVar = ContextVar("gw_pii_summary", default=None)


def _pii_audit_summary(tf) -> dict | None:
    """inspect findings -> audit PII summary (type counts + keywords only).

    Deliberately NO value_preview / matched raw text: audit lands in Redis/MySQL,
    raw hits would widen exposure; triage only needs which-pattern-fired-how-often.
    Any malformed input returns None and never raises (audit path must not blow up).
    """
    try:
        tf = tf or {}
        by_type = dict(tf.get("by_type") or {})
        if not by_type and isinstance(tf.get("findings"), list):
            for f in tf["findings"]:
                if isinstance(f, dict) and f.get("type"):
                    by_type[f["type"]] = by_type.get(f["type"], 0) + 1
        kw = [k for k in (tf.get("keyword_hits") or []) if isinstance(k, str)]
        if not by_type and not kw:
            return None
        return {
            "by_type": by_type,
            "keyword_hits": kw,
            "weighted": int(tf.get("weighted_pii_score") or 0),
            "keyword_score": int(tf.get("keyword_score") or 0),
        }
    except Exception:
        return None




app = FastAPI(title="Secure Gateway — 大模型请求统一审查出口", version="0.3.0")


# P2: middleware 抓 HTTP 响应码, set_current_status() 写到 ContextVar;
# log_entry() 读出来当 gateway_requests_v2_total{status_code="..."}.
# 每次请求一个 dict 赋值, 可忽略不计。


@app.middleware("http")
async def _capture_response_status(request, call_next):
    set_current_status(0)
    _v1 = request.url.path.startswith("/v1/")
    if _v1:
        get_metrics().begin_request()
    try:
        response = await call_next(request)
    finally:
        if _v1:
            get_metrics().end_request()
    set_current_status(response.status_code)
    return response



@app.on_event("startup")
async def _ring_startup():
    from .metrics_ring import get_ring
    get_ring().start()

@app.on_event("startup")
async def _l2_overrides_startup():
    # T46: 应用 l2_overrides.yaml 持久化覆盖（首个请求前；键白名单见 l2_overrides.py）
    from src.gateway.l2_overrides import apply_overrides

    apply_overrides()


@app.on_event("shutdown")

async def _shutdown():

    await aclose_clients()



# 内存审计（postgres 接入后可落库）

# AUDIT_LOG delegated to AuditStore (Redis or in-memory)





def _bind_identity(request, identity, token):
    """把身份挂到 ``request.state``（同一 request 对象，跨 task 稳定）。

    不用 ContextVar：BaseHTTPMiddleware 下中间件与 handler 分属不同 context，
    只有「中间件 → handler」单向继承可见，set 的可见性容易咬人（已记录同类坑）。
    ``AI_GATEWAY_IDENTITY_V2=false`` ⇒ 不注入并退回裸 token（旧行为，便于秒回滚）。
    """
    if not _identity_v2():
        return token
    try:
        if request is not None:
            request.state.gw_identity = identity
    except Exception:
        pass
    # 身份**不在这里**写进 ContextVar（T45 修正）：``auth`` 是同步 def，FastAPI 对
    # 同步依赖走 ``run_in_threadpool``（anyio ``copy_context()`` 出副本）⇒ 此处 set
    # 会随副本一起被丢弃，handler 侧 ``current_identity()`` 恒为 None。写入点见
    # ``_bind_identity_ctx()``（async 依赖，在同一 context 内 set），**只此一处**。
    return identity


def auth(authorization: Optional[str] = Header(None), x_api_key: Optional[str] = Header(None, alias="x-api-key"), request: Request = None):
    """鉴权：token 必须在登记表（未停用）中，否则 401。2026-09-20 收紧：env dev key / 上游验签兜底已删。

    P0（客户端 key 身份化）：返回值由**裸 token 改为** Identity 对象。
    限流分桶与涉密绑定一律取 ``identity.bind_key``（口径跟随身份记录 —— 换凭据
    不丢桶、不丢 30 分钟涉密标记）；**不要再把返回值当凭据字符串使用**。
    身份同时写入 ``request.state.gw_identity`` 供下游消费点读取。
    回滚开关 ``AI_GATEWAY_IDENTITY_V2=false`` ⇒ 退回「返回裸 token」旧行为。
    """
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1].strip()
    elif x_api_key:
        token = x_api_key.strip()
    if not token:
        raise HTTPException(401, "missing api key")
    if request is not None:
        _memo = getattr(getattr(request, "state", None), "gw_key_entry", None)
        if _memo and _memo[1]:  # P1: 中间件已命中登记表，跳过重复 DB
            return _bind_identity(request, _identity_from_entry(token, _memo), token)
    try:
        from src.gateway.admin_store import get_admin_store
        store = get_admin_store()
        _kid, _name, _via_fuzzy = store.find_key_entry(token)
        if _name:
            if _via_fuzzy:
                try:
                    store.promote_key(_kid, token)
                    store.note_promoted(token, (_kid, _name, False))  # P1
                except Exception:
                    pass
            return _bind_identity(request, _identity_from_entry(token, (_kid, _name, False)), token)
    except Exception:
        pass
    # 未登记一律 401：env dev key 与上游验签（fail-open）兜底已删。
    # 迁移中的合法 key 先登记进表（admin UI / create_api_key），再收紧不断流。
    raise HTTPException(401, "invalid api key")

async def _bind_identity_ctx(identity: Identity = Depends(auth)) -> Identity:
    """把身份写进**事件循环这一侧**的 ContextVar（routing 层准入门禁要读它）。

    为什么不能写在 ``auth()`` / ``_bind_identity()`` 里：``auth`` 是同步 ``def``，
    FastAPI 对同步依赖走 ``run_in_threadpool``（anyio 会 ``copy_context()`` 出副本）
    ⇒ 那里 ``set`` 的 ContextVar 随副本一起被丢弃，handler 侧 ``current_identity()``
    恒为 ``None``。生产实测（2026-09-17，切 ``gateway_only`` 后）：**已登记身份全被
    判成「无权使用网关凭据」** ⇒ 非流式 503；流式被 main.py「首字节前 status>=500
    透明降级本地」吞掉，控制台仍显示 ``deepseek/200``（真实由 vllm_local 服务）。

    本依赖是 ``async def``，FastAPI 在请求的事件循环上下文里 ``await`` 它，``set``
    才留在当前 task 的 context 上，向内层 ``routing``（纯函数层）可见。

    ⚠️ ``/v1/*`` 端点一律用 ``Depends(_bind_identity_ctx)``，**不要**再用
    ``Depends(auth)`` —— 少这一层就没有准入身份（fail-closed 成全量 503）。
    """
    try:
        if _identity_v2() and isinstance(identity, Identity):
            _set_current_identity(identity)
    except Exception:
        pass
    return identity

def _rate_limit_chat(request: Request, identity: Identity = Depends(_bind_identity_ctx)) -> Identity:

    """/v1/chat/* 限流；返回调用方身份（供下游归因）。"""

    rpm = int(os.getenv("AI_GATEWAY_RATE_LIMIT_RPM", "0") or 0)

    rps = int(os.getenv("AI_GATEWAY_RATE_LIMIT_RPS", "0") or 0)

    if rpm <= 0 and rps <= 0:

        return identity

    authz = request.headers.get("authorization", "")

    xak = request.headers.get("x-api-key", "")

    client_ip = _resolve_client_ip(request)

    key = _rl_key(authz, xak, client_ip, identity_key=_identity_bind_key(request))

    ok, retry, reason = get_limiter().check(key, scope="chat")

    if not ok:

        get_metrics().inc_rate_limited("chat", reason)

        raise HTTPException(

            status_code=429,

            detail={"type": "rate_limited", "scope": "chat", "reason": reason, "retry_after": retry},

            headers={"Retry-After": str(retry)},

        )

    return identity





def _rate_limit_files(request: Request, identity: Identity = Depends(_bind_identity_ctx)) -> Identity:

    """文件预检限流（独立桶，更严）。"""

    rpm = int(os.getenv("AI_GATEWAY_RATE_LIMIT_FILES_RPM", "0") or 0)

    if rpm <= 0:

        return identity

    authz = request.headers.get("authorization", "")

    xak = request.headers.get("x-api-key", "")

    client_ip = _resolve_client_ip(request)

    key = _rl_key(authz, xak, client_ip, identity_key=_identity_bind_key(request))

    ok, retry, reason = get_limiter().check(key, scope="files")

    if not ok:

        get_metrics().inc_rate_limited("files", reason)

        raise HTTPException(

            status_code=429,

            detail={"type": "rate_limited", "scope": "files", "reason": reason, "retry_after": retry},

            headers={"Retry-After": str(retry)},

        )

    return identity





def _rate_limit_embed(request: Request, identity: Identity = Depends(_bind_identity_ctx)) -> Identity:

    rpm = int(os.getenv("AI_GATEWAY_RATE_LIMIT_RPM", "0") or 0)

    rps = int(os.getenv("AI_GATEWAY_RATE_LIMIT_RPS", "0") or 0)

    if rpm <= 0 and rps <= 0:

        return identity

    authz = request.headers.get("authorization", "")

    xak = request.headers.get("x-api-key", "")

    client_ip = _resolve_client_ip(request)

    key = _rl_key(authz, xak, client_ip, identity_key=_identity_bind_key(request))

    ok, retry, reason = get_limiter().check(key, scope="embed")

    if not ok:

        get_metrics().inc_rate_limited("embed", reason)

        raise HTTPException(

            status_code=429,

            detail={"type": "rate_limited", "scope": "embed", "reason": reason, "retry_after": retry},

            headers={"Retry-After": str(retry)},

        )

    return identity





def _rate_limit_messages(request: Request, identity: Identity = Depends(_bind_identity_ctx)) -> Identity:

    rpm = int(os.getenv("AI_GATEWAY_RATE_LIMIT_RPM", "0") or 0)

    rps = int(os.getenv("AI_GATEWAY_RATE_LIMIT_RPS", "0") or 0)

    if rpm <= 0 and rps <= 0:

        return identity

    authz = request.headers.get("authorization", "")

    xak = request.headers.get("x-api-key", "")

    client_ip = _resolve_client_ip(request)

    key = _rl_key(authz, xak, client_ip, identity_key=_identity_bind_key(request))

    ok, retry, reason = get_limiter().check(key, scope="messages")

    if not ok:

        get_metrics().inc_rate_limited("messages", reason)

        raise HTTPException(

            status_code=429,

            detail={"type": "rate_limited", "scope": "messages", "reason": reason, "retry_after": retry},

            headers={"Retry-After": str(retry)},

        )

    return identity









def _parse_admin_allowlist() -> set[str]:

    raw = os.getenv("AI_GATEWAY_ADMIN_IP_ALLOWLIST", "127.0.0.1,::1")

    return {x.strip() for x in raw.split(",") if x.strip()}






# ---------- /admin 登录（cookie 会话，AI_GATEWAY_ADMIN_PASSWORD 启用） ----------

_ADMIN_COOKIE = "gw_admin_session"
_ADMIN_SESSION_TTL_S = 7 * 24 * 3600   # 登录态 7 天
_ADMIN_LOGIN_MAX_FAILS = 5         # 每 IP 连续失败 5 次
_ADMIN_LOGIN_WINDOW_S = 300        # 5 分钟窗口内

_admin_login_fails: dict = {}


def _admin_password() -> str:
    return (os.getenv("AI_GATEWAY_ADMIN_PASSWORD", "") or "").strip()


def _admin_secret() -> str:
    # cookie 签名密钥：优先 AI_GATEWAY_ADMIN_SESSION_SECRET；
    # 缺省从密码派生 —— 改密码即全量下线，天然换签。
    _ov = _admin_secret_override()
    if _ov:
        return _ov
    return (os.getenv("AI_GATEWAY_ADMIN_SESSION_SECRET", "") or "").strip() or ("gw-admin-v1:" + _admin_password())


def _admin_session_token() -> str:
    expiry = int(time.time()) + _ADMIN_SESSION_TTL_S
    mac = hmac.new(_admin_secret().encode("utf-8"), str(expiry).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{expiry}.{mac}"


def _admin_session_valid(request: Request) -> bool:
    token = request.cookies.get(_ADMIN_COOKIE, "") or ""
    expiry_s, _, mac = token.rpartition(".")
    if not expiry_s.isdigit() or not mac:
        return False
    if int(expiry_s) < int(time.time()):
        return False
    expected = hmac.new(_admin_secret().encode("utf-8"), expiry_s.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, expected)


def _admin_login_throttled(ip: str) -> bool:
    rec = _admin_login_fails.get(ip)
    if not rec:
        return False
    count, ts = rec
    if time.time() - ts > _ADMIN_LOGIN_WINDOW_S:
        _admin_login_fails.pop(ip, None)
        return False
    return count >= _ADMIN_LOGIN_MAX_FAILS


def _admin_login_fail(ip: str) -> None:
    rec = _admin_login_fails.get(ip)
    now = time.time()
    if rec and now - rec[1] <= _ADMIN_LOGIN_WINDOW_S:
        _admin_login_fails[ip] = (rec[0] + 1, rec[1])
    else:
        _admin_login_fails[ip] = (1, now)


def _safe_next(nxt: Optional[str]) -> str:
    # 只允许站内相对路径，防 open redirect
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return "/admin/app"


def admin_auth(request: Request) -> str:
    """Restrict /admin* endpoints: login cookie first, then IP allowlist.

    设置了 AI_GATEWAY_ADMIN_PASSWORD 时：/admin* 需要有效登录 cookie，
    本机 127.*/::1 仍豁免（健康检查 + 本地运维脚本）。
    未设置密码时：保持原有 IP allowlist 行为（默认 127/::1/内网放行，
    AI_GATEWAY_ADMIN_STRICT_LAN=true 可收紧内网）。
    """
    client_ip = (request.client.host if request.client else "") or ""
    if _admin_password():
        if _admin_session_valid(request):
            return client_ip or "session"
        if client_ip.startswith("127.") or client_ip == "::1":
            return client_ip
        raise HTTPException(401, "admin login required")
    if client_ip in _parse_admin_allowlist():
        return client_ip
    if client_ip.startswith("127.") or client_ip == "::1":
        return client_ip
    # P1-3: LAN恒放行是便利默认；生产收紧时设 AI_GATEWAY_ADMIN_STRICT_LAN=true，
    # 则 10./192.168./169.254. 也必须显式列在 AI_GATEWAY_ADMIN_IP_ALLOWLIST 里。
    # 默认 false = 保持现有行为（办公网 dashboard 不破）。
    strict_lan = os.getenv("AI_GATEWAY_ADMIN_STRICT_LAN", "false").lower() in ("1", "true", "yes", "on")
    if not strict_lan and (client_ip.startswith("10.") or client_ip.startswith("192.168.") or client_ip.startswith("169.254.")):
        return client_ip
    raise HTTPException(403, f"admin endpoint restricted (client={client_ip})")


def admin_page_auth(request: Request) -> str:
    """HTML 面板专用依赖：未登录 302 到登录页，而不是 401 JSON。"""
    client_ip = (request.client.host if request.client else "") or ""
    if _admin_password():
        if _admin_session_valid(request):
            return client_ip or "session"
        if client_ip.startswith("127.") or client_ip == "::1":
            return client_ip
        raise HTTPException(302, "admin login required", headers={"Location": "/admin/login"})
    return admin_auth(request)








def _resolve_client_ip(request) -> str:
    """P1-2: canonical client IP for rate limiting and audit.

    Socket IP wins (unspoofable). X-Forwarded-For is attacker-controlled and
    is only honored when the gateway sits behind a trusted reverse proxy
    (AI_GATEWAY_TRUST_XFF=true). Last-resort XFF fallback exists only for
    harnesses where request.client is None (e.g. some TestClient setups).
    """
    sock = ""
    try:
        if request is not None and request.client:
            sock = (request.client.host or "").strip()
    except Exception:
        sock = ""
    trust_xff = os.getenv("AI_GATEWAY_TRUST_XFF", "false").lower() in ("1", "true", "yes", "on")
    if trust_xff:
        try:
            xff = (request.headers.get("x-forwarded-for", "") if request is not None else "").split(",")[0].strip()
            if xff:
                return xff[:64]
        except Exception:
            pass
    if sock:
        return sock[:64]
    try:
        xff = (request.headers.get("x-forwarded-for", "") if request is not None else "").split(",")[0].strip()
        return xff[:64]
    except Exception:
        return ""


def _identity_bind_key(request) -> str:
    """P0：当前请求的身份绑定键（限流分桶 / 涉密标记**共用**同一口径）。

    ``AI_GATEWAY_IDENTITY_V2=false`` 或尚无身份时返回空串，调用方回落旧口径。
    """
    if not _identity_v2():
        return ""
    try:
        ident = getattr(getattr(request, "state", None), "gw_identity", None)
    except Exception:
        ident = None
    if isinstance(ident, Identity):
        return ident.bind_key
    return ""


def _is_session_confidential(session_id) -> bool:
    """P1: True if the session was marked confidential (sticky route_local).

    （P1-1 的 client 身份标记已于 2026-09-18 整体移除：与黑名单建议引擎的
    route_local 计数形成误报回路，上线前决定不启用；只保留 session 级标记。）
    """
    store = get_session_store()
    return bool(session_id and store.is_confidential(session_id))


_MENTIONED_FILE_RE = None
def _mentioned_data_filename(text):
    """workbuddy 风格：正文里提及的数据文件名（文件路径/附件名+粘贴内容，无字节附件）。

    有文件名才算文件参与；纯闲聊短文本无文件名，命中也不标。
    """
    global _MENTIONED_FILE_RE
    try:
        import re as _re
        if _MENTIONED_FILE_RE is None:
            _MENTIONED_FILE_RE = _re.compile(r"[A-Za-z0-9_\-\u4e00-\u9fa5~]+\.(?:csv|tsv|txt|xlsx|xls|pdf|docx?)", _re.IGNORECASE)
        m = _MENTIONED_FILE_RE.search(text or "")
        return m.group(0) if m else ""
    except Exception:
        return ""


def _mark_session_hit(session_id, decision, name=""):
    """命中（route_local/block）即标 session，后续同会话粘本地；与 files/check 同口径。

    沿用 responses 既有约定：只有文件参与（name 非空）才标记，纯文本命中不标。
    标记失败绝不影响主链路。
    """
    try:
        if session_id and name and (decision or {}).get("action") in ("route_local", "block"):
            get_session_store().mark_confidential(
                session_id,
                {"name": name, "rule": (decision or {}).get("name"), "action": (decision or {}).get("action")},
            )
    except Exception:
        pass





class ChatMessage(BaseModel):

    model_config = ConfigDict(extra="allow")

    role: str

    content: Any = None  # str or list[{type:text}] or None for tool_calls

    tool_calls: Any = None

    tool_call_id: Any = None

def _clip_audit(s: Any, n: int) -> str:
    if s is None:
        return ""
    return str(s)[:n]

def _clip_audit_url(url: Any) -> str:
    u = str(url or "")
    return u[:200] if u.startswith("data:") else u[:2000]

def _hdr_safe(v: Any) -> str:
    """HTTP 头值必须 latin-1 可编码（RFC 7230 / starlette init_headers）。

    规则名可能是中文（policy.yaml 里的业务规则，如 `AB文件`），直接塞进 header 会让
    StreamingResponse 在构造时抛 UnicodeEncodeError，表现为**整个请求 500**
    （2026-09-15 Codex CLI 连不上的根因）。这里对非 ASCII 值降级为 UTF-8 百分号编码：
    ASCII 安全、可逆、客户端可用 urllib.parse.unquote 还原。
    """
    s = "" if v is None else str(v)
    try:
        s.encode("latin-1")
        return s
    except UnicodeEncodeError:
        return quote(s, safe="")
def _chat_part_ref(p: Any) -> str:
    """chat content part 的可审查文本(P1)：text 全收；image/file 引用限长收。

    input_audio 的字节无法文本审查（残留，见测试），可审的只有转写后的文本。
    未知 part 兜底收 filename/file_id，防新字段绕过。
    """
    if p is None:
        return ""
    if isinstance(p, str):
        return p
    if not isinstance(p, dict):
        return ""
    t = p.get("type", "")
    if t == "text" or "text" in p:
        return str(p.get("text") or "")
    if t in ("image_url", "image", "input_image"):
        iu = p.get("image_url", "")
        url = iu.get("url", "") if isinstance(iu, dict) else iu
        if not url:
            url = p.get("url", "")
        return _clip_audit_url(url)
    if t in ("file", "input_file"):
        f = p.get("file", {})
        if isinstance(f, dict):
            return " ".join(_clip_audit(f.get(k, ""), 500) for k in ("filename", "file_id")).strip()
        for k in ("filename", "file_id"):
            if p.get(k):
                return _clip_audit(p[k], 500)
        return ""
    if t in ("input_audio", "audio"):
        return ""
    for k in ("filename", "file_id"):
        if isinstance(p.get(k), str) and p[k]:
            return _clip_audit(p[k], 500)
    return ""

def _msg_content_to_str(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(filter(None, (_chat_part_ref(x) for x in content)))
    return str(content) if content is not None else ""

def _tool_calls_text(tool_calls: Any) -> str:
    """message 级 tool_calls 参数送审(P1)：function name + arguments(限长)。"""
    if not isinstance(tool_calls, list):
        return ""
    parts = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        if not isinstance(fn, dict):
            continue
        if fn.get("name"):
            parts.append(_clip_audit(fn["name"], 200))
        if fn.get("arguments"):
            parts.append(_clip_audit(fn["arguments"], 4000))
    return " ".join(parts)

def _last_user_text_chat(messages) -> str:
    """最后一条人类 user 消息全文（跳过 tool 调用/结果）。"""
    try:
        for m in reversed(messages or []):
            d = m if isinstance(m, dict) else None
            role = (d.get("role") if d else getattr(m, "role", ""))
            if role != "user":
                continue
            if d:
                if d.get("tool_calls") or d.get("tool_call_id"):
                    continue
                c = d.get("content")
            else:
                if getattr(m, "tool_calls", None) or getattr(m, "tool_call_id", None):
                    continue
                c = getattr(m, "content", "")
            s = _msg_content_to_str(c).strip()
            if s:
                return s
    except Exception:
        pass
    return ""


def _last_user_text_anthropic(body) -> str:
    """最后一条纯文本 user 消息全文（跳过 tool_result）。"""
    try:
        msgs = (body.get("messages") or []) if isinstance(body, dict) else []
        for m in reversed(msgs):
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                return c.strip()
            if isinstance(c, list):
                ts = [b.get("text", "") for b in c
                      if isinstance(b, dict) and b.get("type") in ("text", "input_text") and b.get("text")]
                s = " ".join(ts).strip()
                if s:
                    return s
    except Exception:
        pass
    return ""


def _audit_preview_anthropic(body, text: str) -> str:
    s = _last_user_text_anthropic(body)
    return (s or (text or ""))[:200]


def _last_user_text_responses(body) -> str:
    """最后一条 user input 全文（跳过 function_call/output）。"""
    try:
        inp = body.get("input") if isinstance(body, dict) else None
        if isinstance(inp, str) and inp.strip():
            return inp.strip()
        if isinstance(inp, list):
            for item in reversed(inp):
                if not isinstance(item, dict):
                    continue
                if item.get("type") in ("function_call_output", "function_call"):
                    continue
                c = item.get("content")
                if isinstance(c, str) and c.strip():
                    return c.strip()
                if isinstance(c, list):
                    ts = [b.get("text", "") for b in c
                          if isinstance(b, dict) and b.get("type") in ("input_text", "text") and b.get("text")]
                    s = " ".join(ts).strip()
                    if s:
                        return s
    except Exception:
        pass
    return ""


def _audit_preview_responses(body, text: str) -> str:
    s = _last_user_text_responses(body)
    return (s or (text or ""))[:200]


def _l2_source_text(kind: str, payload, merged_text: str) -> str:
    """未截断的 L2 源文本 = 用户 query + 内联 OCR 文本。

    `_select_l2_text` 只是它的 `[:1200]` 视图；`tail` scope 从同一个源取第 2 段 ——
    共用同一个源，避免「同一语义两份实现」（MEMORY §2 纪律）。
    """
    q = ""
    try:
        if kind == "chat":
            q = _last_user_text_chat(payload)
        elif kind == "anthropic":
            q = _last_user_text_anthropic(payload)
        elif kind == "responses":
            q = _last_user_text_responses(payload)
    except Exception:
        q = ""
    try:
        ocr = " ".join(re.findall(r"\[INLINE_MEDIA \d+\]\n(.*?)\n\[/INLINE_MEDIA\]",
                                 merged_text or "", re.S)).strip()
    except Exception:
        ocr = ""
    return (q.strip() + "\n" + ocr).strip()


def _select_l2_text(kind: str, payload, merged_text: str) -> str:
    """L2 只吃用户 query + 内联 OCR 文本（≤1200 字），不喂 system prompt。
    全文前 1200 字喂小模型等于让它审提示词，query 才有判决意义。抽不到回退全文。"""
    focused = _l2_source_text(kind, payload, merged_text)[:1200]
    return focused or (merged_text or "")[:1200]


def _tool_text_chat(messages) -> str:
    """最后一条 role=tool 的结果正文 —— RAG 召回的内部文档，第三方内容进上下文的主通路。"""
    try:
        for m in reversed(messages or []):
            d = m if isinstance(m, dict) else None
            role = (d.get("role") if d else getattr(m, "role", ""))
            if role != "tool":
                continue
            c = d.get("content") if d else getattr(m, "content", "")
            s = _msg_content_to_str(c).strip()
            if s:
                return s
    except Exception:
        pass
    return ""


def _tool_text_anthropic(body) -> str:
    """messages 端点把工具结果伪装成 role=user + type=tool_result 的块。"""
    try:
        msgs = (body.get("messages") or []) if isinstance(body, dict) else []
        for m in reversed(msgs):
            if not isinstance(m, dict):
                continue
            c = m.get("content")
            if not isinstance(c, list):
                continue
            for b in reversed(c):
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    s = _msg_content_to_str(b.get("content")).strip()
                    if s:
                        return s
    except Exception:
        pass
    return ""


def _tool_text_responses(body) -> str:
    """responses 端点的 function_call_output.output。"""
    try:
        inp = body.get("input") if isinstance(body, dict) else None
        if isinstance(inp, list):
            for item in reversed(inp):
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "function_call_output":
                    s = _msg_content_to_str(item.get("output")).strip()
                    if s:
                        return s
    except Exception:
        pass
    return ""


# ---- L2 scope 名（docs/l2-scope-extension-plan-2026-09-16.md §3.1）----
_L2_SCOPE_LAST_USER = "last_user"
_L2_SCOPE_TOOL = "tool"
_L2_SCOPE_TAIL = "tail"
# 明确排除的 scope：system 见 §3.8，history 见 §3.7（它是判决失忆，不是视野盲区）。
# 写进配置只留痕、不产块 —— 防止有人以为「配上就生效」。
_L2_SCOPE_EXCLUDED = ("system", "developer", "instructions", "history")
_L2_SCOPE_MAX_BLOCKS = 5      # = classify_chunks 的 chunks[:5] 硬上限
# 灰区强制 L2 下限（env 可调）：risk_score ∈ [FLOOR, 60) 且 L1 放行的文本，
# 无论长短都送 L2 语义复核（短句语义泄密是 L1 格式规则的真空带）；≥60 已转本地，无需 L2。
_L2_GRAY_FLOOR = int((os.getenv("AI_GATEWAY_L2_GRAY_FLOOR", "20") or 20).strip() or 20)


def _l2_gray_trigger(findings) -> bool:
    """灰区命中即 True（供 _review_text 门与 route-inspect 干跑共用，保持口径一致）。"""
    try:
        from .inspection import RISK_THRESHOLD_ROUTE_LOCAL
        s = int((findings or {}).get("risk_score") or 0)
        return _L2_GRAY_FLOOR <= s < RISK_THRESHOLD_ROUTE_LOCAL
    except Exception:
        return False


def _l2_scope_warn(msg: str) -> None:
    """scope 配置/容量类告警。绝不抛异常：L2 视野是增强，不能反过来打断请求。"""
    try:
        import logging
        logging.getLogger("gateway").warning("l2_scope: %s", msg)
    except Exception:
        pass


def _l2_scope_cfg() -> dict:
    """review_model 的 scope 配置（mtime 热重载 ⇒ 调档/降采样不需要重启）。

    异常一律回退到「今天的行为」（只审 last_user），**并留痕** ——
    静默降级必须留痕（MEMORY §1 贯穿性判断）。
    """
    d = {"scopes": [_L2_SCOPE_LAST_USER], "chunk_chars": 1200,
         "budget_chars": 5000}
    try:
        from .model_policy import (review_budget_chars, review_chunk_chars,
                                   review_scopes)
        d["scopes"] = review_scopes()
        d["chunk_chars"] = review_chunk_chars()
        d["budget_chars"] = review_budget_chars()
    except Exception as e:
        _l2_scope_warn(f"scope 配置读取失败，回退默认档（只审 last_user）: {e}")
    return d


def _select_l2_scopes(kind: str, payload, merged_text: str) -> list:
    """按 scope 名切 L2 视野块，返回 [(scope, text), ...]。

    - 空块不返回（少了就不发这次调用）。
    - `last_user` 走 `_select_l2_text` ⇒ **与历史行为逐字节一致**（默认档唯一成员）。
    - `tool` = 最后一条工具结果；`tail` = 源文本 `[chunk_chars : 2*chunk_chars]`（长粘贴尾部）。
    - 预算先到先用；`last_user` 不受预算裁剪（它是判定基线，不能被配置饿死）。
    """
    cfg = _l2_scope_cfg()
    names = list(cfg["scopes"] or [_L2_SCOPE_LAST_USER])
    cc = max(1, int(cfg["chunk_chars"] or 1200))
    budget = max(0, int(cfg["budget_chars"] or 0))

    src = ""
    if _L2_SCOPE_TAIL in names:
        try:
            src = _l2_source_text(kind, payload, merged_text)
        except Exception:
            src = ""
    tool = ""
    if _L2_SCOPE_TOOL in names:
        try:
            if kind == "chat":
                tool = _tool_text_chat(payload)
            elif kind == "anthropic":
                tool = _tool_text_anthropic(payload)
            elif kind == "responses":
                tool = _tool_text_responses(payload)
        except Exception:
            tool = ""

    out = []
    used = 0
    for name in names:
        if name in _L2_SCOPE_EXCLUDED:
            _l2_scope_warn(f"scope {name!r} 已在方案 §3.7/§3.8 排除，配置项被忽略")
            continue
        if name == _L2_SCOPE_LAST_USER:
            t = _select_l2_text(kind, payload, merged_text)
        elif name == _L2_SCOPE_TOOL:
            t = tool[:cc]
        elif name == _L2_SCOPE_TAIL:
            t = src[cc:cc * 2]
        else:
            _l2_scope_warn(f"未知 scope {name!r}，已忽略")
            continue
        t = (t or "").strip()
        if not t:
            continue
        if name != _L2_SCOPE_LAST_USER and budget and used + len(t) > budget:
            continue
        used += len(t)
        out.append((name, t))
    if len(names) > _L2_SCOPE_MAX_BLOCKS:
        _l2_scope_warn(f"scope 数量 {len(names)} 超过上限 {_L2_SCOPE_MAX_BLOCKS}，"
                       f"超出部分会被 classify_chunks 静默丢弃: {names}")
    return out


def _extract_chat_bundle(messages):
    """P1: chat 消息单遍提取 text / last_user / total_chars。

    与旧内联四遍逻辑逐字节一致：text = content 全量 + tool_calls 全量拼接；
    last_user = _last_user_text_chat 等价（含 dict/对象双分支）；
    total_chars = 截断计数等价。
    """
    parts = []
    tool_parts = []
    total_chars = 0
    last_user = ""
    for m in messages or []:
        raw_c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        c = _msg_content_to_str(raw_c)
        if c:
            parts.append(c)
        total_chars += len(c)
        t = _tool_calls_text(getattr(m, "tool_calls", None))
        if t:
            tool_parts.append(t)
        try:
            d = m if isinstance(m, dict) else None
            role = (d.get("role") if d else getattr(m, "role", ""))
            if role != "user":
                continue
            if d:
                if d.get("tool_calls") or d.get("tool_call_id"):
                    continue
                cc = d.get("content")
            else:
                if getattr(m, "tool_calls", None) or getattr(m, "tool_call_id", None):
                    continue
                cc = getattr(m, "content", "")
            s = _msg_content_to_str(cc).strip()
            if s:
                last_user = s
        except Exception:
            pass
    text = " ".join(parts + tool_parts)
    return {"text": text, "last_user": last_user, "total_chars": total_chars,
            "audit_preview": (last_user or text)[:200]}



def _truncate_messages_for_limit(messages: list, max_chars: int = 100000) -> list:

    """Truncate oldest/longest messages to fit max_chars, keep last messages prioritized"""

    if not messages:

        return messages

    def msg_len(m):

        c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")

        if isinstance(c, str):

            return len(c)

        if isinstance(c, list):

            return sum(len(str(part.get("text","")) if isinstance(part, dict) else len(str(part))) for part in c)

        if c is None:

            return 0

        return len(str(c))

    total = sum(msg_len(m) for m in messages)

    if total <= max_chars:

        return messages

    new_msgs = []

    for i, m in enumerate(messages):

        is_dict = isinstance(m, dict)

        content = m.get("content") if is_dict else getattr(m, "content", None)

        cur_len = msg_len(m)

        if i >= len(messages) - 3:

            new_msgs.append(m)

            continue

        if cur_len > 5000:

            if isinstance(content, str):

                trunc = content[:5000] + "...[truncated]"

                if is_dict:

                    new_m = dict(m); new_m["content"] = trunc

                else:

                    new_m = m.model_dump() if hasattr(m, "model_dump") else dict(m)

                    new_m["content"] = trunc

                new_msgs.append(new_m)

            elif isinstance(content, list):

                new_content = []

                chars = 0

                for part in content:

                    if isinstance(part, dict) and part.get("type") == "text":

                        txt = str(part.get("text",""))

                        if chars + len(txt) > 5000:

                            txt = txt[:5000 - chars] + "...[truncated]"

                            new_content.append({"type":"text","text": txt})

                            break

                        new_content.append(part)

                        chars += len(txt)

                    else:

                        new_content.append(part)

                if is_dict:

                    new_m = dict(m); new_m["content"] = new_content

                else:

                    new_m = m.model_dump() if hasattr(m, "model_dump") else dict(m)

                    new_m["content"] = new_content

                new_msgs.append(new_m)

            else:

                new_msgs.append(m)

        else:

            new_msgs.append(m)

    return new_msgs









class ChatReq(BaseModel):

    model_config = ConfigDict(extra="allow")  # temperature / top_p 等原样透传



    model: str = "gpt-4o-mini"

    messages: list[ChatMessage]

    stream: bool = False





class EmbeddingReq(BaseModel):

    model_config = ConfigDict(extra="allow")



    model: str = ""

    input: Any = ""





class RegisterReq(BaseModel):

    """用户自定义模型注册请求体"""



    name: str

    base_url: str

    api_key: str

    api_mode: str = "openai"        # openai | anthropic

    default_model: str = ""





# ---------------------------------------------------------------- 公共逻辑



async def _inline_media_scan(kind: str, payload) -> tuple[str, bool]:
    """内联 base64 图片/文档 → (并入 policy 的文本, 是否有不可读块)。

    kind: chat | anthropic | responses。解码+检查在线程池跑，不阻塞事件循环；
    任一块不可读(解码失败/超 cap/抽空文本)即 fail-closed，由 policy p12 接住。
    """
    if kind == "chat":
        blobs, bad = collect_inline_blobs_chat(payload)
    elif kind == "anthropic":
        blobs, bad = collect_inline_blobs_anthropic(payload)
    elif kind == "responses":
        blobs, bad = collect_inline_blobs_responses(payload)
    else:
        return "", False
    if not blobs:
        return "", bad
    results = await asyncio.gather(*[run_in_threadpool(inspect_inline_bytes, x) for x in blobs])
    parts = []
    unreadable = bad
    for i, r in enumerate(results):
        ct = (r.get("text") or "").strip()
        if ct:
            parts.append(f"[INLINE_MEDIA {i + 1}]\n{ct}\n[/INLINE_MEDIA]")
        if r.get("unreadable"):
            unreadable = True
    return ("\n".join(parts), unreadable)


def wl_l2_sample() -> float:
    try:
        return float(os.getenv("AI_GATEWAY_WL_L2_SAMPLE", "0.05"))
    except (TypeError, ValueError):
        return 0.05


def _wl_l2_skip(request: Optional[Request]) -> bool:
    """白名单 KEY 的 L2 豁免（含抽样）；仅对非机密会话生效（调用点短路保证）。

    True=本次跳过 L2。判定按请求缓存；任何异常不豁免。
    只作用于文本 L2；文件通道（files/OCR/内联媒体）不经过这里。
    """
    if request is None:
        return False
    cached = getattr(request.state, "gw_wl_l2_skip", None)
    if cached is not None:
        return cached
    skip = False
    try:
        from . import admin_api as _admin_api
        raw = _CURRENT_CLIENT_KEY.get() or ""
        if raw and _admin_api.key_verdict(raw) == "white":
            p = wl_l2_sample()
            if random.random() < p:
                get_metrics().inc_l2_sampled()
            else:
                skip = True
                get_metrics().inc_l2_skipped()
    except Exception:
        skip = False
    try:
        request.state.gw_wl_l2_skip = skip
    except Exception:
        pass
    return skip


def _review_file_ctx(file_ctx: Optional[dict]) -> dict:
    """responses \u9644\u4ef6\u4e0a\u4e0b\u6587\u5e76\u5165 L1 ctx\uff08\u65e0\u5219\u5168\u7a7a\uff1b\u53ea\u53d6\u767d\u540d\u5355\u952e\uff09\u3002"""
    base = {"ext": "", "filename": "", "headers": [], "sheet_names": [], "text": ""}
    if not isinstance(file_ctx, dict):
        return base
    for _k in ("ext", "filename"):
        if file_ctx.get(_k):
            base[_k] = file_ctx[_k]
    for _k in ("headers", "sheet_names"):
        if file_ctx.get(_k):
            base[_k] = list(file_ctx[_k])
    return base

async def _review_text(text: str, request: Optional[Request] = None, extra_findings: Optional[dict] = None, l2_text: Optional[str] = None, l2_chunks: Optional[list] = None, file_ctx: Optional[dict] = None) -> tuple[dict, dict, dict | None]:

    """

    纯文本审查（chat / messages / responses 共用）：

    L1 规则 -> 命中即返回；L1 放行且（文本>30字 或 risk_score 灰区[20,60)）时触发 L2 小模型语义判定。

    返回 (decision, text_findings, l2_result)

    """

    text_findings = inspect_text(text)
    if extra_findings:
        text_findings.update(extra_findings)
    _note_risk(text_findings)

    session_id = extract_session_id(request.headers) if request is not None else None

    session_conf = _is_session_confidential(session_id)

    ctx = {

        "text": text,

        "file": _review_file_ctx(file_ctx),

        "findings": text_findings,

        "session": {"confidential": session_conf},

    }

    decision = decide(load_policy(), ctx)
    if request is not None and decision.get("action") != "allow":
        request.state.gw_block_reason = f"l1:{decision.get('name') or 'rule'}"



    l2_result = None

    # 注：/v1/embeddings 未复用本函数（内联 L1 副本，且不跑 L2），
    #     行为差异见 docs/gateway-hidden-issues-2026-09-15.md「范围外」一节。
    # 门槛按"实际喂给模型的文本"（l2_chunks / last_user 回退）计长，不按 merged
    # text：responses 路径 merged 恒含 instructions（codex ~100 字），旧口径让
    # codex 请求必然触发 L2，30 字边界失效（2026-09-18 修，三路径对齐）。
    _dec_blocks = l2_chunks or [(_L2_SCOPE_LAST_USER, l2_text or text)]
    _l2_feed = _dec_blocks[0][1] or ""
    if (decision.get("action") == "allow" and (len(_l2_feed.strip()) > 30 or _l2_gray_trigger(text_findings))
            and (session_conf or not _wl_l2_skip(request))):

        if not l2_chunks:
            _l2_scope_warn("无有效判定块，已回退 last_user")
        _hit = _dec_blocks[0][0]
        if len(_dec_blocks) == 1:
            # 单块（默认档 scopes=["last_user"]）仍走 classify —— 与历史逐字节等价
            l2_result = await small_classify(_dec_blocks[0][1], filename="", headers=[], sheet_names=[])
        else:
            l2_result = await classify_chunks([t for _, t in _dec_blocks],
                                              filename="", headers=[], sheet_names=[],
                                              scopes=[s for s, _ in _dec_blocks])
            _hit = (l2_result or {}).get("scope") or _hit
        if isinstance(l2_result, dict):
            # P2 留痕：命中块 + 实际跑过的块（「被哪一类内容拦下」要可查）
            l2_result["scope"] = _hit
            l2_result["scopes_run"] = [s for s, _ in _dec_blocks]
            l2_result.setdefault("degraded_scopes", [])

        _d = _l2_decision(l2_result)
        # P2b: decision-side scope metrics —— 判定块也要进 scope 指标。
        # 否则 `gateway_l2_scope_calls_total{last_user}` / `scope_hits{last_user}`
        # 恒为 0（只由探针路径写），而 P2 的交付物是「各 scope 命中率」：
        # 读的人会以为 last_user 从没跑过、也从没拦下过（2026-09-16 生产实测）。
        # 口径与探针侧一致：按「提交送审的块」计，缓存命中同样计入。
        # 注：/v1/files/check 那处 `_l2_decision` 是文件通道单块判定，不计入。
        try:
            _m_scope = get_metrics()
            for _s_dec, _ in _dec_blocks:
                _m_scope.inc_l2_scope_call(_s_dec)
            if _d is not None:
                _m_scope.inc_l2_scope_hit(_hit)
        except Exception:
            pass
        if _d is not None:
            decision = _d
            text_findings["l2"] = l2_result
            if request is not None:
                request.state.gw_block_reason = f"l2:{_hit}:{(l2_result or {}).get('reason') or 'confidential'}"

    return decision, text_findings, l2_result



def _l2_decision(l2_result: dict | None) -> dict | None:

    """L2 小模型结论 -> 覆盖用的 decision；None = 维持原判（L1 放行）。

    - 判密（label=CONFIDENTIAL 且 confidence >= 阈值）-> route_local
    - L2 判定不可用（超时 / 坏 JSON / 非法 label / 熔断，见 small_model.is_degraded）
      -> 默认同样 route_local：P0-2 修复。安全防线不得在故障时静默消失，
         而 route_local 既守住「内容不出境」，又不牺牲可用性（本地模型承接）。
    - AI_GATEWAY_L2_FAIL_MODE=open 时退回旧行为（当作 NORMAL 放行），
      但 classify 侧仍计 l2_degraded_total、审计里仍带 degraded 标记，留痕可查。
    """
    if not l2_result:
        return None
    if is_confidential(l2_result):
        return {
            "name": "small_model_confidential",
            "priority": 15,
            "action": "route_local",
            "target": {"provider": load_routing().default_local},
            "reason": l2_result.get("reason"),
            "confidence": l2_result.get("confidence"),
        }
    if is_degraded(l2_result):
        if (os.getenv("AI_GATEWAY_L2_FAIL_MODE", "local") or "local").strip().lower() == "open":
            return None
        return {
            "name": "l2_unavailable",
            "priority": 15,
            "action": "route_local",
            "target": {"provider": load_routing().default_local},
            "reason": f"L2 不可用（{l2_result.get('degraded_reason') or 'unknown'}），已降级到本地",
            "confidence": 0.0,
        }
    return None



def _mark_gw_action(request, decision) -> None:

    """把最终处置写进 request.state，供中间件落 request_log。

    审计条目的 action 直接取 handler 的 decision，而 request_log 过去是中间件
    用 gw_block_reason 是否存在推出来的 —— 两侧来源不同，口径必然漂移
    （2026-09-15：480 条全 2xx 请求被记成拦截 381）。这里让两侧同源。
    """
    if request is None:
        return
    try:
        request.state.gw_action = str((decision or {}).get("action") or "allow")
    except Exception:
        pass


def _block_to_fallback(decision: dict) -> tuple[dict, str | None]:

    """

    block 规则的处置：默认降级到本地模型而非 403。

    routing.yaml 的 routing.on_block 可切回 reject。

    """

    if decision.get("action") != "block":

        return decision, None

    cfg = load_routing()

    if cfg.on_block == "reject":

        return decision, None

    rule = decision.get("name")

    return {

        "name": rule,

        "priority": decision.get("priority"),

        "action": "route_local",

        "target": {"provider": cfg.default_local},

        "reason": f"命中拦截规则 {rule}，已自动降级到本地模型，内容未出境",

    }, rule





def _alias_ctx_now():
    from .alias_router import ctx_get
    return ctx_get()


def _gateway_meta(

    decision: dict,

    prov,

    model: str,

    notes: dict,

    l2_result: dict | None,

    downgraded: str | None,

) -> dict:

    return {

        "provider": prov.name if prov else "",

        "model": model,

        "endpoint": (prov.base_url if prov else "") or "",

        "local": bool(prov.local) if prov else False,

        "action": decision.get("action"),

        "policy_rule": decision.get("name"),
        "alias": ((_alias_ctx_now() or {}).get("name")) or "",
        "alias_failover": ((_alias_ctx_now() or {}).get("failover")) or [],

        "layer": "L2" if l2_result and decision.get("name") == "small_model_confidential" else "L1",

        "downgraded_from": downgraded,

        "override_denied": notes.get("override_denied"),

        "findings": None,

    }





def _apply_token_cap(payload: dict) -> dict:

    """调用方没给 max_tokens 时兜底 _DEFAULT_MAX_TOKENS；给了但超过上限才截断

    注意：兜底值必须与 `routing._DEFAULT_MAX_TOKENS` **同源**。本函数在 handler
    层先执行，会先于 routing 层把 max_tokens 填上 —— 若这里写死 300，routing
    层的默认值就永远轮不到（2026-09-15 实测：走网关时 completion_tokens 卡在
    300、finish_reason=length，长回答被截断）。
    """

    cap = int(os.getenv("AI_GATEWAY_MAX_TOKENS_CAP", "0") or 0)

    try:

        cur = int(payload.get("max_tokens") or 0)

    except (TypeError, ValueError):

        cur = 0

    if cur <= 0:

        payload["max_tokens"] = _DEFAULT_MAX_TOKENS

    elif cap and cur > cap:

        payload["max_tokens"] = cap

    return payload





def _note_risk(tf):
    """把本次文本审查的风险分暂存进请求上下文，log_entry 落审计时读取。"""
    try:
        _CURRENT_RISK.set(int((tf or {}).get("risk_score") or 0))
        try:
            _CURRENT_PII_SUMMARY.set(_pii_audit_summary(tf))
        except Exception:
            pass
    except (TypeError, ValueError):
        pass


_CTX_OVERFLOW_MARKERS = (
    "maximum context length",      # openai / vllm
    "context_length_exceeded",     # openai / deepseek
    "max_num_tokens",              # vllm 参数名
    "prompt length",               # vllm / 部分网关
    "input length",                # 通用
)


def _looks_like_context_overflow(msg: str) -> bool:
    """上游报错是否真的是「上下文/输入过长」。

    2026-09-15：原先只要 status==502（含超时、连接重置）就当成 prompt too long，
    长生成超时被谎报成 413，且真实错误不落审计。
    """
    low = (msg or "").lower()
    return any(k in low for k in _CTX_OVERFLOW_MARKERS)


def _upstream_error_detail_response(exc, msg: str, ctx_overflow: bool, prov, decision, request,
                                    *, type_: str) -> HTTPException:
    """把上游错误如实转成 HTTPException（并落审计）。

    请求已在本地上游时本地降级无从谈起，必须如实上报：
      真上下文溢出 -> 413 prompt_too_long（带真实 message）
      其他（超时/5xx） -> 原状态码 upstream_error（带真实 message）
    """
    try:
        log_upstream_failure(request, type_=type_, status=getattr(exc, "status_code", 0),
                             action=(decision or {}).get("action"),
                             rule=(decision or {}).get("name"),
                             provider=(getattr(prov, "name", "") or ""),
                             local=bool(getattr(prov, "local", False)))
    except Exception:
        pass
    if ctx_overflow:
        return HTTPException(413, detail={"type": "prompt_too_long",
                                          "message": "Prompt too long: " + str(msg)[:300]})
    return HTTPException(int(getattr(exc, "status_code", 0) or 502),
                         detail={"type": "upstream_error",
                                 "provider": (getattr(prov, "name", "") or ""),
                                 "message": str(msg)[:300]})


def log_upstream_failure(request, *, type_: str, status, action=None, rule=None,
                         provider=None, local=False, model="", count=True, **extra):
    """上游转发/路由失败也要记账（否则控制台错误率恒 0%）。

    背景（2026-09-14 生产实测）：各 handler 的 `except RoutingError` 只把
    RoutingError 转成 HTTPException、不调 log_entry()，而 log_entry() ->
    inc_request_v2() 是 metrics._requests_v2 的唯一写入点。结果 502/503
    这类失败在指标里完全不存在：by_type 里连这一类都没有、by_status_class
    全是 2xx、控制台错误率恒 0%，而这类请求实际 100% 失败。

    状态码必须显式写进 ContextVar 再调 log_entry()：_capture_response_status
    中间件要等 call_next 返回后才 set_current_status(response.status_code)，
    handler 内读到的永远是 0，只能靠 _infer_status() 猜。
    """
    try:
        set_current_status(int(status or 0))
    except (TypeError, ValueError):
        pass
    entry = {
        "client_ip": _resolve_client_ip(request),
        "requested_model": str(model or ""),
        "type": type_,
        "action": action,
        "rule": rule,
        "provider": getattr(provider, "name", "") or "",
        "local": bool(getattr(provider, "local", False)),
        "model": model or "",
        "upstream_error": int(status or 0),
    }
    entry.update({k: v for k, v in extra.items() if v is not None})
    try:
        log_entry(entry, count=count)
    except Exception:
        # 记账失败绝不能吃掉真实的上游错误响应
        pass

def _resolve_actual_model(model, provider):
    """审计 model 列记实际模型：别名按别名组候选解析（优先同 provider，否则主候选）。

    非别名 / 无 provider / 解析失败一律原样返回；requested_model 保留别名可追溯。
    """
    m = (model or "").strip()
    if not m:
        return m
    try:
        from src.gateway.admin_store import get_admin_store
        cands = (get_admin_store().get_alias_routes().get(m.lower()) or {}).get("candidates") or []
    except Exception:
        return m
    if not cands:
        return m
    p = (provider or "").strip()
    for c in cands:
        if p and c.get("provider") == p:
            return c.get("model") or m
    return cands[0].get("model") or m


def log_entry(entry: dict, *, count: bool = True):

    redact_audit_entry(entry)

    # 模型列记实际：别名请求放行/本地路由时 model 还是别名（如 ext-flash），
    # 这里按落点 provider 解析成真实模型（deepseek-flash）。无落点（拦截/失败）保持原样。
    if entry.get("model") and entry.get("provider"):
        try:
            entry["model"] = _resolve_actual_model(entry.get("model"), entry.get("provider"))
        except Exception:
            pass

    entry["time"] = time.strftime("%Y-%m-%d %H:%M:%S")

    entry["id"] = str(uuid.uuid4())[:8]

    if not entry.get("token_masked"):
        _ck = _CURRENT_CLIENT_KEY.get()
        if _ck:
            try:
                from .admin_store import mask_key as _mask_key
                entry["token_masked"] = _mask_key(_ck)
            except Exception:
                pass
    if entry.get("risk_score") is None:
        _rs = _CURRENT_RISK.get()
        if _rs is not None:
            entry["risk_score"] = _rs
    if entry.get("pii_summary") is None:
        try:
            _ps = _CURRENT_PII_SUMMARY.get()
        except Exception:
            _ps = None
        if _ps is not None:
            entry["pii_summary"] = _ps

    get_audit_store().append(entry)

    if not count:
        # T43：流式失败的补写行（log_stream_error）走这里 —— 该请求已被流开始前的首行
        # 计过一次数，重复计数会让 requests_v2（请求总数分母）把一次失败算成两次。
        # ⚠️ 本函数此后新增的副作用都会被这里跳过；新逻辑请放在 append 之前。
        return

    # metrics: P2 7-axis counter (provider/model/local + status_code)

    status = get_current_status() or _infer_status(entry)

    get_metrics().inc_request_v2(

        type_=entry.get("type", ""),

        action=entry.get("action", ""),

        rule=entry.get("rule", ""),

        provider=entry.get("provider", ""),

        model=entry.get("model", "") or entry.get("requested_model", ""),

        local=bool(entry.get("local", False)),

        status_code=status,

    )

    # legacy 5-axis counter (kept for old dashboards)

    get_metrics().inc_request(

        type_=entry.get("type", ""),

        action=entry.get("action", ""),

        rule=entry.get("rule", ""),

        provider=entry.get("provider", ""),

        local=bool(entry.get("local", False)),

    )

    if entry.get("override_denied"):

        get_metrics().inc_override_denied()

    if entry.get("downgraded_from") == "prompt_too_long" or entry.get("rule") == "first_chunk_fallback":

        get_metrics().inc_fallback()

    # token counts (if upstream reported)

    # P1-b/P1-c 三守卫：
    #   ① 缓存命中不是真实调用，入直方图会把 p50 拉低；
    #   ② latency_ms == 0 一律视为「无真实上游调用」（small_model disabled /
    #      空输入 / 熔断短路），同样不入桶；
    #   ③ degraded 的 label 恒为 "NORMAL"（small_model._degraded），必须单独成桶，
    #      否则超时 / 坏 JSON / 熔断的耗时会伪装成「正常判定耗时」。
    _l2e = entry.get("l2")

    if isinstance(_l2e, dict):

        if _l2e.get("cached"):

            get_metrics().inc_l2_cached()

        else:

            l2_lat = _l2e.get("latency_ms")

            if isinstance(l2_lat, (int, float)) and l2_lat > 0:

                label = "DEGRADED" if _l2e.get("degraded") else _l2e.get("label", "UNKNOWN")

                get_metrics().observe_l2(label, float(l2_lat))



def log_stream_error(request, *, type_: str, status, action=None, rule=None,
                     provider=None, model="", kind="upstream", **extra):
    """T43：流式路径在**首行已落库之后**失败时补写的第二条留痕。

    背景（2026-09-17，T37/T43 两次生产实证）：三个流式端点的 log_entry() 都写在
    `return StreamingResponse` **之前**，流内失败只能发 SSE error 事件 ⇒ 审计里
    只有一条 allow/200，管理面 100% 隐身（Codex 五次重试全记 200）。

    与 log_upstream_failure 的差异（两点都不可省）：
      1. `count=False` —— 首行已在流开始前 inc_request_v2 记过一次；补写行若再计，
         `sum(requests_v2)`（请求总数分母，方案 §4 与 overview.total 都用它）
         会把一次失败请求记成两次 ⇒ 分母失真。
      2. `stream_error=True` + `stream_error_kind` —— 审计里的可归因标记；
         另打独立计数 gateway_stream_errors_total（否则错误率仍恒 0%）。

    ⚠️ HTTP 层仍是 200，**这是真实语义**（SSE 失败走流内 error 事件）。
    本函数不改变状态码，只补「失败确实发生了」这件事的留痕；判据因此是
    审计里出现 stream_error=true，而不是「状态码变 5xx」。

    ⚠️ 本函数运行在生成器内部，抛异常会掐断客户端流 ⇒ 全路径吞异常。
    """
    try:
        get_metrics().inc_stream_error(type_, getattr(provider, "name", "") or "",
                                       int(status or 0))
    except Exception:
        pass
    try:
        log_upstream_failure(request, type_=type_, status=status, action=action, rule=rule,
                             provider=provider, local=bool(getattr(provider, "local", False)),
                             model=model, count=False, stream_error=True,
                             stream_error_kind=kind, **extra)
    except Exception:
        # 留痕失败绝不能吃掉真实的上游错误响应
        pass

def _infer_status(entry: dict) -> int:

    """P2 fallback: estimate HTTP status from action/rule when context var is empty.

    Used in tests and call paths that do not go through middleware."""

    rule = (entry.get("rule") or "").lower()

    action = (entry.get("action") or "").lower()

    if action == "block" or rule == "egress_denied":

        return 403

    if action == "route_local" and "fallback" in rule:

        return 502

    if action in ("allow", "route_local"):

        return 200

    return 0


def _entry_blocked(entry: dict) -> bool:
    """审计条目是否算「拦截」——口径唯一实现在 stat_scope.is_blocked()。

    审计条目不带 status_code，用 _infer_status() 反推（action=block 或
    rule=egress_denied -> 403），再交给同一条判定式。审计页 KPI、可视化页
    c_block、趋势红柱三处共用本函数，避免同页自相矛盾。
    """
    return is_blocked(entry.get("action"), _infer_status(entry))



def _normalize_usage(u) -> tuple:
    """上游 usage -> (prompt_tokens, completion_tokens)，非负 int。

    兼容 OpenAI(prompt/completion_tokens) 与 Anthropic(input/output_tokens)。
    非流式取完整 JSON 的 usage；流式由中间件 SSE 扫描提取（见 _parse_sse_usage_event）。
    """
    try:
        d = dict(u or {})
    except Exception:
        return 0, 0
    def _int(*keys):
        for k in keys:
            try:
                v = int(d.get(k) or 0)
            except (TypeError, ValueError):
                continue
            if v > 0:
                return v
        return 0
    return _int("prompt_tokens", "input_tokens"), _int("completion_tokens", "output_tokens")


def _parse_sse_usage_event(ev: bytes, out: dict, client_wants_usage: bool) -> bytes:
    """扫描单个 SSE 事件：提取 usage 到 out；按需剥离 usage-only chunk。

    返回应转发给客户端的字节（b"" 表示丢弃该事件）。
    仅识别同时含 prompt/input 与 completion/output 两侧键的 OpenAI 风格 usage，
    避免 Anthropic 流 message_start(仅 input)/message_delta(仅 output) 被误判。
    """
    try:
        line = None
        for ln in ev.split(b"\n"):
            if ln.startswith(b"data:"):
                line = ln[5:].strip()
                break
        if not line or line == b"[DONE]":
            return ev
        obj = json.loads(line)
        if not isinstance(obj, dict):
            return ev
        usage = obj.get("usage")
        if not isinstance(usage, dict) or not usage:
            return ev
        has_p = any(k in usage for k in ("prompt_tokens", "input_tokens"))
        has_c = any(k in usage for k in ("completion_tokens", "output_tokens"))
        if has_p and has_c:
            out["p"], out["c"] = _normalize_usage(usage)
        # usage-only chunk（choices 为空）且客户端未主动要求 -> 剥离不转发
        if not client_wants_usage and obj.get("choices") == []:
            return b""
        return ev
    except Exception:
        return ev


def _sse_err(obj: dict) -> bytes:

    """SSE 错误事件（流式响应中途路由失败时用）"""

    return f"event: error\ndata: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")





# ---------------------------------------------------------------- 端点



@app.get("/", response_class=HTMLResponse)

async def index():

    p = os.path.join(os.path.dirname(__file__), "../../static/index.html")

    if os.path.exists(p):

        return FileResponse(p)

    return HTMLResponse("<h3>Secure Gateway running. POST /v1/chat/completions or /v1/files/check</h3>")





@app.get("/favicon.ico")
@app.get("/favicon.png")
async def favicon():
    """Browser tab icon (Image 1)."""
    p = os.path.join(os.path.dirname(__file__), "../../static/favicon.png")
    if os.path.exists(p):
        return FileResponse(p, media_type="image/png")
    return Response(status_code=404)


@app.get("/health")

async def health():

    cfg = load_routing()

    return {

        "status": "ok",

        "policy_count": len(load_policy()),

        "providers": {n: {"local": p.local, "kind": p.kind, "configured": p.configured} for n, p in cfg.providers.items()},

        "default_external": cfg.default_external,

        "default_local": cfg.default_local,

        "on_block": cfg.on_block,

    }





@app.get("/v1/models")
async def list_models(identity: Identity = Depends(_bind_identity_ctx)):
    """OpenAI compatible model list — 只暴露对外别名组（ext-flash / ext-pro）。

    别名组成员与默认模型在 Admin Console 配置；用户自注册模型（/v1/models/register）
    保持展示，不受别名机制影响。上游真实模型目录不再对外暴露（真实模型名请求仍兼容）。
    """
    from .admin_store import get_admin_store
    data: list = []
    now = int(time.time())
    for g in get_admin_store().list_alias_groups():
        members = [m for m in (g.get("members") or []) if m.get("enabled", 1)]
        if not members:
            continue
        members = sorted(members, key=lambda x: x["priority"])
        default = members[0]
        data.append({
            "id": g["name"],
            "object": "model",
            "created": now,
            "owned_by": "gateway",
            "gateway": {
                "kind": "chat",
                "alias_group": g["name"],
                "default": f"{default['provider']}/{default['model']}",
                "candidates": [f"{m['provider']}/{m['model']}" for m in members],
            },
        })
    # 用户注册模型始终展示（不受别名机制影响；chat 路径按注册名解析）
    for m in get_store().list():
        mid = m.default_model or m.name
        data.append({
            "id": str(m.name),
            "object": "model",
            "created": int(m.created_at),
            "owned_by": f"user:{m.owner}",
            "gateway": {
                "kind": "chat",
                "local": False,
                "default_model": m.default_model,
                "model": str(mid),
                "configured": True,
                "user_defined": True,
            },
        })
    return {"object": "list", "data": data}


@app.post("/v1/models/register")

async def register_model(req: RegisterReq, request: Request, identity: Identity = Depends(_bind_identity_ctx)):

    """

    用户自定义模型注册：提交 name + endpoint + key，网关校验域名白名单后持久化。

    后续请求用 model=<name> 即可走该模型（仍过安全审查，不通过降级本地）。

    """

    session_id = extract_session_id(request.headers)

    owner = request.headers.get("X-Gateway-User") or "anonymous"

    err = validate(req.name, req.base_url, req.api_key, req.api_mode)

    if err:

        raise HTTPException(400, detail={"type": "invalid_model", "message": err})

    m = UserModel(

        name=req.name,

        base_url=req.base_url,

        api_key=req.api_key,

        api_mode=req.api_mode,

        default_model=req.default_model,

        owner=owner,

    )

    get_store().register(m)

    log_entry({"client_ip": _resolve_client_ip(request), "requested_model": "", "type": "model_register", "name": req.name, "owner": owner,

               "base_url": req.base_url, "api_mode": req.api_mode,

               "session_id": session_id,})

    return {"object": "model", "id": m.name, "gateway": m.public()}





@app.delete("/v1/models/{name}")

async def unregister_model(name: str, request: Request, identity: Identity = Depends(_bind_identity_ctx)):

    """注销用户自定义模型（仅本人或管理员可删；管理员通过 X-Gateway-Admin: 1 标识）"""

    store = get_store()

    m = store.get(name)

    if m is None:

        raise HTTPException(404, detail={"type": "not_found", "message": f"模型 {name} 不存在"})

    caller = request.headers.get("X-Gateway-User") or "anonymous"

    is_admin = request.headers.get("X-Gateway-Admin") == "1"

    if not is_admin and m.owner != caller:

        raise HTTPException(403, detail={"type": "forbidden",

                                         "message": "只能注销本人注册的模型"})

    store.delete(name)

    log_entry({"client_ip": _resolve_client_ip(request), "requested_model": "", "type": "model_unregister", "name": name, "owner": m.owner, "caller": caller})

    return {"object": "model", "id": name, "deleted": True}





@app.post("/v1/chat/completions")

async def chat_completions(req: ChatReq, request: Request, identity: Identity = Depends(_rate_limit_chat)):

    session_id = extract_session_id(request.headers)

    _bundle = _extract_chat_bundle(req.messages)
    text = _bundle["text"]

    audit_preview = _bundle["audit_preview"]
    inline_text, inline_bad = await _inline_media_scan("chat", req.messages)
    if inline_text:
        text = text + "\n" + inline_text
    l2_blocks = _select_l2_scopes("chat", req.messages, text)

    decision, text_findings, l2_result = await _review_text(
        text, request, {"ocr_empty_and_image": True} if inline_bad else None,
        l2_chunks=l2_blocks)



    # block -> 默认自动降级到本地模型（routing.on_block=reject 时才 403）

    decision, downgraded = _block_to_fallback(decision)
    _mark_gw_action(request, decision)
    _mark_pre_upstream(request)
    _mentioned = _mentioned_data_filename(text)
    _mark_session_hit(session_id, decision, "chat-inline-image" if (inline_text or inline_bad) else ("chat-mentioned-file:" + _mentioned if _mentioned else ""))

    if decision.get("action") == "block":

        log_entry({"client_ip": _resolve_client_ip(request), "requested_model": str(req.model or ""), "type": "chat", "action": "block", "rule": decision.get("name"), "text_preview": audit_preview, "l2": l2_result,

        "session_id": session_id,})

        raise HTTPException(403, detail={"type": "policy_violation", "rule": decision.get("name"), "message": "内容被安全策略拦截，未转发外网"})



    payload = _apply_token_cap(req.model_dump())

    # pre-truncate if prompt too long (DeepInfra max 32768 tokens ~ 130k chars)

    if _bundle["total_chars"] > 100000:
        _msgs = payload.get("messages") or []
        payload["messages"] = _truncate_messages_for_limit(_msgs, max_chars=90000)

    try:

        prov = None

        model = ""

        prov, model, notes = resolve(decision, payload, request.headers, kind="chat")

        if payload.get("stream"):
            # 流式 token 统计：外部供应商注入 include_usage，让上游在末 chunk 回 usage
            # 本地 vLLM 不注入（老版本兼容性优先），其流式 tokens 记 0
            try:
                if not prov.local and not (payload.get("stream_options") or {}).get("include_usage"):
                    payload = {**payload, "stream_options": {"include_usage": True}}
            except Exception:
                pass


            async def gen():

                sent = False

                try:

                    # stream upstream

                    async for chunk in route_chat_stream(payload, decision, request.headers):

                        sent = True

                        yield chunk

                except RoutingError as exc:

                    if not sent and exc.status_code >= 500:  # 仅瞬态错误(5xx/网络)降级；上游 4xx(密钥/权限/限流)直接透传

                        # 首字节前上游失败：透明降级到本地模型，对调用方无感

                        try:

                            cfg_fc = load_routing()

                            lp = cfg_fc.local_provider()

                            if lp is not None and prov.name != lp.name:

                                fb_decision = {"name": "first_chunk_fallback", "priority": 0,

                                               "action": "route_local", "target": {"provider": lp.name}}

                                notice = {"id": "chatcmpl-fallback", "object": "chat.completion.chunk",

                                          "created": int(time.time()), "model": lp.default_model,

                                          "choices": [{"index": 0, "delta": {}, "finish_reason": None}],

                                          "gateway": {"provider": lp.name, "downgraded_from": "upstream_error"}}

                                yield f"data: {json.dumps(notice, ensure_ascii=False)}\n\n".encode("utf-8")

                                fb_payload = dict(payload)
                                fb_payload.pop("stream_options", None)

                                fb_payload["messages"] = _truncate_messages_for_limit(payload.get("messages") or [], max_chars=60000)

                                async for chunk in route_chat_stream(fb_payload, fb_decision, request.headers):

                                    sent = True

                                    yield chunk

                                log_entry({"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(req.model or ""),

                                    "type": "chat", "action": "route_local", "rule": "first_chunk_fallback",

                                    "provider": lp.name, "local": True, "model": lp.default_model,

                                    "text_preview": audit_preview, "downgraded_from": "upstream_error", "stream": True})

                                return

                        except Exception:

                            pass  # 本地也失败 -> 走下方原有错误上报

                    msg = str(exc)

                    _ctx_overflow = _looks_like_context_overflow(msg)
                    if _ctx_overflow or exc.status_code in (408, 502, 504):

                        # fallback: truncate + reroute to local

                        try:

                            cfg = load_routing()

                            local_prov = cfg.local_provider()

                            if local_prov and prov and prov.name != local_prov.name:

                                fallback_payload = dict(payload)
                                fallback_payload.pop("stream_options", None)

                                fallback_payload["messages"] = _truncate_messages_for_limit(payload.get("messages") or [], max_chars=60000)

                                fallback_decision = {"name": "prompt_too_long_fallback", "priority": 0, "action": "route_local", "target": {"provider": local_prov.name}}

                                # emit a small notice chunk so client knows

                                notice = {"id": "chatcmpl-fallback", "object": "chat.completion.chunk", "created": int(time.time()),

                                          "model": local_prov.default_model,

                                          "choices": [{"index": 0, "delta": {"role": "assistant", "content": "[fallback to local due to prompt too long, regenerating...]\n"}, "finish_reason": None}],

                                          "gateway": {"provider": local_prov.name, "downgraded_from": "prompt_too_long"}}

                                yield f"data: {json.dumps(notice, ensure_ascii=False)}\n\n".encode("utf-8")

                                log_entry({"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(req.model or ""), 

                                    "type": "chat", "action": "route_local", "rule": "prompt_too_long_fallback",

                                    "provider": local_prov.name, "local": True, "model": local_prov.default_model,

                                    "text_preview": audit_preview, "layer": "L1", "downgraded_from": "prompt_too_long",

                                    "stream": True,

                                })

                                async for chunk in route_chat_stream(fallback_payload, fallback_decision, request.headers):

                                    yield chunk

                                return

                        except Exception as e2:

                            # fallback failed

                            log_stream_error(request, type_="chat", status=getattr(exc, "status_code", 0) or 502,
                                 action=decision.get("action"), rule=decision.get("name"),
                                 provider=prov, model=model, kind="upstream_fallback_failed",
                                 session_id=session_id, text_preview=audit_preview)
                            err = {"error": {"type": (getattr(exc, "type_", "") or "routing_error"), "provider": prov.name if prov else "", "message": str(exc) + f" (fallback failed: {e2})"}}

                            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode("utf-8")

                            yield b"data: [DONE]\n\n"

                            return

                    # non-fallback error

                    log_stream_error(request, type_="chat", status=getattr(exc, "status_code", 0) or 502,
                         action=decision.get("action"), rule=decision.get("name"),
                         provider=prov, model=model, kind="upstream",
                         session_id=session_id, text_preview=audit_preview)
                    err = {"error": {"type": (getattr(exc, "type_", "") or "routing_error"), "provider": prov.name if prov else "", "message": str(exc)}}

                    yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode("utf-8")

                    yield b"data: [DONE]\n\n"

                except Exception as exc:

                    # 兜底：任何异常都不允许逃出生成器，否则客户端看到的是被掐断的流
                    log_stream_error(request, type_="chat", status=502,
                         action=decision.get("action"), rule=decision.get("name"),
                         provider=prov, model=model, kind="internal",
                         session_id=session_id, text_preview=audit_preview)
                    err = {"error": {"type": "internal_error", "message": f"gateway stream error: {exc}"}}

                    yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode("utf-8")

                    yield b"data: [DONE]\n\n"



            log_entry({"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(req.model or ""), 

                "type": "chat", "action": decision.get("action"), "rule": decision.get("name"),

                "provider": prov.name, "local": prov.local, "model": model,

                "text_preview": audit_preview, "layer": "L2" if decision.get("name") == "small_model_confidential" else "L1",

                "downgraded_from": downgraded, "stream": True,

                "session_id": session_id,})

            headers = {

                "X-Gateway-Provider": prov.name,

                "X-Gateway-Local": "1" if prov.local else "0",

                "X-Gateway-Action": _hdr_safe(decision.get("action")),

                "X-Gateway-Rule": _hdr_safe(decision.get("name")),

            }

            return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)



        try:

            result = await route_chat(payload, decision, request.headers)

        except RoutingError as exc:

            msg = str(exc)

            _ctx_overflow = _looks_like_context_overflow(msg)
            if _ctx_overflow or exc.status_code in (408, 502, 504):

                try:

                    cfg = load_routing()

                    local_prov = cfg.local_provider()

                    if local_prov and prov and prov.name != local_prov.name:

                        fallback_decision = {"name": "prompt_too_long_fallback", "priority": 0, "action": "route_local", "target": {"provider": local_prov.name}}

                        payload["messages"] = _truncate_messages_for_limit(payload.get("messages") or [], max_chars=60000)

                        result = await route_chat(payload, fallback_decision, request.headers)

                        downgraded = downgraded or "prompt_too_long"

                        prov, model, notes = local_prov, local_prov.default_model, {"fallback": "prompt_too_long"}

                    else:

                        raise _upstream_error_detail_response(exc, msg, _ctx_overflow, prov, decision, request, type_="chat")

                except HTTPException:

                    raise

                except Exception as e2:

                    log_upstream_failure(request, type_="chat", status=exc.status_code,

                                          action=decision.get("action"), rule=decision.get("name"),

                                          provider=prov, model=model, text_preview=audit_preview, session_id=session_id)

                    raise HTTPException(exc.status_code, detail={"type": (getattr(exc, "type_", "") or "routing_error"), "provider": prov.name if prov else "", "message": str(exc) + f" (fallback failed: {e2})"})

            else:

                log_upstream_failure(request, type_="chat", status=exc.status_code,

                                      action=decision.get("action"), rule=decision.get("name"),

                                      provider=prov, model=model, text_preview=audit_preview, session_id=session_id)

                raise HTTPException(exc.status_code, detail={"type": (getattr(exc, "type_", "") or "routing_error"), "provider": prov.name if prov else "", "message": str(exc)})



        gw = _gateway_meta(decision, prov, model, notes, l2_result, downgraded)
        try:
            from .model_policy import pop_fallback_trace
            _fb = pop_fallback_trace()
            if _fb:
                gw["model_fallback"] = _fb
        except Exception:
            pass

        gw["findings"] = text_findings

        if l2_result:

            gw["l2"] = l2_result

        # 别名请求：route_chat 实际路由的候选与 main.py 预解析不同，用 ctx 里的真实值覆盖展示
        _actx = _alias_ctx_now() or {}
        _acur = _actx.get("current")
        if _acur:
            gw["provider"] = _acur.get("provider") or gw["provider"]
            gw["model"] = _acur.get("model") or gw["model"]
            if isinstance(_acur.get("local"), bool):
                gw["local"] = _acur["local"]

        result["gateway"] = gw

        log_entry({"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(req.model or ""), 

            "type": "chat", "action": decision.get("action"), "rule": decision.get("name"),

            "provider": gw["provider"], "local": gw["local"], "model": gw["model"],

            "text_preview": audit_preview, "layer": gw["layer"], "downgraded_from": downgraded,

            "filename": _mentioned, "session_id": session_id,})

        try:
            request.state.gw_usage = result.get("usage") if isinstance(result, dict) else None
        except Exception:
            pass

        return JSONResponse(result)

    except RoutingError as exc:

        log_upstream_failure(request, type_="chat", status=exc.status_code,

                              action=decision.get("action"), rule=decision.get("name"),

                              provider=prov, model=model, text_preview=audit_preview, session_id=session_id)

        raise HTTPException(exc.status_code, detail={"type": (getattr(exc, "type_", "") or "routing_error"), "provider": prov.name if prov else "", "message": str(exc)})





@app.post("/v1/embeddings")

async def embeddings(req: EmbeddingReq, request: Request, identity: Identity = Depends(_rate_limit_embed)):

    raw = req.input

    text = " ".join(raw) if isinstance(raw, list) else str(raw or "")

    text_findings = inspect_text(text)
    _note_risk(text_findings)



    session_id = extract_session_id(request.headers) if request is not None else None

    session_conf = _is_session_confidential(session_id)

    ctx = {

        "text": text,

        "file": {"ext": "", "filename": "", "headers": [], "sheet_names": [], "text": ""},

        "findings": text_findings,

        "session": {"confidential": session_conf},

    }

    decision = decide(load_policy(), ctx)
    if decision.get("action") != "allow":
        request.state.gw_block_reason = f"l1:{decision.get('name') or 'rule'}"

    decision, downgraded = _block_to_fallback(decision)
    _mark_gw_action(request, decision)
    _mark_pre_upstream(request)

    if decision.get("action") == "block":

        log_entry({"client_ip": _resolve_client_ip(request), "requested_model": str(req.model or ""),
                   "type": "embedding", "action": "block", "rule": decision.get("name"),
                   "text_preview": text[:200], "session_id": session_id})
        raise HTTPException(403, detail={"type": "policy_violation", "rule": decision.get("name")})



    payload = req.model_dump()

    payload["input"] = raw

    try:

        prov = None

        model = ""

        prov, model, notes = resolve(decision, payload, request.headers, kind="embedding")

        result = await route_embeddings(payload, decision, request.headers)

    except RoutingError as exc:

        log_upstream_failure(request, type_="embedding", status=exc.status_code,

                              action=decision.get("action"), rule=decision.get("name"),

                              provider=prov, model=model, text_preview=text[:200])

        raise HTTPException(exc.status_code, detail={"type": (getattr(exc, "type_", "") or "routing_error"), "provider": prov.name if prov else "", "message": str(exc)})



    result["gateway"] = _gateway_meta(decision, prov, model, notes, None, downgraded)

    log_entry({"client_ip": _resolve_client_ip(request), "requested_model": str(req.model or ""), 

        "type": "embedding", "action": decision.get("action"), "rule": decision.get("name"),

        "provider": prov.name, "local": prov.local, "model": model, "text_preview": text[:200],

    })

    return JSONResponse(result)





# ---------------------------------------------------------------- Anthropic Messages（ccswitch / Claude Code）

# ccswitch 把 ANTHROPIC_BASE_URL 指向本网关后，Claude Code 会发

# POST {base}/v1/messages（Anthropic 协议，可能流式）。审查与降级逻辑同 chat。



@app.post("/v1/messages")

async def anthropic_messages(request: Request, identity: Identity = Depends(_rate_limit_messages)):

    session_id = extract_session_id(request.headers)

    body = await request.json()

    text = anthropic_text(body)

    audit_preview = _audit_preview_anthropic(body, text)
    inline_text, inline_bad = await _inline_media_scan("anthropic", body)
    if inline_text:
        text = text + "\n" + inline_text
    l2_blocks = _select_l2_scopes("anthropic", body, text)

    decision, text_findings, l2_result = await _review_text(
        text, request, {"ocr_empty_and_image": True} if inline_bad else None,
        l2_chunks=l2_blocks)



    decision, downgraded = _block_to_fallback(decision)
    _mark_gw_action(request, decision)
    _mark_pre_upstream(request)
    _mentioned = _mentioned_data_filename(text)
    _mark_session_hit(session_id, decision, "messages-inline-image" if (inline_text or inline_bad) else ("messages-mentioned-file:" + _mentioned if _mentioned else ""))

    if decision.get("action") == "block":

        log_entry({"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(body.get("model") or ""), "type": "messages", "action": "block", "rule": decision.get("name"), "text_preview": audit_preview,

        "session_id": session_id,})

        raise HTTPException(403, detail={"type": "policy_violation", "rule": decision.get("name"),

                                         "message": "请求因机密策略被拦截，未转发至外网"})



    def _meta(prov, model, notes):

        gw = _gateway_meta(decision, prov, model, notes, l2_result, downgraded)

        gw["findings"] = text_findings

        return gw



    if body.get("stream"):

        try:

            prov = None

            model = ""

            prov, model, notes = resolve(decision, {"model": str(body.get("model") or "")}, request.headers, kind="chat")

        except RoutingError as exc:

            log_upstream_failure(request, type_="messages", status=exc.status_code,

                                  action=decision.get("action"), rule=decision.get("name"),

                                  provider=prov, model=model, text_preview=audit_preview, session_id=session_id)

            raise HTTPException(exc.status_code, detail={"type": (getattr(exc, "type_", "") or "routing_error"), "message": str(exc)})



        async def gen():

            sent = False

            try:

                async for chunk in route_messages_stream(body, decision, request.headers):

                    sent = True

                    yield chunk

            except RoutingError as exc:

                if not sent and exc.status_code >= 500:  # 仅瞬态错误(5xx/网络)降级；上游 4xx(密钥/权限/限流)直接透传

                    # 首字节前上游失败：透明降级到本地模型

                    try:

                        cfg_fc = load_routing()

                        lp = cfg_fc.local_provider()

                        if lp is not None and prov.name != lp.name:

                            fb_decision = {"name": "first_chunk_fallback", "priority": 0,

                                           "action": "route_local", "target": {"provider": lp.name}}

                            async for chunk in route_messages_stream(body, fb_decision, request.headers):

                                sent = True

                                yield chunk

                            log_entry({"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(body.get("model") or ""),

                                "type": "messages", "action": "route_local", "rule": "first_chunk_fallback",

                                "provider": lp.name, "local": True, "text_preview": audit_preview,

                                "downgraded_from": "upstream_error", "stream": True})

                            return

                    except Exception:

                        pass

                log_stream_error(request, type_="messages", status=getattr(exc, "status_code", 0) or 502,
                     action=decision.get("action"), rule=decision.get("name"),
                     provider=prov, model=model, kind="upstream",
                     session_id=session_id, text_preview=audit_preview)
                yield _sse_err({"type": "error", "error": {"type": "gateway_error", "message": str(exc)},

                "session_id": session_id,})

            except Exception as exc:

                log_stream_error(request, type_="messages", status=502,
                     action=decision.get("action"), rule=decision.get("name"),
                     provider=prov, model=model, kind="internal",
                     session_id=session_id, text_preview=audit_preview)
                yield _sse_err({"type": "error", "error": {"type": "gateway_error", "message": f"gateway stream error: {exc}"},

                "session_id": session_id,})



        log_entry({"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(body.get("model") or ""), 

            "type": "messages", "action": decision.get("action"), "rule": decision.get("name"),

            "provider": prov.name, "local": prov.local, "model": model,

            "text_preview": audit_preview, "downgraded_from": downgraded, "stream": True, "filename": _mentioned,

        })

        return StreamingResponse(gen(), media_type="text/event-stream", headers={

            "X-Gateway-Provider": prov.name,

            "X-Gateway-Local": "1" if prov.local else "0",

            "X-Gateway-Rule": _hdr_safe(decision.get("name")),

        })



    try:

        prov = None

        model = ""

        prov, model, notes = resolve(decision, {"model": str(body.get("model") or "")}, request.headers, kind="chat")

        result = await route_messages(body, decision, request.headers)

    except RoutingError as exc:

        log_upstream_failure(request, type_="messages", status=exc.status_code,

                              action=decision.get("action"), rule=decision.get("name"),

                              provider=prov, model=model, text_preview=audit_preview, session_id=session_id)

        raise HTTPException(exc.status_code, detail={"type": (getattr(exc, "type_", "") or "routing_error"),

                                                     "provider": prov.name if prov else "", "message": str(exc),

                                                     "session_id": session_id,})



    result["gateway"] = _meta(prov, model, notes)

    log_entry({"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(body.get("model") or ""), 

        "type": "messages", "action": decision.get("action"), "rule": decision.get("name"),

        "provider": result["gateway"]["provider"], "local": result["gateway"]["local"], "model": model,

        "text_preview": audit_preview, "downgraded_from": downgraded, "filename": _mentioned,

    })

    return JSONResponse(result)





@app.post("/v1/messages/count_tokens")

async def anthropic_count_tokens(request: Request, identity: Identity = Depends(_bind_identity_ctx)):

    """Claude Code 偶尔调用的 token 估算端点；网关层面给近似值即可"""

    body = await request.json()

    text = anthropic_text(body)

    # 中文约 1.5-2 token/字，保守按 len/3 估算，仅用于客户端预算

    return {"input_tokens": max(1, (len(text) + 2) // 3)}





# ---------------------------------------------------------------- OpenAI Responses（ccswitch / Codex CLI）

# Codex 的 wire_api="responses"：POST {base}/v1/responses。放行直通上游，

# 拦截/降级时转 chat.completions 打本地模型，再转回 Responses 格式。



def _codex_session_id(body):
    """codex 等 responses 客户端不发会话头：用对话首条 user 消息做指纹当 session key。

    实测（2026-09-20 dump）：codex 每轮全量重发 input、无 previous_response_id，
    首条 user 消息（environment_context+首问）在对话内不变。
    """
    try:
        inp = body.get("input") if isinstance(body, dict) else None
        if not isinstance(inp, list):
            return None
        for it in inp:
            if not isinstance(it, dict) or it.get("role") != "user":
                continue
            c = it.get("content")
            if isinstance(c, str):
                first = c
            elif isinstance(c, list):
                first = "\n".join(str(b.get("text") or "") for b in c
                                   if isinstance(b, dict) and b.get("type") == "input_text")
            else:
                continue
            if first.strip():
                return "codex-" + hashlib.sha256(first.encode("utf-8", "replace")).hexdigest()[:16]
    except Exception:
        pass
    return None


@app.post("/v1/responses")

async def responses_api(request: Request, identity: Identity = Depends(_rate_limit_messages)):

    body = await request.json()

    # codex 不发 X-Session-ID：回落对话指纹（见 _codex_session_id）
    session_id = extract_session_id(request.headers) or _codex_session_id(body)

    text = responses_text(body)

    audit_preview = _audit_preview_responses(body, text)
    inline_text, inline_bad = await _inline_media_scan("responses", body)
    if inline_text:
        text = text + "\n" + inline_text
    l2_blocks = _select_l2_scopes("responses", body, text)
    file_ctx = extract_responses_filectx(body, text)

    decision, text_findings, l2_result = await _review_text(
        text, request, {"ocr_empty_and_image": True} if inline_bad else None,
        l2_chunks=l2_blocks, file_ctx=file_ctx)



    decision, downgraded = _block_to_fallback(decision)
    _mark_gw_action(request, decision)
    _mark_pre_upstream(request)

    if responses_should_mark(decision.get("name"), decision.get("action"), file_ctx):
        # 附件/内联文件触发拦截后污染 session，后续追问自动走本地（TTL 见 .env）。
        # （P1-1 client 身份标记已移除，2026-09-18）
        _mark_file = file_ctx.get("filename") or "responses-inline"
        if session_id:
            get_session_store().mark_confidential(
                session_id,
                {"name": _mark_file, "rule": decision.get("name"), "action": decision.get("action")},
            )
    if decision.get("action") == "block":

        log_entry({"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(body.get("model") or ""), "type": "responses", "action": "block", "rule": decision.get("name"), "text_preview": audit_preview,

        "session_id": session_id,})

        raise HTTPException(403, detail={"type": "policy_violation", "rule": decision.get("name"),

                                         "message": "请求因机密策略被拦截，未转发至外网"})



    try:

        prov = None

        model = ""

        prov, model, notes = resolve(decision, {"model": str(body.get("model") or "")}, request.headers, kind="chat")

    except RoutingError as exc:

        log_upstream_failure(request, type_="responses", status=exc.status_code,

                              action=decision.get("action"), rule=decision.get("name"),

                              provider=prov, model=model, session_id=session_id)

        raise HTTPException(exc.status_code, detail={"type": (getattr(exc, "type_", "") or "routing_error"), "message": str(exc)})



    if body.get("stream"):

        async def gen():

            sent = False

            try:

                async for chunk in route_responses_stream(body, decision, request.headers):

                    sent = True

                    yield chunk

            except RoutingError as exc:

                if not sent and exc.status_code >= 500:  # 仅瞬态错误(5xx/网络)降级；上游 4xx(密钥/权限/限流)直接透传

                    # 首字节前上游失败：透明降级到本地模型（本地走 chat->responses 事件转换）

                    try:

                        cfg_fc = load_routing()

                        lp = cfg_fc.local_provider()

                        if lp is not None and prov.name != lp.name:

                            fb_decision = {"name": "first_chunk_fallback", "priority": 0,

                                           "action": "route_local", "target": {"provider": lp.name}}

                            async for chunk in route_responses_stream(body, fb_decision, request.headers):

                                sent = True

                                yield chunk

                            log_entry({"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(body.get("model") or ""),

                                "type": "responses", "action": "route_local", "rule": "first_chunk_fallback",

                                "provider": lp.name, "local": True, "text_preview": audit_preview,

                                "downgraded_from": "upstream_error", "stream": True, "session_id": session_id})

                            return

                    except Exception:

                        pass

                # 降级不可用/也失败：显式发 response.failed 事件收尾，Codex 前端可见，不再静默断流
                log_stream_error(request, type_="responses", status=getattr(exc, "status_code", 0) or 502,
                     action=decision.get("action"), rule=decision.get("name"),
                     provider=prov, model=model, kind="upstream",
                     session_id=session_id, text_preview=audit_preview)
                yield _sse_err({"type": "response.failed", "response": {"status": "failed", "error": {"message": str(exc)}}, "session_id": session_id})

            except Exception as exc:

                log_stream_error(request, type_="responses", status=502,
                     action=decision.get("action"), rule=decision.get("name"),
                     provider=prov, model=model, kind="internal",
                     session_id=session_id, text_preview=audit_preview)
                yield _sse_err({"type": "response.failed", "response": {"error": {"message": f"gateway stream error: {exc}"}}, "session_id": session_id,})



        log_entry({"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(body.get("model") or ""), 

            "type": "responses", "action": decision.get("action"), "rule": decision.get("name"),

            "provider": prov.name, "local": prov.local, "model": model,

            "text_preview": audit_preview, "downgraded_from": downgraded, "stream": True, "filename": file_ctx.get("filename") or "",

            "session_id": session_id,

        })

        return StreamingResponse(gen(), media_type="text/event-stream", headers={

            "X-Gateway-Provider": prov.name,

            "X-Gateway-Local": "1" if prov.local else "0",

            "X-Gateway-Rule": _hdr_safe(decision.get("name")),

        })



    try:

        result = await route_responses(body, decision, request.headers)

    except RoutingError as exc:

        log_upstream_failure(request, type_="responses", status=exc.status_code,

                              action=decision.get("action"), rule=decision.get("name"),

                              provider=prov, model=model, session_id=session_id)

        raise HTTPException(exc.status_code, detail={"type": (getattr(exc, "type_", "") or "routing_error"),

                                                     "provider": prov.name if prov else "", "message": str(exc),

                                                     "session_id": session_id,})



    gw = _gateway_meta(decision, prov, model, notes, l2_result, downgraded)

    gw["findings"] = text_findings

    result["gateway"] = gw

    log_entry({"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(body.get("model") or ""), 

        "type": "responses", "action": decision.get("action"), "rule": decision.get("name"),

        "provider": gw["provider"], "local": gw["local"], "model": model,

        "text_preview": audit_preview, "downgraded_from": downgraded, "session_id": session_id, "filename": file_ctx.get("filename") or "",

    })

    return JSONResponse(result)





# ---------------------------------------------------------------- /v1/* 兜底

# 收口用：OpenAI 兼容 SDK 可能打到 /v1/responses、/v1/images、/v1/moderations 等

# 未单独实现的端点。若不兜底，这些请求会绕过审查直连外网 —— 与"全部请求必须过网关"冲突。

# 规则：

#   - action = allow        -> 原样转发到 default_external（非 chat 端点无法降级到本地）

#   - action = block/route_local -> 拒绝（443/403），绝不静默发给外网

#   - AI_GATEWAY_STRICT_EGRESS=true -> 白名单模式，未在白名单内的 /v1/* 一律拒绝

DEFAULT_EGRESS_PATHS = (

    "chat/completions,completions,embeddings,models,responses,"

    "images/generations,moderations"

)





def _egress_whitelist() -> set[str]:

    """每次读取，改环境变量即时生效，不需要重启"""

    return {p.strip() for p in os.getenv("AI_GATEWAY_EGRESS_PATHS", DEFAULT_EGRESS_PATHS).split(",") if p.strip()}





def _strict_egress() -> bool:

    return os.getenv("AI_GATEWAY_STRICT_EGRESS", "false").lower() in ("1", "true", "yes", "on")





def _body_text(payload: Any, _depth: int = 0, _budget: list | None = None) -> str:
    """兜底 /v1/* 请求体取文本：递归展开 dict/list，供 L1 审查。

    覆盖 messages[].content、batch requests[].params、rerank documents/query、
    multipart 表单字段等。单串截断 4000 字符，总量上限 20000 字符，
    深度上限 6，防止超大/畸形 body 拖慢审查。
    """
    if _budget is None:
        _budget = [20000]
    if _budget[0] <= 0 or _depth > 6:
        return ""
    if isinstance(payload, str):
        chunk = payload[:4000]
        _budget[0] -= len(chunk)
        return chunk
    if isinstance(payload, dict):
        parts = []
        for v in payload.values():
            if _budget[0] <= 0:
                break
            parts.append(_body_text(v, _depth + 1, _budget))
        return " ".join(x for x in parts if x)
    if isinstance(payload, (list, tuple)):
        parts = []
        for v in payload:
            if _budget[0] <= 0:
                break
            parts.append(_body_text(v, _depth + 1, _budget))
        return " ".join(x for x in parts if x)
    return ""


@app.post("/v1/files/check")

async def files_check(request: Request, file: UploadFile = File(...), identity: Identity = Depends(_rate_limit_files)):

    data = await file.read()

    filename = file.filename or "unknown"

    # 异步线程池，避免阻塞事件循环（Excel/PDF 解析 20-100ms）

    file_info = await run_in_threadpool(inspect_file, data, filename)

    text = file_info.get("text", "")[:5000]

    text_findings = inspect_text(text)
    _note_risk(text_findings)

    session_id = extract_session_id(request.headers)

    session_conf = _is_session_confidential(session_id)

    ctx = {

        "text": text + " " + filename,

        "session": {"confidential": session_conf},

        "file": {

            "ext": file_info.get("ext", ""),

            "filename": filename,

            "headers": file_info.get("headers", []),

            "sheet_names": file_info.get("sheet_names", []),

            "text": text,

            "watermark": file_info.get("findings", {}).get("text_preview", "") if isinstance(file_info.get("findings"), dict) else "",

            "size": len(data),

        },

        "findings": {**text_findings, **file_info.get("findings", {})},

    }

    ctx["file"]["findings_text"] = str(file_info.get("findings", {}))



    policy_list = load_policy()

    decision = decide(policy_list, ctx)
    if decision.get("action") != "allow":
        request.state.gw_block_reason = f"l1:{decision.get('name') or 'rule'}"



    # L2 小模型二次判定（文件场景：L1 为 allow 时触发，支持分块聚合应对长文档）

    l2_result = None

    if decision.get("action") == "allow":

        chunks = file_info.get("chunks") or [text[:2000]]

        if len(chunks) > 1:

            l2_result = await classify_chunks(chunks, filename=filename, headers=file_info.get("headers", []), sheet_names=file_info.get("sheet_names", []))

        else:

            l2_result = await small_classify(

                text[:2000],

                filename=filename,

                headers=file_info.get("headers", []),

                sheet_names=file_info.get("sheet_names", []),

            )

        _d = _l2_decision(l2_result)
        if _d is not None:
            decision = _d
            request.state.gw_block_reason = f"l2:{(l2_result or {}).get('reason') or 'confidential'}"



    decision, downgraded = _block_to_fallback(decision)
    _mark_gw_action(request, decision)
    _mark_pre_upstream(request)

    try:

        prov = None

        model = ""

        prov, model, notes = resolve(decision, {"model": ""}, None, kind="chat")

    except RoutingError as exc:

        log_upstream_failure(request, type_="file", status=exc.status_code,

                              action=decision.get("action"), rule=decision.get("name"),

                              provider=prov, model=model, filename=filename, ext=file_info.get("ext"), session_id=session_id)

        raise HTTPException(exc.status_code, detail={"type": (getattr(exc, "type_", "") or "routing_error"), "message": str(exc)})



    log_entry({"client_ip": _resolve_client_ip(request), "requested_model": "", 

        "type": "file", "filename": filename, "ext": file_info.get("ext"),

        "action": decision.get("action"), "rule": decision.get("name"),

        "provider": prov.name, "local": prov.local, "model": model,

        "override_denied": notes.get("override_denied") if notes else None,

        "findings": file_info.get("findings"), "l2": l2_result,

        "layer": "L2" if l2_result and decision.get("name") in ("small_model_confidential", "l2_unavailable") else "L1",

        "downgraded_from": downgraded,

        "session_id": session_id,

    })

    if decision.get("action") in ("route_local", "block"):
        # 命中即标 session（若有），后续同会话请求粘性走本地。
        # （P1-1 client 身份标记已移除，2026-09-18）
        if session_id:
            get_session_store().mark_confidential(

                session_id,

                {"name": filename, "rule": decision.get("name"), "action": decision.get("action")},

            )



    if decision.get("action") == "block":

        raise HTTPException(403, detail={"type": "policy_violation", "rule": decision.get("name"), "findings": file_info.get("findings")})



    gw = _gateway_meta(decision, prov, model, notes, l2_result, downgraded)

    if l2_result:

        gw["l2"] = l2_result



    return {

        "filename": filename,

        "ext": file_info.get("ext"),

        "gateway": gw,

        "findings": file_info.get("findings"),

        "text_findings": text_findings,

        "preview": file_info.get("findings", {}).get("sample_preview") or file_info.get("findings", {}).get("text_preview", "")[:800],

        "message": "审查未通过：已自动转本地模型，文件内容不会出境" if gw["action"] == "route_local" else "审查通过：可走外网模型",

    }





@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])

async def v1_proxy(path: str, request: Request, identity: Identity = Depends(_bind_identity_ctx)):

    """

    未单独实现的 /v1/* 端点：先审查，通过才转发到外网 provider。

    必须注册在所有具体 /v1 路由之后，否则会把 /v1/files/check 之类吞掉。

    """

    session_id = extract_session_id(request.headers)

    if _strict_egress() and path not in _egress_whitelist():

        raise HTTPException(403, detail={"type": "egress_denied", "path": path,

                                         "message": "严格出网模式：该端点不在白名单内"})



    if request.method in ("POST", "PUT"):
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype.startswith("multipart/"):
            # 上游 files / audio 转写等多为 multipart：request.json() 会直接抛
            # JSONDecodeError（之前是未处理 500）。只取表单文本字段+文件名送审，
            # 文件字节本身不审（走 /v1/files/check 做内容预检）。
            try:
                form = await request.form()
            except Exception:
                raise HTTPException(400, detail={"type": "invalid_request", "path": path,
                                                 "message": "multipart 解析失败"})
            fields: dict[str, Any] = {}
            for _k, _v in form.multi_items():
                if hasattr(_v, "filename"):
                    fields[_k] = getattr(_v, "filename", "") or ""
                else:
                    fields[_k] = _v
            text = (_body_text(fields) + " " + path).strip()
        else:
            try:
                payload = await request.json()
            except Exception:
                raise HTTPException(400, detail={"type": "invalid_request", "path": path,
                                                 "message": "请求体不是合法 JSON"})
            text = _body_text(payload)
    else:
        text = path

    findings = inspect_text(text)
    _note_risk(findings)

    decision = decide(load_policy(), {

        "text": text,

        "file": {"ext": "", "filename": "", "headers": [], "sheet_names": [], "text": ""},

        "findings": findings,

        "session_id": session_id,

        "session": {"confidential": _is_session_confidential(session_id)},

    })
    if decision.get("action") != "allow":
        request.state.gw_block_reason = f"l1:{decision.get('name') or 'rule'}"

    decision, _ = _block_to_fallback(decision)
    _mark_gw_action(request, decision)
    _mark_pre_upstream(request)



    # 非 chat 端点无法"降级到本地模型"（本地大模型没有 /v1/images 之类接口），

    # 所以审查不通过只能拒绝，绝不静默发给外网。

    if decision.get("action") != "allow":

        log_entry({"client_ip": _resolve_client_ip(request), "requested_model": "", "type": "egress_blocked", "path": path, "action": decision.get("action"),

                   "rule": decision.get("name"), "text_preview": text[:200]})

        raise HTTPException(403, detail={

            "type": "policy_violation", "path": path, "rule": decision.get("name"),

            "message": "审查未通过，且该端点无本地模型可降级，已拒绝外发",

        })



    cfg = load_routing()

    prov = cfg.external_provider()

    if prov is None or not prov.base_url:

        raise HTTPException(503, detail={"type": "routing_error", "message": "未配置外网 provider"})

    try:
        # T44/P1：/v1/* 兜底代理原来自行读 X-Upstream-Api-Key，是第 4 个凭据入口
        # （方案只数了 3 个）⇒ 统一收口到 upstream_key()，否则切 gateway_only 后
        # 代理路径仍收客户端凭据。
        ukey = upstream_key(request.headers, prov=prov)
    except RoutingError as _uk_exc:
        raise HTTPException(_uk_exc.status_code, detail={
            "type": getattr(_uk_exc, "type_", "") or "routing_error",
            "provider": prov.name,
            "message": str(_uk_exc)})

    # T44 修：``prov = cfg.external_provider()`` 恒为外网，故只看本次解析出的凭据。
    # 原 ``prov.configured or ukey`` 在网关持有 key 后恒真 ⇒ 未登记身份白吃公司额度。
    if not ukey:

        raise HTTPException(503, detail={"type": "provider_not_configured", "provider": prov.name,

                                         "message": f"网关未配置 {prov.name} 凭据，请联系管理员（{prov.api_key_env}；不再支持随请求携带上游密钥）"})



    body = await request.body()

    client = get_proxy_client(prov.local)

    try:

        r = await client.request(

            request.method, prov.endpoint("proxy", custom_path=path),

            headers=prov.headers(ukey), content=body,

            params=dict(request.query_params), timeout=prov.timeout,

        )

    except httpx.HTTPError as exc:

        raise HTTPException(502, detail={"type": (getattr(exc, "type_", "") or "routing_error"), "provider": prov.name if prov else "", "message": str(exc),

        "session_id": session_id,})



    log_entry({"client_ip": _resolve_client_ip(request), "requested_model": "", "type": "egress", "path": path, "provider": prov.name, "local": prov.local,

               "rule": decision.get("name"), "text_preview": text[:200]})

    return Response(

        content=r.content, status_code=r.status_code,

        media_type=r.headers.get("content-type", "application/json"),

        headers={"X-Gateway-Provider": _hdr_safe(prov.name), "X-Gateway-Local": "1" if prov.local else "0",

                 "X-Gateway-Rule": _hdr_safe(decision.get("name"))},

    )






_ADMIN_LOGIN_PAGE_TMPL = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>Secure Gateway — 管理登录</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
  :root{--bg:#F8FAFC;--panel:#FFFFFF;--line:#E2E8F0;--line-strong:#CBD5E1;--muted:#64748B;--text:#0F172A;--accent:#1E40AF;--accent-600:#1E3A8A;--err:#DC2626}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:var(--bg);color:var(--text);font-family:Inter,ui-sans-serif,system-ui;-webkit-font-smoothing:antialiased}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:32px;width:340px;box-shadow:0 8px 30px rgba(15,23,42,.08)}
  .logo{width:44px;height:44px;border-radius:11px;background:linear-gradient(135deg,#3B82F6,#1E40AF);display:grid;place-items:center;color:#fff;box-shadow:0 4px 14px rgba(30,64,175,.35);margin:0 auto 14px}
  h1{font-size:18px;margin:0 0 6px;text-align:center}
  p.sub{margin:0 0 20px;font-size:13px;color:var(--muted);text-align:center}
  input{width:100%;padding:10px 12px;border-radius:10px;border:1px solid var(--line-strong);background:#fff;color:var(--text);font-size:14px}
  input::placeholder{color:#94A3B8}
  input:focus{outline:none;border-color:#3B82F6;box-shadow:0 0 0 3px rgba(59,130,246,.35)}
  button{width:100%;margin-top:12px;padding:10px;border:0;border-radius:10px;background:var(--accent);color:#fff;font-weight:700;font-size:14px;cursor:pointer;box-shadow:0 2px 8px rgba(30,64,175,.25);transition:background 150ms,transform 150ms}
  button:hover{background:var(--accent-600);transform:translateY(-1px)}
  .err{color:var(--err);font-size:13px;margin:10px 0 0;text-align:center}
</style>

</head>
<body>
<form class="card" method="post" action="/admin/login?next={NEXT}">
  <div class="logo" aria-hidden="true"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2"><path d="M12 3l7 4v5c0 5-3.5 7.5-7 9-3.5-1.5-7-4-7-9V7l7-4z"/><path d="M9 12l2 2 4-4"/></svg></div>
  <h1>Secure Gateway</h1>
  <p class="sub">管理面板需要登录（7 天有效）</p>
  <input type="password" name="password" placeholder="管理员密码" autofocus autocomplete="current-password" required>
  <button type="submit">登录</button>
  {ERR}
</form>
</body>
</html>
"""


def _admin_login_page(error: str = "", next_url: str = "/admin") -> str:
    err_html = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return (_ADMIN_LOGIN_PAGE_TMPL
            .replace("{ERR}", err_html)
            .replace("{NEXT}", quote(next_url, safe="")))


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request, next: Optional[str] = None):
    if not _admin_password():
        # 未启用密码：无需登录
        return RedirectResponse("/admin/app", status_code=303)
    return HTMLResponse(_admin_login_page(next_url=_safe_next(next)))


@app.post("/admin/login")
async def admin_login_submit(request: Request, next: Optional[str] = None):
    """浏览器 form 提交；脚本可用 JSON：POST /admin/login {"password": "..."}"""
    if not _admin_password():
        return RedirectResponse("/admin/app", status_code=303)
    # 兼容两种提交方式：x-www-form-urlencoded（浏览器）与 JSON（脚本）
    ctype = (request.headers.get("content-type", "") or "").lower()
    pw = ""
    if "json" in ctype:
        try:
            body = json.loads((await request.body()) or b"{}")
            pw = str((body or {}).get("password", ""))
        except (ValueError, TypeError):
            pw = ""
    else:
        try:
            form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
            pw = (form.get("password") or [""])[0]
        except Exception:
            pw = ""
    ip = _resolve_client_ip(request) or "unknown"
    if _admin_login_throttled(ip):
        raise HTTPException(429, "too many login attempts, retry in 5 minutes")
    if not _verify_admin_pw(pw):
        _admin_login_fail(ip)
        return HTMLResponse(
            _admin_login_page(error="密码错误", next_url=_safe_next(next)),
            status_code=401,
        )
    _admin_login_fails.pop(ip, None)
    resp = RedirectResponse(_safe_next(next), status_code=303)
    resp.set_cookie(
        _ADMIN_COOKIE,
        _admin_session_token(),
        max_age=_ADMIN_SESSION_TTL_S,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return resp


@app.get("/admin/logout")
async def admin_logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(_ADMIN_COOKIE, path="/")
    return resp


def _to_beijing(value):
    """审计时间落库为 UTC（naive），展示时 +8h 对齐北京时间。解析失败原样返回。

    只在**读/展示**路径调用（entries API / CSV 导出），存储与 since/until 过滤
    仍按 UTC，避免口径漂移；服务器时区是 UTC，别把这里挪到写路径。
    """
    if not value:
        return value
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.strptime(str(value).strip().replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S")
        return (dt + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return value


@app.get("/admin/audit/entries", dependencies=[Depends(admin_page_auth)])

async def audit_entries_api(request: Request, ip: Optional[str] = None, token: Optional[str] = None, action: Optional[str] = None, limit: Optional[str] = None, type: Optional[str] = None, page: Optional[str] = None, since: Optional[str] = None, until: Optional[str] = None, provider: Optional[str] = None, rule: Optional[str] = None, q: Optional[str] = None, sort: Optional[str] = None):

    # Filtering: ip (client_ip) and action

    store = get_audit_store()

    # 固定每页 50 条；接受 str/int/None

    try:

        limit = int(limit) if str(limit).strip() not in ("", "None", None) else 50

    except (ValueError, TypeError):

        limit = 50

    limit = 50

    try:

        page = int(page) if str(page).strip() not in ("", "None", None) else 1

    except (ValueError, TypeError):

        page = 1

    page = max(1, page)

    try:

        page = int(page)

    except Exception:

        page = 1

    page = max(1, page)

    # 为分页需要足够数据，固定拉取 5000 条（覆盖 100页*50），足够仪表盘使用且不影响性能

    fetch_n = 5000

    # 仪表盘筛选：始终基于 Redis 热缓存 tail（oldest-first），内存过滤后分页，保持与表格一致

    all_entries = store.tail(fetch_n)

    entries = all_entries

    if ip:

        ip = ip.strip()

        if ip:

            entries = [e for e in entries if ip in (e.get("client_ip") or "")]

    if token:

        token = token.strip()

        if token:

            _tl = token.lower()

            entries = [e for e in entries if _tl in str(e.get("token_masked") or "").lower()]

    if action:

        action = action.strip()

        if action:

            entries = [e for e in entries if e.get("action") == action]

    if provider:

        provider = provider.strip()

        if provider:

            entries = [e for e in entries if (e.get("provider") or "") == provider]

    if rule:

        rule = rule.strip()

        if rule:

            entries = [e for e in entries if (e.get("rule") or "") == rule]

    # 时间范围：since/until 支持 1h/24h/7d/30d 或 YYYY-MM-DD 或 ISO

    def _parse_t(s, is_until: bool = False):

        if not s or not str(s).strip():

            return None

        s = str(s).strip()

        if s.endswith("h") or s.endswith("d"):

            try:

                n = int(s[:-1])

                delta = timedelta(hours=n) if s.endswith("h") else timedelta(days=n)

                return datetime.now() - delta

            except Exception:

                pass

        try:

            if "T" in s:

                return to_local_naive(datetime.fromisoformat(s.replace("Z", "+00:00")))

            if len(s) == 10:

                dt = datetime.strptime(s, "%Y-%m-%d")

                if is_until:

                    return dt.replace(hour=23, minute=59, second=59, microsecond=999999)

                return dt

            return datetime.fromisoformat(s) if " " in s else datetime.strptime(s, "%Y-%m-%d %H:%M:%S")

        except Exception:

            try:

                return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")

            except Exception:

                return None



    since_dt = _parse_t(since, is_until=False)

    until_dt = _parse_t(until, is_until=True)

    if since_dt or until_dt:

        def _to_dt(v):

            if not v:

                return None

            if isinstance(v, datetime):

                return to_local_naive(v)

            v = str(v).strip()

            try:

                if "T" in v:

                    return to_local_naive(datetime.fromisoformat(v.replace("Z", "+00:00")))

                return datetime.fromisoformat(v)

            except Exception:

                try:

                    return datetime.strptime(v.split(".")[0], "%Y-%m-%d %H:%M:%S")

                except Exception:

                    try:

                        return datetime.strptime(v, "%Y-%m-%d")

                    except Exception:

                        return None



        tmp = []

        for e in entries:

            t = _to_dt(e.get("time") or e.get("ts") or "")

            if not t: 

                continue

            if since_dt and t < since_dt:

                continue

            if until_dt and t > until_dt:

                continue

            tmp.append(e)

        entries = tmp

    if q:

        q=q.strip()

        if q:

            ql=q.lower()

            def _is_local(e):
                v = e.get("local")
                return v is True or v in (1, "1", "true", "True")
            # “local/external”只是前端渲染标签（local 布尔），落库字段里没有这个串；
            # 搜这几个词时按路由归属匹配，否则 external 永远搜不到（与表格 Tag 口径一致：缺 local 视为 external）。
            q_route = None
            if ql in ("external", "外部", "外发"):
                q_route = False
            elif ql in ("local", "本地"):
                q_route = True
            def _match(e):
                if q_route is not None and _is_local(e) == q_route:
                    return True
                for k in ("text_preview","filename","requested_model","model","provider","rule","client_ip","type","token_masked"):

                    v=e.get(k)

                    if v and ql in str(v).lower():

                        return True

                return False

            entries=[e for e in entries if _match(e)]

    # 排序：默认严格 time_desc（最新在前），支持 time_asc / provider / rule / type
    def _t_key(x):
        return str(x.get("time") or x.get("ts") or "").replace("T", " ")[:19]
    _sort = (sort or "").strip()
    if _sort == "provider":
        entries = sorted(entries, key=lambda x: (x.get("provider") or "", _t_key(x)), reverse=True)
    elif _sort == "rule":
        entries = sorted(entries, key=lambda x: (x.get("rule") or "", _t_key(x)), reverse=True)
    elif _sort == "type":
        entries = sorted(entries, key=lambda x: (x.get("type") or "", _t_key(x)), reverse=True)
    elif _sort == "time_asc":
        entries = sorted(entries, key=_t_key)
    else:
        entries = sorted(entries, key=_t_key, reverse=True)

    # KPI 统计基于筛选后全量（分页前）

    total_filtered = len(entries)

    total_pages = max(1, (total_filtered + limit - 1) // limit)

    if page > total_pages:

        page = total_pages

    # 分页切片（排序已在上方显式完成，默认 time_desc 时 page=1 即最新一页、页内最新在前）
    start = (page-1)*limit
    end = start+limit
    page_entries = entries[start:end]



    # --- stats for header KPI ---  # 基于筛选后全量（分页前）

    total = total_filtered

    c_allow = sum(1 for e in entries if e.get("action")=="allow")

    c_route = sum(1 for e in entries if is_local_route(e.get("action")))

    c_block = sum(1 for e in entries if _entry_blocked(e))

    c_other = total - c_allow - c_route - c_block

    c_local = sum(1 for e in entries if e.get("local"))

    c_ext = total - c_local



    def _plain(v):
        if v is None or isinstance(v, (str, int, float, bool)):
            return v
        return str(v)

    _keys = ("time", "type", "rule", "action", "provider", "local", "model",
             "requested_model", "downgraded_from", "filename", "text_preview", "client_ip", "token_masked", "session_id")
    return JSONResponse({
        "total": total, "page": page, "pages": total_pages, "limit": limit,
        "c_allow": c_allow, "c_route": c_route, "c_block": c_block,
        "c_other": c_other, "c_local": c_local, "c_ext": c_ext,
        "entries": [{k: (_to_beijing(e.get(k)) if k == "time" else _plain(e.get(k))) for k in _keys} for e in page_entries],
    })


@app.get("/admin", dependencies=[Depends(admin_page_auth)])
async def admin_legacy_redirect():
    """旧审计 HTML 页已退役，统一跳转 Admin Console SPA。"""
    return RedirectResponse("/admin/app", status_code=302)


@app.get("/admin/csv")

async def export_csv(

    _ip: str = Depends(admin_auth),

    since: Optional[str] = None,       # ISO 8601 or "YYYY-MM-DD" or "1h"/"24h"/"7d"

    until: Optional[str] = None,       # ISO 8601 or "YYYY-MM-DD"; default = now

    action: Optional[str] = None,      # allow | route_local | block

    rule: Optional[str] = None,

    type: Optional[str] = None,        # chat | file | messages | responses | embedding

    ip: Optional[str] = None,          # client_ip filter

    token: Optional[str] = None,       # caller key (masked) filter

    q: Optional[str] = None,           # keyword filter

    source: Optional[str] = "auto",    # auto | redis | mysql  (auto = mysql if since/until given)

    limit: int = 5000,

):

    """Export audit log as CSV.



    - No since/until: pulls hot cache (Redis, fast, last 5000).

    - With since/until: queries MySQL for time-range + filters (90-day window).

    - Time shortcuts: "1h", "24h", "7d", "30d" relative to now.

    """

    import csv, io

    from datetime import datetime, timedelta

    store = get_audit_store()



    def _parse(t: Optional[str], default: Optional[datetime] = None, is_until: bool = False) -> Optional[datetime]:

        if not t:

            return default

        t = str(t).strip()

        if t.endswith("h") or t.endswith("d"):

            try:

                n = int(t[:-1])

                delta = timedelta(hours=n) if t.endswith("h") else timedelta(days=n)

                return datetime.now() - delta

            except ValueError:

                pass

        try:

            if "T" in t:

                return to_local_naive(datetime.fromisoformat(t.replace("Z", "+00:00")))

            if len(t) == 10:

                dt = datetime.strptime(t, "%Y-%m-%d")

                if is_until:

                    return dt.replace(hour=23, minute=59, second=59, microsecond=999999)

                return dt

            return datetime.fromisoformat(t) if " " in t else datetime.strptime(t, "%Y-%m-%d %H:%M:%S")

        except ValueError:

            return default



    use_mysql = (source == "mysql") or (source == "auto" and (since or until or action or rule or type or ip or q or token))



    cols = ["time", "type", "rule", "action", "provider", "local", "model", "requested_model", "downgraded_from", "filename", "text_preview", "id", "client_ip", "token_masked", "session_id"]

    def _csv_safe(v):  # 防 Excel 公式注入：=,+,-,@ 开头加单引号前缀
        if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
            return "'" + v
        return v

    buf = io.StringIO()

    w = csv.DictWriter(buf, fieldnames=cols)

    w.writeheader()



    if use_mysql:

        since_dt = _parse(since, is_until=False)

        until_dt = _parse(until, default=datetime.now(), is_until=True)

        rows = store.query_mysql(since=since_dt, until=until_dt, action=action, rule=rule, type_=type, limit=limit)

        if ip:

            ip = ip.strip()

            rows = [r for r in rows if ip in (r.get("client_ip") or "")]

        if q:

            ql = q.strip().lower()

            if ql:

                rows = [r for r in rows if any(ql in str(r.get(k) or "").lower() for k in ("text_preview","filename","requested_model","model","provider","rule","client_ip","type","token_masked"))]

        if token:

            _tl = token.strip().lower()

            if _tl:

                rows = [r for r in rows if _tl in str(r.get("token_masked") or "").lower()]

        for r in rows:

            if "error" in r:

                continue

            _row = {

                "time": _to_beijing(r.get("ts", "")),

                "type": r.get("type", ""),

                "rule": r.get("rule", ""),

                "action": r.get("action", ""),

                "provider": r.get("provider", "") or "",

                "local": "1" if r.get("local_flag") else "0",

                "model": r.get("model", "") or "",

                "requested_model": r.get("requested_model", "") or "",

                "downgraded_from": r.get("downgraded_from", "") or "",

                "filename": r.get("filename", "") or "",

                "text_preview": (r.get("text_preview", "") or "")[:200],

                "id": r.get("req_id", ""),

                "client_ip": r.get("client_ip", "") or "",

                "token_masked": r.get("token_masked", "") or "",

            }
            w.writerow({k: _csv_safe(v) for k, v in _row.items()})

    else:
        # Redis 热缓存路径：显式按时间升序导出，保证确定性（MySQL 分支已 ORDER BY ts）
        rows_hot = sorted(store.all(), key=lambda x: str(x.get("time") or x.get("ts") or "").replace("T", " ")[:19])
        for e in rows_hot:
            w.writerow({k: (_csv_safe(_to_beijing(e.get(k, ""))) if k == "time" else (_csv_safe(e.get(k, "")) if k != "local" else (1 if e.get(k) else 0))) for k in cols})



    return HTMLResponse(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=gateway_audit.csv"})





@app.get("/admin/metrics")

async def metrics_endpoint(_ip: str = Depends(admin_auth)):

    return Response(content=get_metrics().render(), media_type=f"text/plain; version={GATEWAY_VERSION}")


@app.get("/admin/metrics/data", dependencies=[Depends(admin_page_auth)])
async def metrics_data():
    return JSONResponse(get_metrics().export_panel())

@app.get("/admin/metrics/timeseries", dependencies=[Depends(admin_page_auth)])
async def metrics_timeseries():
    from .metrics_ring import get_ring
    ring = get_ring()
    panel = get_metrics().export_panel()
    try:
        from .circuit_breaker import get_breaker
        panel["circuit_providers"] = {p: s.get("state")
                                      for p, s in get_breaker().snapshot().items()}
    except Exception:
        panel["circuit_providers"] = {}
    try:
        _ast = get_audit_store()
        panel["audit"] = {"backend": _ast.backend(), "mysql": _ast.mysql_status()}
    except Exception:
        panel["audit"] = {"backend": "unknown", "mysql": "unknown"}
    panel["boot_ts"] = round(get_metrics()._start, 1)
    # 环尾可能是重启断点（只有 t/gap，无 l2_up），往前找最后一个真实 tick
    _last = next((p for p in reversed(ring.points) if "l2_up" in p), None)
    panel["l2_up"] = _last["l2_up"] if _last else None
    panel["ring"] = ring.status()
    return JSONResponse({"interval_s": ring.tick_s, "ticks": list(ring.points), "panel": panel})



@app.get("/admin/metrics/panel", dependencies=[Depends(admin_page_auth)])
async def metrics_panel_redirect():
    """旧指标面板 HTML 页已退役，统一跳转 Admin Console SPA。"""
    return RedirectResponse("/admin/app", status_code=302)


@app.get("/admin/circuit")

async def circuit_state(_ip: str = Depends(admin_auth)):

    from .circuit_breaker import get_breaker

    return JSONResponse(get_breaker().snapshot())





@app.post("/admin/circuit/reset")

async def circuit_reset(_ip: str = Depends(admin_auth)):

    """手动重置所有/指定 provider 的熔断器。仅供运维使用。"""

    from .circuit_breaker import get_breaker

    b = get_breaker()

    b.reset()

    return JSONResponse({"status": "reset", "snapshot": b.snapshot()})





@app.get("/admin/audit/status")

async def audit_status(_ip: str = Depends(admin_auth)):

    """Show audit backend status (Redis hot cache + MySQL long-term)."""

    import re as _re
    store = get_audit_store()
    # 密码脱敏：redis url 与 backend 串里都带明文密码，不进管理 API 响应
    _redact = lambda u: _re.sub(r"://[^@/]*@", "://***@", str(u or ""))

    return {

        "backend": _redact(store.backend()),

        "redis": {

            "connected": store._r is not None,

            "url": _redact(store._r_url),

        },

        "mysql": store.mysql_status(),

    }





@app.post("/admin/audit/cleanup")

async def audit_cleanup(_ip: str = Depends(admin_auth), days: int = 90):

    """Delete audit rows older than N days (default 90). Returns row count deleted."""

    store = get_audit_store()

    deleted = store.cleanup_old(days=days)

    return {"deleted": deleted, "days": days}





@app.post("/admin/audit/reset")

async def audit_reset(_ip: str = Depends(admin_auth)):

    """Reset audit backends after DB/Redis fix (clears disabled flag)."""

    store = get_audit_store()

    result = store.reset()

    import time, uuid

    test_entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "id": str(uuid.uuid4())[:8], "type": "audit_reset", "action": "reset", "rule": "admin", "text_preview": "audit reset triggered"}

    store.append(test_entry)

    return result







def _parse_window_ts(v, now=None):
    """时间窗参数 -> 本地墙钟朴素 datetime（唯一实现）。

    支持 "1h"/"7d" 相对窗与 ISO/日期串。ISO 一律先解析成 aware 再 to_local_naive()
    落本地：审计明细的 time 是 time.strftime 本地墙钟，两侧必须同一时基
    （原实现把 UTC 串直接当本地用，窗口整体偏一个时区）。
    """
    if not v:
        return None
    v = str(v).strip()
    now = now or datetime.now()
    if v.endswith("h") or v.endswith("d"):
        try:
            n_ = int(v[:-1])
            return now - (timedelta(hours=n_) if v.endswith("h") else timedelta(days=n_))
        except (TypeError, ValueError):
            pass
    try:
        if "T" in v:
            return to_local_naive(datetime.fromisoformat(v.replace("Z", "+00:00")))
        if len(v) == 10:
            return datetime.strptime(v, "%Y-%m-%d")
        return datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _analytics_aggregate(entries, since=None, until=None):
    """按所选时间跨度聚合；趋势固定 60 桶，桶宽 = 跨度/60（自适应标签粒度）。"""
    from datetime import datetime, timedelta
    now0 = datetime.now()

    def _t(v):
        return _parse_window_ts(v, now=now0)

    end = _t(until) or now0
    start = _t(since)
    if start is None:
        start = end - timedelta(hours=1)

    filtered = []
    for e in entries:
        t = _t(e.get("time") or "")
        if t is None:
            t = _t(str(e.get("time") or "")[:19].replace("T", " "))
        if t is None:
            continue
        if t < start or t >= end:
            continue
        filtered.append((t, e))
    entries = [e for _, e in filtered]

    total = len(entries)
    c_allow = sum(1 for e in entries if e.get("action") == "allow")
    c_route = sum(1 for e in entries if is_local_route(e.get("action")))
    c_block = sum(1 for e in entries if _entry_blocked(e))
    c_other = total - c_allow - c_route - c_block
    c_local = sum(1 for e in entries if e.get("local"))
    rule_counts = {}
    for e in entries:
        r = e.get("rule") or ""
        rule_counts[r] = rule_counts.get(r, 0) + 1
    rule_top = sorted(rule_counts.items(), key=lambda x: -x[1])[:10]
    risk_buckets = {"0-19": 0, "20-39": 0, "40-59": 0, "60-79": 0, "80-99": 0, "100+": 0}
    for e in entries:
        f = e.get("findings") or {}
        try:
            _rs = e.get("risk_score")
            if _rs is None:
                # 旧条目无顶层 risk_score，回退 findings（文件流特征字典）
                s = int(f.get("weighted_pii_score") or f.get("risk_score") or 0)
            else:
                s = int(_rs)
        except (TypeError, ValueError):
            s = 0
        if s <= 19:
            risk_buckets["0-19"] += 1
        elif s <= 39:
            risk_buckets["20-39"] += 1
        elif s <= 59:
            risk_buckets["40-59"] += 1
        elif s <= 79:
            risk_buckets["60-79"] += 1
        elif s <= 99:
            risk_buckets["80-99"] += 1
        else:
            risk_buckets["100+"] += 1
    n = 60
    width = max((end - start).total_seconds(), float(n)) / n
    counts = [0] * n
    rout = [0] * n
    blk = [0] * n
    for t, e in filtered:
        idx = int((t - start).total_seconds() // width)
        if 0 <= idx < n:
            counts[idx] += 1
            if is_local_route(e.get("action")):
                rout[idx] += 1
            elif _entry_blocked(e):
                blk[idx] += 1
    # 时段桶标签 +8h 对齐北京时间（分桶 idx 仍用 UTC 的 t/start，只平移标签）
    if (end - start).total_seconds() <= 26 * 3600:
        slots = [(start + timedelta(hours=8, seconds=i * width)).strftime("%H:%M") for i in range(n)]
    else:
        slots = [(start + timedelta(hours=8, seconds=i * width)).strftime("%m-%d") for i in range(n)]
    pc = {}
    for e in entries:
        p = e.get("provider") or "unknown"
        pc[p] = pc.get(p, 0) + 1
    provider_top = sorted(pc.items(), key=lambda x: -x[1])[:8]
    ap = {}
    for e in entries:
        p = e.get("provider") or "unknown"
        a = e.get("action") or "unknown"
        ap.setdefault(p, {})
        ap[p][a] = ap[p].get(a, 0) + 1
    return {"total": total, "c_allow": c_allow, "c_route": c_route, "c_block": c_block,
            "c_local": c_local, "c_ext": total - c_local, "c_other": c_other,
            "override_denied": sum(1 for e in entries if e.get("override_denied")),
            "fallback": sum(1 for e in entries if (e.get("downgraded_from") or "")),
            "rule_top": [{"rule": k, "count": v} for k, v in rule_top],
            "risk_buckets": risk_buckets, "time_slots": slots, "time_counts": counts,
            "time_route": rout, "time_block": blk,
            "provider_top": [{"provider": k, "count": v} for k, v in provider_top],
            "action_provider": {p: dict(a) for p, a in ap.items()}}


@app.get("/admin/analytics/data", dependencies=[Depends(admin_page_auth)])
async def analytics_data(since: Optional[str] = None, until: Optional[str] = None, limit: Optional[int] = None):
    limit = limit or 5000
    store = get_audit_store()
    entries = store.tail(limit)
    return JSONResponse(_analytics_aggregate(entries, since=since, until=until))

@app.get("/admin/analytics", dependencies=[Depends(admin_page_auth)])
async def analytics_redirect():
    """旧可视化 HTML 页已退役，统一跳转 Admin Console SPA。"""
    return RedirectResponse("/admin/app", status_code=302)


from . import admin_api as _admin_api


def _admin_secret_override() -> Optional[str]:
    """DB 管理员凭据存在时，会话签名密钥从密码哈希派生。
    改密码 -> 哈希变 -> 密钥变 -> 全部已登录 session 立即失效。"""
    try:
        return _admin_api.store_session_secret()
    except Exception:
        return None


def _verify_admin_pw(pw: str) -> bool:
    """登录密码校验：DB 凭据优先（scrypt 哈希），无凭据回退 env 明文比对。"""
    try:
        return _admin_api.verify_admin_password(pw)
    except Exception:
        return False


app.include_router(_admin_api.router, dependencies=[Depends(admin_auth)])


# SPA 静态资源（/static/admin/assets/*）；index.html 由 /admin/app 路由托管。
# 目录存在才挂载，避免未构建环境下启动报错。
_app_static_dir = os.path.join("static", "admin")
if os.path.isdir(_app_static_dir):
    app.mount("/static/admin", StaticFiles(directory=_app_static_dir), name="admin_static")


# SPA 静态资源（/static/admin/assets/*）；index.html 由 /admin/app 路由托管。
# 目录存在才挂载，避免未构建环境下启动报错。
_app_static_dir = os.path.join("static", "admin")
if os.path.isdir(_app_static_dir):
    app.mount("/static/admin", StaticFiles(directory=_app_static_dir), name="admin_static")


@app.get("/admin/app", response_class=HTMLResponse, dependencies=[Depends(admin_page_auth)])
async def admin_console_spa():
    """Admin Console SPA（React 构建产物 static/admin/）。"""
    p = os.path.join("static", "admin", "index.html")
    if os.path.exists(p):
        return FileResponse(p)
    return HTMLResponse(
        "<h3>Admin Console 未构建</h3><p>在 web/admin/ 下执行 npm install && npm run build</p>",
        status_code=404,
    )


def _mark_pre_upstream(request: Request) -> None:
    """Mark gateway-internal/upstream boundary via perf_counter."""
    request.state._t_pre_upstream = time.perf_counter()


def _gw_internal_ms(request: Request, t0: float) -> int:
    """Gateway-internal latency (L1+L2) in ms. Falls back to full duration if unmarked."""
    pre = getattr(request.state, "_t_pre_upstream", None)
    if pre is None:
        return int((time.perf_counter() - t0) * 1000)
    return int((pre - t0) * 1000)


def _upstream_ms(request: Request, t0: float) -> int:
    """Upstream model call latency in ms. 0 if not marked (no upstream call)."""
    pre = getattr(request.state, "_t_pre_upstream", None)
    if pre is None:
        return 0
    return int((time.perf_counter() - pre) * 1000)


@app.middleware("http")
async def _admin_ip_filter_mw(request: Request, call_next):
    """KEY 黑白名单拦截（需求 2）+ 请求明细异步落库（需求 3.x）。
    白名单放行、黑名单 403，仅作用于 /v1/* 出口路径；落库走单线程 executor
    fire-and-forget，store/统计任何故障都不阻断主链路。"""
    from .stats_service import log_request_async, peek_model_from_body, peek_provider_from_response, provider_from_json_bytes

    path = request.url.path
    if not path.startswith("/v1/"):
        return await call_next(request)

    # 审计归因：/v1/* 请求的调用方 key 落 ContextVar，log_entry 自动补 token_masked
    _ck = ""
    try:
        _ck = (request.headers.get("authorization", "") or "")
        if _ck.startswith("Bearer "):
            _ck = _ck[7:].strip()
        if not _ck:
            _ck = (request.headers.get("x-api-key", "") or "").strip()
        _CURRENT_CLIENT_KEY.set(_ck)
        # P0：登记表身份先行注入 request.state，供中间件之后的消费点读取。
        # 注入失败绝不能影响主链路。
        if _ck and _identity_v2():
            _ident = _local_identity(_ck)
            if _ident is not None:
                request.state.gw_identity = _ident
    except Exception:
        pass

    # 黑名单判定与审计归因同口径（Bearer / x-api-key 两头都查）——旧实现只看
    # Bearer，拉黑 key 改用 x-api-key 头即绕 403，而 Claude Code / Anthropic
    # 协议客户端发的正是 x-api-key（2026-09-18 修）
    try:
        verdict = _admin_api.key_verdict(_ck) if _ck else None
    except Exception:
        verdict = None

    if verdict == "black":
        log_entry({
            "client_ip": _resolve_client_ip(request),
            "requested_model": "",
            "type": "ip_blocked",
            "action": "block",
            "rule": "key_blacklist",
            "path": path,
        })
        try:
            log_request_async(
                _admin_api.get_admin_store(),
                client_ip=_resolve_client_ip(request),
                action="block",
                status_code=403,
                blocked_reason="key_blacklist",
            )
        except Exception:
            pass
        return JSONResponse(
            {"error": {"type": "key_blocked", "message": "client key is blacklisted"}},
            status_code=403,
        )

    # 明细抓取：仅 POST（推理入口），GET /v1/models 等轮询不记
    _model, _key_name, _key_id, _logged = "", "", 0, False
    if request.method == "POST":
        try:
            _body = await request.body()
            _model = peek_model_from_body(_body)
        except Exception:
            _body = b""
        try:
            _raw = ""
            _authz = request.headers.get("authorization", "") or ""
            if _authz.startswith("Bearer "):
                _raw = _authz.split(" ", 1)[1].strip()
            if not _raw:
                _raw = (request.headers.get("x-api-key", "") or "").strip()
            if _raw:
                _entry = _admin_api.get_admin_store().find_key_entry(_raw)  # P1: 一次查询，auth 复用
                _key_id = _entry[0]  # 稳定身份 kid（0=未登记）；统计按它分组，改名不分裂
                _key_name = _entry[1]
                if not _entry[2]:  # P1: 仅精确/未命中可复用；模糊走 auth 首鉴权升级（语义不变）
                    try:
                        request.state.gw_key_entry = _entry
                    except Exception:
                        pass
        except Exception:
            _key_name = ""
        _t0 = time.perf_counter()
        response = await call_next(request)
        _reason = getattr(request.state, "gw_block_reason", None)
        _act = getattr(request.state, "gw_action", None) or resolve_action(_reason, getattr(response, "status_code", 0))
        _breason = _reason or ""
        _p_tok, _c_tok = _normalize_usage(getattr(request.state, "gw_usage", None))
        _ct = (response.headers.get("content-type") or "")
        # provider 归属（request_log.provider 的唯一来源）：响应头优先 ——
        # 流式与 egress 直通都带头；非流式 JSON 没有头，得读 body。
        _provider = peek_provider_from_response(response)
        if (not _provider and _ct.startswith("application/json")
                and hasattr(response, "body_iterator")):
            # BaseHTTPMiddleware 把下游包成 _StreamingResponse ⇒ JSONResponse 在
            # 这里**没有 .body**，只能消费 body_iterator 再原样回填（字节不变）。
            # 以 content-length 设 1MB 上限：超大响应不缓冲，provider 留空
            #（退化成旧行为），绝不为了统计把响应体吃坏。
            _cl = (response.headers.get("content-length") or "").strip()
            if _cl.isdigit() and int(_cl) <= 1_000_000:
                _raw = bytearray()
                try:
                    async for _chunk in response.body_iterator:
                        _raw += (_chunk if isinstance(_chunk, (bytes, bytearray))
                                 else str(_chunk).encode("utf-8", "replace"))
                except Exception:
                    pass
                response = Response(
                    content=bytes(_raw), status_code=response.status_code,
                    media_type=_ct, headers=dict(response.headers),
                )
                _provider = provider_from_json_bytes(bytes(_raw))
        if _ct.startswith("text/event-stream") and hasattr(response, "body_iterator"):
            # 流式：延迟到流结束再落库。透传时逐事件扫描 usage，
            # 客户端未自带 include_usage 时剥离上游 usage-only chunk。
            _client_wants_usage = False
            try:
                _bj = json.loads(_body) if _body and len(_body) <= 1_000_000 else None
                _client_wants_usage = bool(isinstance(_bj, dict) and (( _bj.get("stream_options") or {}).get("include_usage")))
            except Exception:
                _client_wants_usage = False
            _usage = {"p": _p_tok, "c": _c_tok, "logged": False}

            def _flush():
                if _usage["logged"]:
                    return
                _usage["logged"] = True
                try:
                    log_request_async(
                        _admin_api.get_admin_store(),
                        client_ip=_resolve_client_ip(request),
                        key_name=_key_name,
                        key_id=_key_id,
                        model=_model,
                        action=_act,
                        blocked_reason=_breason,
                        status_code=response.status_code,
                        duration_ms=int((time.perf_counter() - _t0) * 1000),
                        gateway_internal_ms=_gw_internal_ms(request, _t0),
                        upstream_ms=_upstream_ms(request, _t0),
                        provider=_provider,
                        prompt_tokens=_usage["p"],
                        completion_tokens=_usage["c"],
                    )
                    if _usage["p"] or _usage["c"]:
                        try:
                            get_metrics().add_tokens("prompt", _usage["p"])
                            get_metrics().add_tokens("completion", _usage["c"])
                        except Exception:
                            pass
                    try:
                        get_metrics().observe_gateway_internal(float(_gw_internal_ms(request, _t0)))
                        get_metrics().observe_upstream(float(_upstream_ms(request, _t0)))
                    except Exception:
                        pass
                except Exception:
                    pass

            async def _sse_relay():
                _buf = b""
                try:
                    async for _chunk in response.body_iterator:
                        if not _chunk:
                            continue
                        _buf += _chunk
                        while True:
                            _i = _buf.find(b"\n\n")
                            if _i < 0:
                                break
                            _ev, _buf = _buf[:_i + 2], _buf[_i + 2:]
                            _out = _parse_sse_usage_event(_ev, _usage, _client_wants_usage)
                            if _out:
                                yield _out
                        if len(_buf) > 1_000_000:
                            yield _buf
                            _buf = b""
                    if _buf:
                        yield _buf
                finally:
                    _flush()

            return StreamingResponse(_sse_relay(), status_code=response.status_code,
                                     headers=dict(response.headers), media_type=_ct)
        try:
            log_request_async(
                _admin_api.get_admin_store(),
                client_ip=_resolve_client_ip(request),
                key_name=_key_name,
                key_id=_key_id,
                model=_model,
                action=_act,
                blocked_reason=_breason,
                status_code=response.status_code,
                duration_ms=int((time.perf_counter() - _t0) * 1000),
                gateway_internal_ms=_gw_internal_ms(request, _t0),
                upstream_ms=_upstream_ms(request, _t0),
                provider=_provider,
                prompt_tokens=_p_tok,
                completion_tokens=_c_tok,
            )
            if _p_tok or _c_tok:
                try:
                    get_metrics().add_tokens("prompt", _p_tok)
                    get_metrics().add_tokens("completion", _c_tok)
                except Exception:
                    pass
            try:
                get_metrics().observe_gateway_internal(float(_gw_internal_ms(request, _t0)))
                get_metrics().observe_upstream(float(_upstream_ms(request, _t0)))
            except Exception:
                pass
            _logged = True
        except Exception:
            pass
        return response

    return await call_next(request)


