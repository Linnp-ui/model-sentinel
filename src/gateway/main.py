"""
Gateway 主入口 — 公司内网大模型请求的统一出口。

请求链路（/v1/*，客户端用 OpenAI 兼容协议接入）:
    auth 鉴权（key 必带且须在管理台登记，否则 401）
      -> L1 规则（policy.yaml，_review_text / _review_file_ctx）
      -> L2 小模型判密（仅灰区 risk_score >= 20 触发，无上界，门逻辑见 AGENTS.md「L1/L2 门」）
      -> 通过: 转发外网 provider（routing.yaml 声明）
      -> 不通过: 自动降级内网模型（内容不出境，调用方无感知）

文件区段地图:
    auth / admin_auth                /v1/* key 校验；/admin 登录（cookie 会话）
    _review_text / _review_file_ctx  L1+L2 审查调度，返回 allow/route_local/block 判定
    log_entry / log_upstream_failure 审计双写（Redis 热 + MySQL 持久，见 audit_store.py）
    「# ---- 端点 ----」区段          /v1/* 转发端点、/v1/files/check、/admin* 页面
    admin_api.py（include 的 router） 管理面 REST：key/规则/模型/provider/审计/L2 影子

改 src/*.py 后必须重启 gateway.service 并核对 MainPID 变更（用 scripts/deploy.sh）。
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

from datetime import datetime, timedelta, timezone

from urllib.parse import parse_qs, quote

from contextvars import ContextVar
from typing import Any, Optional


import httpx

from fastapi import FastAPI, Header, UploadFile, File, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler

from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, RedirectResponse, Response, StreamingResponse

from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, ConfigDict, ValidationError

from starlette.concurrency import run_in_threadpool

from starlette.middleware.gzip import GZipMiddleware


from .inspection import inspect_text

from .file_inspector import inspect_file, inspect_inline_bytes, collect_inline_blobs_chat, collect_inline_blobs_anthropic, collect_inline_blobs_responses
from .llms.bridges import clip_audit, clip_audit_url, sse as _sse, tools_text

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

from .small_model import classify as small_classify, classify_chunks, is_confidential, is_injection, is_degraded

from .user_models import UserModel, get_store, validate

from .audit_store import get_audit_store

from .session_store import get_session_store, extract_session_id

from .metrics import get_current_status, get_metrics, set_current_status, GATEWAY_VERSION
from .redaction import redact_audit_entry
from .responses_filectx import NON_CONFIDENTIAL_RULES, extract_responses_filectx, responses_should_mark

from .stat_scope import is_blocked, is_local_route, resolve_action, to_beijing

from .rule_names import rule_match_text

from .rate_limit import get_limiter, extract_client_key as _rl_key
from .identity import (
    set_current_identity as _set_current_identity,
    Identity,
    identity_v2_enabled as _identity_v2,
    local_identity as _local_identity,
    identity_from_entry as _identity_from_entry,
    current_identity,
)


# 审计归因：当前请求的调用方 key（由 _admin_ip_filter_mw 在 /v1/* 写入）。
# 中间件是 async，set 后在同一请求 task 的 handler/log_entry 内可见。
_CURRENT_CLIENT_KEY: ContextVar[str] = ContextVar("gw_client_key", default="")
# 审计归因：当前请求文本的风险分（_review_text / 各 inspect_text 调用点写入，log_entry 自动补 risk_score）。
_CURRENT_RISK: ContextVar = ContextVar("gw_risk_score", default=None)
# 审计归因：当前请求文本的 L1 命中构成（与风险分同点写入，log_entry 自动补 findings；
# 文件行显式传入的 findings 优先，不覆盖）。
_CURRENT_FINDINGS: ContextVar = ContextVar("gw_findings", default=None)
# 审计归因：内联附件预检结果（文件 flags/sheet_names 等），log_entry 自动补
# file_findings 独立键——与文本 findings 拆开，展开区两段同显（2026-10-29）。
_CURRENT_FILE_FINDINGS: ContextVar = ContextVar("gw_file_findings", default=None)
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


# 响应 gzip：管理台首屏 561KB JS 原样传输，gzip 后约 190KB（内网也省）。
# starlette 默认排除 text/event-stream（SSE 不压缩不缓冲 ⇒ 状态条推送不受影响），
# 且流式响应整体不压缩；上游已带 content-encoding 的响应也不重复压。
# compresslevel=6（默认 9 对 400KB 级 chunk CPU 偏高，收益差 1%）。
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


@app.exception_handler(RequestValidationError)
async def _validation_zh(request: Request, exc: RequestValidationError):
    """管理台请求体校验错统一中文；/v1 保持 FastAPI 原始形状（协议契约不动）。

    两边都对 errors() 做 JSON 安全清洗：pydantic 的 value_error 上下文会携带
    ValueError 实例，FastAPI 默认处理器直接 json.dumps 会 500。"""
    if request.url.path.startswith("/admin"):
        from src.gateway.admin_api import _verrors
        return JSONResponse(status_code=422, content={"detail": _verrors(exc)})
    return await request_validation_exception_handler(request, exc)


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
async def _provider_probe_startup():
    # 后台周期探测 provider 存活（/models，零 token）；/health 与状态条读缓存，
    # 模型挂了状态条能亮红——补齐「configured 只报配置、不报连通」的观测缺口。
    from .provider_probe import start as _probe_start
    _probe_start()

@app.on_event("startup")
async def _l2_overrides_startup():
    # T46: 应用 l2_overrides.yaml 持久化覆盖（首个请求前；键白名单见 l2_overrides.py）
    from src.gateway.l2_overrides import apply_overrides
    apply_overrides()

@app.on_event("startup")
async def _startup_validate():
    # 生产强校验：仅在启用 admin 密码（生产形态）时强制；dev/测试无密码则跳过，避免破坏 conftest 隔离。
    if not _admin_password():
        return
    errs = []
    if not (os.getenv("ADMIN_DB_URL") or os.getenv("DATABASE_URL")):
        errs.append("ADMIN_DB_URL/DATABASE_URL 未配置：审计/明细将落内存后端，重启即丢")
    if not (os.getenv("AUDIT_MYSQL_URL") or os.getenv("AUDIT_DATABASE_URL")):
        errs.append("AUDIT_MYSQL_URL/AUDIT_DATABASE_URL 未配置：审计落内存后端")
    if (os.getenv("AI_GATEWAY_L2_FAIL_MODE", "") or "").strip().lower() == "open":
        errs.append("AI_GATEWAY_L2_FAIL_MODE=open：L2 不可用时放行，可能泄密，生产应为 route_local")
    dev_key = (os.getenv("AI_GATEWAY_DEV_API_KEY", "") or "").strip()
    if dev_key == "pk_live_dev_changeme" or not dev_key:
        errs.append("AI_GATEWAY_DEV_API_KEY 未设置或仍为默认弱值")
    if errs:
        raise RuntimeError("生产启动校验失败：\n- " + "\n- ".join(errs))


@app.on_event("startup")
async def _log_retention_startup():
    # request_log 明细自动清理：启动时先跑一次（_last_purge_ts=0 立即触发），
    # 之后每 6h 周期跑；落库写入也会按 _maybe_purge 节流触发，二者互补。
    import asyncio as _asyncio
    from .stats_service import _maybe_purge
    from .admin_store import get_admin_store

    def _once():
        try:
            _maybe_purge(get_admin_store())
        except Exception:
            pass

    async def _loop():
        while True:
            try:
                await _asyncio.to_thread(_once)
            except Exception:
                pass
            await _asyncio.sleep(6 * 3600)

    _asyncio.create_task(_loop())
    # 状态条 SSE 停机收尾：unit ExecStop 先 kill -USR1 $MAINPID，
    # status_stream 生成器见 sse_stop 立即正常 return，别让长连接把 stop 拖满 90s 被 SIGKILL。
    from .admin_api import register_sse_stop
    register_sse_stop()


@app.on_event("shutdown")

async def _shutdown():
    await aclose_clients()


# 审计双写：Redis 热缓存 + MySQL 持久化（见 audit_store.py）


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
        raise HTTPException(401, "缺少 API key（请带 Authorization: Bearer <key> 或 x-api-key 头）")
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
    raise HTTPException(401, "API key 未登记或无效（到管理台「API KEY 管理」登记，或找管理员）")

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

    费用上限（weekly_cost_cap）也统一在这里判：它是配额不是限流 ——
    ``/v1/{path}`` 兜底代理没有限流 dep、限流关闭（RPM/RPS=0）时
    ``_make_rate_limit_dep`` 提前 return，放那边上限会被静默关掉 ⇒ 放所有
    /v1 端点必过的唯一入口。
    """
    try:
        if _identity_v2() and isinstance(identity, Identity):
            _set_current_identity(identity)
    except Exception:
        pass
    try:
        kid = getattr(identity, "key_id", None)
        if kid:
            from .admin_store import get_admin_store
            from .stats_service import key_weekly_cost
            _store = get_admin_store()
            # 同步 MySQL 必须挪出事件循环：单行查询本身毫秒级，但本池
            # （pool_size=2+max_overflow=2）与 admin 端点共享，MySQL 抖动/池耗尽时
            # connect() 最长等 30s —— 在循环上跑 = 整个网关（含 /health）无响应
            # （2026-10-27 用户报「网关有时候会无响应」，见 tests/test_loop_responsive.py）。
            _cap = await run_in_threadpool(_store.get_key_weekly_cap, kid)
            if _cap > 0:
                _spend = await run_in_threadpool(key_weekly_cost, _store, kid)
                if _spend >= _cap:
                    get_metrics().inc_rate_limited("cost", "weekly_cost_cap")
                    raise HTTPException(
                        status_code=429,
                        detail={"type": "quota_exceeded", "scope": "cost",
                                "spend": round(_spend, 4), "cap": _cap},
                    )
    except HTTPException:
        raise
    except Exception:
        pass
    return identity

def _make_rate_limit_dep(scope: str, rpm_env: str, rps_env: str = None):
    """限流依赖工厂：4 个 /v1/* 入口的限流逻辑唯一实现（仅 scope 与 env 名不同）。"""
    async def _dep(request: Request, identity: Identity = Depends(_bind_identity_ctx)) -> Identity:
        rpm = int(os.getenv(rpm_env, "0") or 0)
        rps = int(os.getenv(rps_env, "0") or 0) if rps_env else 0
        if rpm <= 0 and rps <= 0:
            return identity
        authz = request.headers.get("authorization", "")
        xak = request.headers.get("x-api-key", "")
        client_ip = _resolve_client_ip(request)
        key = _rl_key(authz, xak, client_ip, identity_key=_identity_bind_key(request))
        # 费用上限不在这里判：见 _bind_identity_ctx（配额要覆盖全部 /v1 入口，
        # 且限流关闭时仍生效；放这里 RPM/RPS=0 提前 return 会静默关掉配额）。
        ok, retry, reason = get_limiter().check(key, scope=scope)
        if not ok:
            get_metrics().inc_rate_limited(scope, reason)
            raise HTTPException(
                status_code=429,
                detail={"type": "rate_limited", "scope": scope, "reason": reason, "retry_after": retry},
                headers={"Retry-After": str(retry)},
            )
        return identity
    return _dep


_rate_limit_chat = _make_rate_limit_dep("chat", "AI_GATEWAY_RATE_LIMIT_RPM", "AI_GATEWAY_RATE_LIMIT_RPS")
_rate_limit_files = _make_rate_limit_dep("files", "AI_GATEWAY_RATE_LIMIT_FILES_RPM")
_rate_limit_embed = _make_rate_limit_dep("embed", "AI_GATEWAY_RATE_LIMIT_RPM", "AI_GATEWAY_RATE_LIMIT_RPS")
_rate_limit_messages = _make_rate_limit_dep("messages", "AI_GATEWAY_RATE_LIMIT_RPM", "AI_GATEWAY_RATE_LIMIT_RPS")


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
        if _MENTIONED_FILE_RE is None:
            # 2026-10-19：原 greedy 字符类在「长无点单词串」上 O(n²) 回溯 —— 512KB 'a'
            # 实测把事件循环卡死 20 分钟（单请求 DoS，M1 探针踩到）。加左边界 lookbehind
            # （只从词首尝试）+ 长度上限 200（文件名远短于此）⇒ 线性；CJK 连写命中形态不变。
            _MENTIONED_FILE_RE = re.compile(
                r"(?<![A-Za-z0-9_\-\u4e00-\u9fa5~])"
                r"[A-Za-z0-9_\-\u4e00-\u9fa5~]{1,200}\.(?:csv|tsv|txt|xlsx|xls|pdf|docx?)",
                re.IGNORECASE)
        m = _MENTIONED_FILE_RE.search(text or "")
        return m.group(0) if m else ""
    except Exception:
        return ""


def _mark_hit_context(session_id, key_id, session_name, taint_name, decision):
    """命中后双轨标记：会话机密 + key 级文件污染；任一失败不阻断主链。

    2026-10-26：纯文本机密判定也标会话（第一轮判密 ⇒ 同会话后续轮粘本地）；
    可用性兜底（NON_CONFIDENTIAL_RULES）不标——那不是内容判定。
    """
    try:
        d = decision or {}
        if d.get("action") not in ("route_local", "block") or d.get("name") in NON_CONFIDENTIAL_RULES:
            return
        if session_id:
            get_session_store().mark_confidential(
                session_id,
                {"name": session_name or "text-hit", "rule": d.get("name"), "action": d.get("action")},
            )
        _name = _mentioned_data_filename(taint_name or "")
        if key_id and _name:
            get_session_store().mark_tainted_file(key_id, _name, d.get("name") or "")
    except Exception:
        pass


def _mark_session_hit(session_id, decision, name=""):
    """命中（route_local/block）即标 session，后续同会话粘本地；与 files/check 同口径。

    2026-10-26：纯文本机密判定也标（此前「无文件参与不标」约定作废）；
    可用性兜底（NON_CONFIDENTIAL_RULES）不标。标记失败绝不影响主链路。
    """
    try:
        d = decision or {}
        if (not session_id or d.get("action") not in ("route_local", "block")
                or d.get("name") in NON_CONFIDENTIAL_RULES):
            return
        get_session_store().mark_confidential(
            session_id,
            {"name": name or "text-hit", "rule": d.get("name"), "action": d.get("action")},
        )
    except Exception:
        pass


def _mark_taint_hit(key_id, filename, decision):
    """命中即记被污染文件名（客户端无感：不依赖 session 头，按 key 隔离）。

    filename 取 basename 段（_mentioned_data_filename 提纯）；key 为空即跳过
    （绝不写全局）。失败绝不影响主链路。
    """
    try:
        if (decision or {}).get("action") not in ("route_local", "block"):
            return
        name = _mentioned_data_filename(filename or "")
        if key_id and name:
            get_session_store().mark_tainted_file(
                key_id, name, (decision or {}).get("name") or "")
    except Exception:
        pass


class ChatMessage(BaseModel):

    model_config = ConfigDict(extra="allow")
    role: str
    content: Any = None  # str or list[{type:text}] or None for tool_calls
    tool_calls: Any = None
    tool_call_id: Any = None

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
        return clip_audit_url(url)
    if t in ("file", "input_file"):
        f = p.get("file", {})
        if isinstance(f, dict):
            return " ".join(clip_audit(f.get(k, ""), 500) for k in ("filename", "file_id")).strip()
        for k in ("filename", "file_id"):
            if p.get(k):
                return clip_audit(p[k], 500)
        return ""
    if t in ("input_audio", "audio"):
        return ""
    for k in ("filename", "file_id"):
        if isinstance(p.get(k), str) and p[k]:
            return clip_audit(p[k], 500)
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
            parts.append(clip_audit(fn["name"], 200))
        if fn.get("arguments"):
            parts.append(clip_audit(fn["arguments"], 4000))
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


def _slim_client_envelope(s: str) -> str:
    """剥客户端塞进 user 正文的环境样板，只留用户真问题（**纯展示**）。

    IDE 类客户端（workbuddy / codex）把 user_info、cwd、craft_mode、当前时间塞在
    `<system-reminder data-role="user-context">` / `<environment_context>` 里，正文裹在
    `<user_query>`（内含「用户输入 / 当前时间 / 期望返回」分段）或 `<session>` 里。原样截
    200 字 ⇒ 审计摘要里只看得到 OS 版本和工作区路径，真问题被埋在 200 字之后。

    `<user_query>` 取「用户输入」段（文件路径另有 filename 列、时间是样板）；认不出的形状
    原样返回，不丢内容。L1/L2 审的是 `text`/`last_user` 全文，不走这里。
    """
    for tag in ("system-reminder", "environment_context"):
        s = re.sub(rf"<{tag}\b.*?</{tag}>", " ", s, flags=re.S)
    m = re.search(r"<user_query\b[^>]*>(.*?)</user_query>", s, re.S)
    if m:
        q = re.search(r"用户输入[：:]\s*(.*)", m.group(1))
        s = (q.group(1).strip() if q else m.group(1).strip())
    return re.sub(r"</?session\b[^>]*>", " ", s).strip()


def _chat_session_id(messages):
    """chat 客户端（workbuddy 等）不发 X-Session-ID：首条 user 消息指纹当会话 key。

    与 _codex_session_id（responses 侧）同原理：多轮客户端每轮全量重发 messages，
    首条 user 消息在对话内不变。指纹前先 _slim_client_envelope 剥客户端样板
    （OS 版本/工作目录/当前时间是易变字段）。单轮无状态客户端指纹每轮都变
    = 无粘性收益，不比无会话头更差。
    """
    try:
        if not isinstance(messages, list):
            return None
        for m in messages:
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            c = m.get("content")
            if isinstance(c, str):
                first = c
            elif isinstance(c, list):
                first = "\n".join(str(b.get("text") or "") for b in c
                                   if isinstance(b, dict) and b.get("type") == "text")
            else:
                continue
            first = _slim_client_envelope(first)
            if first.strip():
                return "chat-" + hashlib.sha256(first.encode("utf-8", "replace")).hexdigest()[:16]
    except Exception:
        pass
    return None


_CODEX_TITLE_HEAD = "You are a helpful assistant. You will be presented with a user prompt"
_CODEX_TITLE_NOISE = ("You are a helpful assistant.", "The tasks typically have to do with")


def _audit_preview_responses(body, text: str) -> str:
    """codex 摘要精简（纯展示，L1/L2 审的是全文不受影响）：
    ① 附件包装 '# Files mentioned... ## My request:' → 只留 My request 正文
       （文件名另有 filename 列）；② 内部标题生成调用 → 剥掉开头样板行，留被
       起标题的用户 prompt。都认不出时原样回退，不丢内容。"""
    s = _last_user_text_responses(body) or (text or "")
    s = _slim_client_envelope(s) or s
    m = re.search(r"##\s*My request:\s*(.*)", s, re.S)
    if m and m.group(1).strip():
        s = m.group(1).strip()
    elif s.startswith(_CODEX_TITLE_HEAD):
        lines = s.splitlines()
        while lines and lines[0].lstrip().startswith(_CODEX_TITLE_NOISE):
            lines.pop(0)
        s = "\n".join(lines).strip() or s
    return s[:200]


def _l2_source_text(kind: str, payload, merged_text: str) -> str:
    """未截断的 L2 源文本 = 用户 query + 内联 OCR 文本。

    `_select_l2_text` 只是它的 `[:1200]` 视图；`long_text` scope 从同一个源按窗抽样 ——
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


def _prev_user_text_chat(messages, limit: int = 2) -> str:
    """最后一条用户消息<b>之前</b>的最近 limit 条 user 正文（多轮追问自指：
    「改成 24055 吧」单独判 NORMAL，带上轮才判得出）。只取 user，跳过 tool 调用/结果。"""
    texts = []
    try:
        for m in messages or []:
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
                texts.append(s)
    except Exception:
        pass
    if len(texts) <= 1:
        return ""
    return "\n".join(texts[-(limit + 1):-1])


def _prev_user_text_anthropic(body, limit: int = 2) -> str:
    """同 _prev_user_text_chat，anthropic 形状（user 消息 str / text 块）。"""
    texts = []
    try:
        msgs = (body.get("messages") or []) if isinstance(body, dict) else []
        for m in msgs:
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                texts.append(c.strip())
            elif isinstance(c, list):
                ts = [b.get("text", "") for b in c
                      if isinstance(b, dict) and b.get("type") in ("text", "input_text") and b.get("text")]
                s = " ".join(ts).strip()
                if s:
                    texts.append(s)
    except Exception:
        pass
    if len(texts) <= 1:
        return ""
    return "\n".join(texts[-(limit + 1):-1])


def _prev_user_text_responses(body, limit: int = 2) -> str:
    """同 _prev_user_text_chat，responses 形状（input 里的 user 消息）。"""
    texts = []
    try:
        inp = body.get("input") if isinstance(body, dict) else None
        if isinstance(inp, list):
            for item in inp:
                if not isinstance(item, dict):
                    continue
                if item.get("type") in ("function_call_output", "function_call"):
                    continue
                if item.get("role") not in (None, "user"):
                    continue
                c = item.get("content")
                if isinstance(c, str) and c.strip():
                    texts.append(c.strip())
                elif isinstance(c, list):
                    ts = [b.get("text", "") for b in c
                          if isinstance(b, dict) and b.get("type") in ("input_text", "text") and b.get("text")]
                    s = " ".join(ts).strip()
                    if s:
                        texts.append(s)
    except Exception:
        pass
    if len(texts) <= 1:
        return ""
    return "\n".join(texts[-(limit + 1):-1])


# ---- L2 scope 名（docs/l2-scope-extension-plan-2026-09-16.md §3.1）----
_L2_SCOPE_LAST_USER = "last_user"
_L2_SCOPE_TOOL = "tool"
_L2_SCOPE_LONG = "long_text"
_L2_SCOPE_HISTORY = "history"
# 明确排除的 scope：system 见 §3.8。写进配置只留痕、不产块 —— 防止有人以为「配上就生效」。
# history 曾在 §3.7 排除（「判决失忆，不是视野盲区」），2026-10-26 实测多轮追问
# 「改成 24055 吧」单独判 NORMAL 出境、带上轮判密 ⇒ 恢复为可用档。
_L2_SCOPE_EXCLUDED = ("system", "developer", "instructions")
_L2_SCOPE_MAX_BLOCKS = 5      # = classify_chunks 的 chunks[:5] 硬上限
# 灰区强制 L2 下限（env 可调）：L1 放行且 risk_score >= FLOOR 的文本，无论长短都送
# L2 语义复核（短句语义泄密是 L1 格式规则的真空带）。
#
# 2026-09-30 修：原上界是 `risk_score < 60`（RISK_THRESHOLD_ROUTE_LOCAL），**这条上界
# 只在 risk_score 全部来自 PII 时才成立**。risk_score = weighted_pii + keyword_score，
# 而唯一按分值判定的 policy 规则（priority 5）读的是 **weighted_pii_score**，
# 不含关键词分 —— 于是「关键词堆叠到 >=60」既超灰区上界（L2 不触发）、
# 又匹配不上 priority 5（L1 放行），两条路都漏，内容直出外网。
# 实测「工资 薪资 奖金」kw=60 → allow/deepseek；单词「机密」kw=30 → route_local（堆得越多越危险）。
#
# 现在**去掉上界**：PII>=60 的请求不会走到这里——priority 5 已判 route_local，
# 门的第一个条件 `action == "allow"` 就是 False，对 PII 是零行为变化（死代码）。
# 剩下的全是「L1 放行但分值偏高」= 关键词驱动，交给 L2 语义判定。
def _l2_gray_floor() -> int:
    """灰区强制 L2 的 risk_score 下限（env 可调、**即时生效**：每次调用读 env，
    管理台改完不用重启；2026-10-21 从模块常量改为函数）。

    上限钉 20：再高则 inspection.KEYWORD_WEIGHTS 里 25/30 档关键词
    （图纸/身份证/机密/绝密…）单命中就不再进灰区 ⇒ 审查被削弱，故不允许调高。
    """
    try:
        v = int((os.getenv("AI_GATEWAY_L2_GRAY_FLOOR", "20") or 20).strip() or 20)
    except (TypeError, ValueError):
        return 20
    return max(0, min(20, v))


def _l2_always() -> bool:
    """L2 常开开关（env 即时生效）：true 时所有 L1 放行请求都送 L2 语义复核，
    不再只看灰区分值 —— risk=0 的语义泄密（「薪酬明细」「改成 24055 吧」）L1 完全
    打不中，实测回放 120 条 risk=0 放行文本有 4 条 L2 会判密。白名单 5% 抽样与
    本地别名直通不受影响。关掉即回灰区口径。"""
    return (os.getenv("AI_GATEWAY_L2_ALWAYS", "false") or "").strip().lower() in ("1", "true", "yes", "on")


def _l2_gray_trigger(findings) -> bool:
    """灰区命中即 True（供 _review_text 门与 route-inspect 干跑共用，保持口径一致）。

    只看下限、不设上界：`risk_score` 混了关键词分，上界用总分会漏掉关键词堆叠的情况
    （见上方注释）。上界天然由 policy 的 `action != allow` 承担。
    """
    try:
        s = int((findings or {}).get("risk_score") or 0)
        return s >= _l2_gray_floor()
    except Exception:
        return False


def _encoded_keyword_decision(findings) -> dict | None:
    """解码层（base64/hex）独有关键词 ⇒ 直接 route_local，不交给 L2。

    2026-10-19 生产实测：整段机密文本编码后 risk_score 进灰区，L2 看到的是编码串，
    判 NORMAL 秒放行 ⇒ 内容直出外网。编码本身即逃避特征，L2 在此看不清明文，
    判决不可信 ⇒ fail-safe 转本地（与 l2_unavailable 同向：守住不出境）。
    读原始 findings（inspection.inspect_text 的 encoded_keyword_hits）。
    """
    hits = (findings or {}).get("encoded_keyword_hits") or []
    if not hits:
        return None
    return _route_local_decision("encoded_keyword",
                                 "解码层关键词: " + ",".join(str(h) for h in hits[:6]))


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
         "budget_chars": 6000}
    try:
        from .model_policy import (review_budget_chars, review_chunk_chars,
                                   review_scopes)
        d["scopes"] = review_scopes()
        d["chunk_chars"] = review_chunk_chars()
        d["budget_chars"] = review_budget_chars()
    except Exception as e:
        _l2_scope_warn(f"scope 配置读取失败，回退默认档（只审 last_user）: {e}")
    return d


def _l2_long_windows(src: str, cc: int, limit: int, start: int = 0) -> list:
    """源文本按 cc 切窗，最多 limit 段：段数够就全取，超了均匀抽样并保住真正的尾段。

    `start` 默认从第 2 段起（首段由 `last_user` 覆盖）；只开 long_text 时传 0，
    否则前 cc 字会成盲区。
    """
    if limit <= 0 or len(src) <= start:
        return []
    rest = src[start:]
    n = (len(rest) + cc - 1) // cc
    if n <= limit:
        idx = list(range(n))
    elif limit == 1:
        idx = [n - 1]          # 只有 1 个槽位时给尾部（前段有 last_user 兜）
    else:
        idx = sorted({round(i * (n - 1) / (limit - 1)) for i in range(limit)})
    return [rest[i * cc:(i + 1) * cc] for i in idx]


def _select_l2_scopes(kind: str, payload, merged_text: str) -> list:
    """按 scope 名切 L2 视野块，返回 [(scope, text), ...]。

    - 空块不返回（少了就不发这次调用）。
    - `last_user` 走 `_select_l2_text` ⇒ **与历史行为逐字节一致**（默认档唯一成员）。
    - `tool` = 最后一条工具结果；`long_text` = 源文本从第 2 段起均匀抽样（最多占满
      剩余槽位，含真正的尾段）——1 段只能看 1200 字，长粘贴的后半段会整段漏掉。
    - `history` = 最后一条用户消息之前的最近 2 条 user 正文（尾部截到 chunk）——
      多轮追问「改成 24055 吧」单独判 NORMAL，带上轮才判得出。
    - 预算先到先用；`last_user` 不受预算裁剪（它是判定基线，不能被配置饿死）。
    """
    cfg = _l2_scope_cfg()
    names = list(cfg["scopes"] or [_L2_SCOPE_LAST_USER])
    cc = max(1, int(cfg["chunk_chars"] or 1200))
    budget = max(0, int(cfg["budget_chars"] or 0))
    src = ""
    if _L2_SCOPE_LONG in names:
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
    hist = ""
    if _L2_SCOPE_HISTORY in names:
        try:
            if kind == "chat":
                hist = _prev_user_text_chat(payload)
            elif kind == "anthropic":
                hist = _prev_user_text_anthropic(payload)
            elif kind == "responses":
                hist = _prev_user_text_responses(payload)
        except Exception:
            hist = ""

    out = []
    used = 0

    def _add(name, t, counted=True):
        nonlocal used
        t = (t or "").strip()
        if not t or len(out) >= _L2_SCOPE_MAX_BLOCKS:
            return
        if counted and budget and used + len(t) > budget:
            return
        if counted:
            used += len(t)
        out.append((name, t))

    # 单块档先占位，long_text 只用剩余槽位 —— 否则长文窗口会把 tool 挤掉。
    # 任一档命中即整体命中，顺序只影响归因。
    for name in names:
        if name in _L2_SCOPE_EXCLUDED:
            _l2_scope_warn(f"scope {name!r} 已在方案 §3.7/§3.8 排除，配置项被忽略")
        elif name == _L2_SCOPE_LAST_USER:
            _add(name, _select_l2_text(kind, payload, merged_text), counted=False)
        elif name == _L2_SCOPE_TOOL:
            _add(name, tool[:cc])
        elif name == _L2_SCOPE_HISTORY:
            _add(name, hist[-cc:])          # 尾部截断：离当前轮最近的上一轮最有指代意义
        elif name == _L2_SCOPE_LONG:
            continue
        else:
            _l2_scope_warn(f"未知 scope {name!r}，已忽略")
    if _L2_SCOPE_LONG in names:
        start = 0 if _L2_SCOPE_LAST_USER not in names else cc
        for w in _l2_long_windows(src, cc, _L2_SCOPE_MAX_BLOCKS - len(out), start):
            _add(_L2_SCOPE_LONG, w)
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
    raw = last_user or text
    return {"text": text, "last_user": last_user, "total_chars": total_chars,
            "audit_preview": (_slim_client_envelope(raw) or raw)[:200]}


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


async def _inline_media_scan(kind: str, payload) -> tuple[str, bool, dict, bool]:
    """内联/远端 图片/文档 → (并入 policy 的文本, 是否有不可读块, 文件预检 findings, 是否有远端 URL 附件)。

    kind: chat | anthropic | responses。解码+检查在线程池跑，不阻塞事件循环；
    任一块不可读(解码失败/超 cap/抽空文本)即 fail-closed，由 policy 接住。
    remote=True（远端 http(s) URL 附件，字节不过网关）⇒ 调用方置 remote_attachment
    让 policy「远端附件转内网」fail-closed。
    带内联图片时置 alias_router 的 needs_image 标记：选头/故障切换硬过滤
    纯文本候选（实测 deepseek-v4-pro 200 但看不见图，静默降级比 400 更隐蔽）。
    """
    if kind == "chat":
        blobs, bad, has_image, remote = collect_inline_blobs_chat(payload)
    elif kind == "anthropic":
        blobs, bad, has_image, remote = collect_inline_blobs_anthropic(payload)
    elif kind == "responses":
        blobs, bad, has_image, remote = collect_inline_blobs_responses(payload)
    else:
        return "", False, {}, False
    if has_image:
        from .alias_router import ctx_set_needs_image
        ctx_set_needs_image(True)
    if not blobs:
        # 无内联字节可检；远端 URL 附件在此显式标记，展开区显示 fail-closed 原因
        return "", bad, ({"remote_attachment": True} if remote else {}), remote
    results = await asyncio.gather(*[run_in_threadpool(inspect_inline_bytes, x) for x in blobs])
    parts = []
    unreadable = bad
    file_findings = {}
    for i, r in enumerate(results):
        ct = (r.get("text") or "").strip()
        if ct:
            parts.append(f"[INLINE_MEDIA {i + 1}]\n{ct}\n[/INLINE_MEDIA]")
        if r.get("unreadable"):
            unreadable = True
        ff = dict(r.get("findings") or {})
        # 表名/表头随预检结果入审计（展开区文件段显示，多 blob 时 _merge_file_findings 去重合并）
        for k in ("sheet_names", "headers"):
            if r.get(k):
                ff[k] = list(r[k])
        if ff:
            file_findings = _merge_file_findings(file_findings, ff)
    if remote:
        file_findings = {**file_findings, "remote_attachment": True}
    return ("\n".join(parts), unreadable, file_findings, remote)


def _merge_file_findings(a: dict, b: dict) -> dict:
    """合并多个内联 blob 的 file findings 进一个摘要 dict。

    镜像 /v1/files/check 单文件的 findings 形态，供审计展开区 (FindingsDetail)
    以「文件预检结果」呈现。列表型(key) 合并去重；数值累加；布尔 OR；字串取首。
    """
    out = dict(a)
    for k, v in b.items():
        if v is None:
            continue
        if isinstance(v, list):
            cur = out.get(k)
            base = list(cur) if isinstance(cur, list) else []
            for item in v:
                if item not in base:
                    base.append(item)
            out[k] = base
        elif isinstance(v, bool):
            out[k] = bool(out.get(k)) or v
        elif isinstance(v, (int, float)):
            ov = out.get(k)
            out[k] = (ov if isinstance(ov, (int, float)) and not isinstance(ov, bool) else 0) + v
        elif isinstance(v, str):
            if not out.get(k):
                out[k] = v
        else:
            out[k] = v
    return out


# 审计 file_findings 里单个列表值的封顶项数（千表/万列工作簿的 sheet_names/headers
# 会撑大 Redis 审计 JSON；截断后补「…共N项」标记，展开区看到的总数不丢）。
_FILE_FINDINGS_LIST_CAP = 100


def _note_file_findings(file_findings: dict) -> None:
    """把内联附件预检 findings 暂存进请求上下文，log_entry 落审计 file_findings 独立键。

    与文本 findings（_CURRENT_FINDINGS）彻底分开：FindingsDetail 双段同显
    （L1 命中构成 + 文件预检结果），PII 命中不再挤掉文件视图
    （2026-10-29 前的「无命中才写」变通因此作废）。
    """
    if not file_findings:
        return
    capped = {}
    for k, v in file_findings.items():
        if isinstance(v, list) and len(v) > _FILE_FINDINGS_LIST_CAP:
            v = v[:_FILE_FINDINGS_LIST_CAP] + [f"…共{len(v)}项"]
        capped[k] = v
    _CURRENT_FILE_FINDINGS.set(capped)


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
    if isinstance(file_ctx.get("text"), str) and file_ctx["text"].strip():
        base["text"] = file_ctx["text"][:60000]
    return base

def _all_local_alias(model: str) -> bool:
    """别名组全部启用候选都是内网 provider → True（如 local-model）。

    这类请求内容不可能出境：所有故障切换路径都跳过 local 候选（routing.py
    三处 `pcfg.local` 判定），全组失败即 fail-loud，因此 L1/L2 审查可跳过。
    """
    from .alias_router import find_alias
    from .providers import load_routing
    group = find_alias(model)
    if not group:
        return False
    cands = group.get("candidates") or []
    if not cands:
        return False
    try:
        cfg = load_routing()
    except Exception:
        return False
    return all(getattr(cfg.get(c["provider"]), "local", False) for c in cands)


# F1（2026-10-14 红队实测）：审查文本总量无预算——每 input item 截 4000 字但
# item 数无限、instructions 不截 ⇒ 2 万条 item × 4000 字 = 76MB 审查串，
# 单请求把 inspect_text/NFKC/正则全跑一遍（阻塞事件循环，别的用户一起停；
# 生产限流 5 RPS  ⇒ 单 key 可打瘫全网关）。这里只保留**尾部**（最新内容优先，
# 历史区降覆盖——旧历史曾按请求逐轮审过，且 session 机密标记 TTL 内粘性）。
# 512KB ≈ 128 个 4000 字段，正常 Codex 会话（数十轮）远够；内联 OCR 追加在
# 尾部，超预算时优先保留。
_REVIEW_TEXT_BUDGET_DEFAULT = 512 * 1024


def _review_text_budget() -> int:
    try:
        return max(1024, int(os.getenv("AI_GATEWAY_REVIEW_TEXT_BUDGET")
                             or _REVIEW_TEXT_BUDGET_DEFAULT))
    except ValueError:
        return _REVIEW_TEXT_BUDGET_DEFAULT


def _cap_review_text(text: str) -> tuple[str, bool]:
    """保留尾部并返回是否溢出（M1，2026-10-19）。

    超预算静默丢头部 = 绕过：头部放密文 + 尾部堆 >预算 良性填充，密文从未被扫
    ⇒ 调用侧必须对溢出整条 fail-safe 转本地（内容不出境），不能假装审完了。
    预算可调 AI_GATEWAY_REVIEW_TEXT_BUDGET（越大 CPU 越贵：512KB ≈ 150ms）。
    """
    b = _review_text_budget()
    if len(text) <= b:
        return text, False
    return text[-b:], True


# F1(2026-10-14)：入站 body 无大小上限、且 request.json()= json.loads 同步跑在
# 事件循环上。实测 1.17GB body → 2.2s 阻塞（chat 端点还叠一层 ChatReq 校验），
# 单 key 5 RPS 可把网关打成全线无响应。上限默认 32MB（合法上限：内联媒体 5 件
# × 20MB 已超此值 ⇒ 多件 20MB 大图走 env 调大 AI_GATEWAY_MAX_BODY_BYTES）。
_MAX_BODY_BYTES_DEFAULT = 32 * 1024 * 1024


def _max_body_bytes() -> int:
    try:
        return max(1024, int(os.getenv("AI_GATEWAY_MAX_BODY_BYTES")
                             or _MAX_BODY_BYTES_DEFAULT))
    except ValueError:
        return _MAX_BODY_BYTES_DEFAULT


async def _json_body(request: Request) -> Any:
    """读 JSON body：超限 413 + json.loads 移出事件循环（线程池）。

    抛 HTTPException(400) 替代原 request.json() 的未捕获 500。
    """
    raw = await request.body()
    cap = _max_body_bytes()
    if len(raw) > cap:
        raise HTTPException(413, detail={
            "type": "body_too_large",
            "message": f"请求体超过 {cap // (1024 * 1024)}MB 上限"})
    try:
        return await run_in_threadpool(json.loads, raw)
    except Exception:
        raise HTTPException(400, detail={"type": "invalid_request",
                                         "message": "请求体不是合法 JSON"})


def _intent_source(l2_chunks, l2_text, text) -> str:
    """复杂度判定的输入 = 最后一条用户消息（含内联 OCR），不是整段会话历史。

    取 L2 的 last_user 块：同一轮 tool 回合里它逐字不变 ⇒ intent_router 的文本缓存
    天然把「一次用户发言」收敛成一次判定（换一句话才重判）。
    """
    if l2_chunks:
        for _s, _t in l2_chunks:
            if _s == _L2_SCOPE_LAST_USER and _t:
                return _t
        return (l2_chunks[0][1] if l2_chunks else "") or text
    return l2_text or text


async def _review_text(text: str, request: Optional[Request] = None, extra_findings: Optional[dict] = None, l2_text: Optional[str] = None, l2_chunks: Optional[list] = None, file_ctx: Optional[dict] = None, model: str = "", session_hint: Optional[str] = None) -> tuple[dict, dict, dict | None]:
    """

    纯文本审查（chat / messages / responses 共用）：

    L1 规则 -> 命中即返回；L1 放行且 risk_score >= 灰区下限(默认20) 时触发 L2 小模型语义判定
    （2026-09-23 起取消字数触发；2026-09-30 起灰区无上界，关键词堆到 risk_score>=60 也转内网）。

    全本地别名组（_all_local_alias，候选全内网）跳过 L1/L2 直接 allow。

    返回 (decision, text_findings, l2_result)

    """
    text, _overflow = _cap_review_text(text)
    from .intent_router import ctx_set as _intent_ctx_reset
    _intent_ctx_reset(None)   # 同 task 二次审查（files_check）时不留上一轮的档位
    if model and _all_local_alias(model):
        return {"action": "allow", "name": "local_alias_bypass"}, {}, None

    # F1: inspect_text 也走线程池——512KB 预算内的对抗文本（海量 b64/hex 候选、
    # 数字簇）单次仍有 ~150ms 纯 CPU，同步跑会阻塞事件循环（其他用户一起停）。
    # 预算(上) + 线程池(本行) 双保险：单请求 CPU 有界，loop 永不被审查堵住。
    text_findings = await run_in_threadpool(inspect_text, text)
    if extra_findings:
        text_findings.update(extra_findings)
    _note_risk(text_findings)
    session_id = (extract_session_id(request.headers) if request is not None else None) or session_hint
    # session store 同步 Redis：挪出事件循环（2026-10-27，见 test_loop_responsive）
    session_conf = await run_in_threadpool(_is_session_confidential, session_id)
    # 文件名污染（客户端无感会话替代）：正文提及同 key 下曾判机密的文件名即命中。
    # key 取身份绑定键（与限流同口径）；干跑/无身份时 key 为空即跳过。
    _taint_name = ""
    _men = _mentioned_data_filename(text)
    if _men and request is not None:
        _kid = _identity_bind_key(request)
        if _kid and await run_in_threadpool(get_session_store().is_tainted_file, _kid, _men):
            _taint_name = _men
    ctx = {

        "text": text,
        "file": _review_file_ctx(file_ctx),
        "findings": text_findings,
        "session": {"confidential": session_conf},
        "tainted_file": {"hit": bool(_taint_name), "name": _taint_name},
        # 规则可匹配维度（管理台 5.2 匹配方式构造器）：模型名/客户端登记名/本地时段
        "model": model or "",
        "client": {"key": (_ident.name if (_ident := current_identity()) else "")},
        "time": {"hour": datetime.now().hour},

    }
    decision = decide(load_policy(), ctx)
    # M1 fail-safe：超审查预算（头部已被截）⇒ 整条转本地，内容不出境；不静默放行。
    if _overflow and decision.get("action") == "allow":
        decision = _route_local_decision(
            "review_text_overflow",
            f"审查文本超预算（> {_review_text_budget()} 字符），整条不出境")
    # 解码层独有关键词：L2 只能看到编码串（实测判 NORMAL 放行）⇒ 直接转本地，不送 L2
    _enc = _encoded_keyword_decision(text_findings)
    if _enc and decision.get("action") == "allow":
        decision = _enc
    if request is not None and decision.get("action") != "allow":
        request.state.gw_block_reason = f"l1:{decision.get('name') or 'rule'}"


    l2_result = None

    # 复杂度路由（别名组 intent=1）：与 L2 判密并行跑，判不出来就不改候选顺序。
    # 只判「最后一条用户消息」（同一轮 tool 回合反复携带同一段文字 ⇒ 缓存命中，只判一次）。
    _intent_task = None
    if decision.get("action") == "allow":
        try:
            from .intent_router import plan as _intent_plan
            _intent_task = _intent_plan(model, _intent_source(l2_chunks, l2_text, text))
        except Exception:
            _intent_task = None

    # 2026-10-19：/v1/embeddings 已收口到本函数（灰区 L2 与 chat 同源，见 embeddings 端点）。
    # files_check 的单块文件式 L2 走自己的路径（门槛同为灰区，见 files_check）。
    _dec_blocks = l2_chunks or [(_L2_SCOPE_LAST_USER, l2_text or text)]
    if (decision.get("action") == "allow"
            and (_l2_always() or _l2_gray_trigger(text_findings))
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
            if is_degraded(l2_result):
                # 降级块归因：没有块产出结论，scope 不得归给首块（会像「首块拦的」）；
                # 真实归因 = degraded_scopes（哪些块失败），审计页一并渲染
                l2_result["scope"] = None
            else:
                l2_result["scope"] = _hit
            l2_result["scopes_run"] = [s for s, _ in _dec_blocks]
            l2_result.setdefault("degraded_scopes", [])

        # A/B 影子（2026-10-26）：AI_GATEWAY_L2_HISTORY_SHADOW 开且本次真跑了 history 块时，
        # 再判一次「无 history」口径，只留痕（l2.history_shadow 进审计）不改判决。
        # 影子期多轮请求在主判定后串行多一次 L2（~200ms），实验结束关 env 即恢复。
        if (os.getenv("AI_GATEWAY_L2_HISTORY_SHADOW", "false") or "").strip().lower() in ("1", "true", "yes", "on") \
                and any(_s == _L2_SCOPE_HISTORY for _s, _ in _dec_blocks) \
                and isinstance(l2_result, dict):
            _rest = [(_s, _t) for _s, _t in _dec_blocks if _s != _L2_SCOPE_HISTORY]
            if _rest:
                try:
                    if len(_rest) == 1:
                        _hs = await small_classify(_rest[0][1], filename="", headers=[], sheet_names=[])
                    else:
                        _hs = await classify_chunks([_t for _s, _t in _rest],
                                                    filename="", headers=[], sheet_names=[],
                                                    scopes=[_s for _s, _ in _rest])
                    l2_result["history_shadow"] = {
                        "label": (_hs or {}).get("label"),
                        "confidence": (_hs or {}).get("confidence"),
                        "reason": (_hs or {}).get("reason"),
                        "latency_ms": (_hs or {}).get("latency_ms"),
                        "degraded": bool((_hs or {}).get("degraded")),
                    }
                except Exception as e:
                    l2_result["history_shadow"] = {"error": str(e)[:120]}

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

    if _intent_task is not None:
        from .intent_router import apply_sticky as _intent_sticky
        from .intent_router import ctx_set as _intent_ctx_set
        if decision.get("action") != "allow":
            # 判决已转本地/拦截 ⇒ 别名组根本不参与路由，别再等这次判定（本地调用已发出，取消即可）
            _intent_task.cancel()
            _intent_ctx_set(None)
        else:
            try:
                # 会话级档位粘性：窗口内只升不降（换模型=废掉上一模型的 prompt cache）
                _intent_ctx_set(await _intent_sticky(session_id, await _intent_task))
            except Exception:
                _intent_ctx_set(None)
    return decision, text_findings, l2_result


def _first_chunk_fb(prov):
    """首字节前 5xx 降级：返回 (local_prov, fb_decision) 或 None。"""
    cfg = load_routing()
    lp = cfg.local_provider()
    if lp is None or prov.name == lp.name:
        return None
    return lp, {"name": "first_chunk_fallback", "priority": 0,
                "action": "route_local", "target": {"provider": lp.name}}


def _route_local_decision(name: str, reason, **extra) -> dict:
    """route_local 判决统一形状（L2 判密 / L2 不可用 / 解码层 / 超预算 共用）。"""
    d = {"name": name, "priority": 15, "action": "route_local",
         "target": {"provider": load_routing().default_local}, "reason": reason}
    d.update(extra)
    return d


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
        return _route_local_decision("l2_confidential", l2_result.get("reason"),
                                     confidence=l2_result.get("confidence"))
    if is_injection(l2_result):
        # 提示注入命中（is_injection）：同 fail-closed 口径转内网 —— 注入指令即高风险，
        # 即使载荷源自 benign 话术也可能携带机密数据
        return _route_local_decision("l2_injection", l2_result.get("reason") or "prompt injection detected",
                                     confidence=l2_result.get("injection_confidence") or 0.0)
    if is_degraded(l2_result):
        if (os.getenv("AI_GATEWAY_L2_FAIL_MODE", "local") or "local").strip().lower() == "open":
            return None
        return _route_local_decision(
            "l2_unavailable", f"L2 不可用（{l2_result.get('degraded_reason') or 'unknown'}），已降级到本地",
            confidence=0.0)
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
    from .alias_router import ctx_now
    return ctx_now()


def _intent_meta() -> dict | None:
    """复杂度判定留痕（仅开了 intent 的别名组有值）：判成几档、耗时、是否缓存命中。

    sticky=true = 本次生效档位由会话粘性抬升（或判定失败时沿用会话档），见 intent_router.apply_sticky。
    """
    try:
        from .intent_router import ctx_info
        c = ctx_info()
        if not c:
            return None
        return {"level": c.get("level"), "ms": c.get("ms"), "cached": bool(c.get("cached")),
                "sticky": bool(c.get("sticky"))}
    except Exception:
        return None


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
        "intent": _intent_meta(),
        "layer": "L2" if l2_result and decision.get("name") in ("l2_confidential", "l2_unavailable", "l2_injection") else "L1",
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
        # 命中构成同点暂存（浅拷贝：_review_text 稍后会往原 dict 挂 l2 键）；
        # 无命中的零分行不落，省每行 ~200B 空 JSON。
        if tf and (tf.get("risk_score") or tf.get("keyword_hits") or tf.get("findings")):
            _CURRENT_FINDINGS.set(dict(tf))
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


async def _upstream_error_detail_response(exc, msg: str, ctx_overflow: bool, prov, decision, request,
                                    *, type_: str) -> HTTPException:
    """把上游错误如实转成 HTTPException（并落审计）。

    请求已在本地上游时本地降级无从谈起，必须如实上报：
      真上下文溢出 -> 413 prompt_too_long（带真实 message）
      其他（超时/5xx） -> 原状态码 upstream_error（带真实 message）
    """
    try:
        await log_upstream_failure(request, type_=type_, status=getattr(exc, "status_code", 0),
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


async def log_upstream_failure(request, *, type_: str, status, action=None, rule=None,
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
        # log_entry 同步打 Redis：挪出事件循环（2026-10-27，见 test_loop_responsive）
        await run_in_threadpool(log_entry, entry, count=count)
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

    # 模型列记实际：别名请求放行/本地路由时 model 还是别名（如 Auto-AB），
    # 这里按落点 provider 解析成真实模型（deepseek-flash）。无落点（拦截/失败）保持原样。
    if entry.get("model") and entry.get("provider"):
        try:
            entry["model"] = _resolve_actual_model(entry.get("model"), entry.get("provider"))
        except Exception:
            pass

    # 显式 UTC 落库，不依赖宿主 TZ（展示层 to_beijing +8h）
    entry["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
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
    if entry.get("findings") is None:
        _ff = _CURRENT_FINDINGS.get()
        if _ff is not None:
            entry["findings"] = _ff
    if entry.get("file_findings") is None:
        _fff = _CURRENT_FILE_FINDINGS.get()
        if _fff:
            entry["file_findings"] = _fff
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


async def log_stream_error(request, *, type_: str, status, action=None, rule=None,
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
        await log_upstream_failure(request, type_=type_, status=status, action=action, rule=rule,
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


def _entry_status(e) -> int:
    """审计行状态码：优先后端显式留痕（status_code/upstream_error），否则按 action/rule 反推。"""
    for _k in ("status_code", "status", "upstream_error"):
        _v = e.get(_k)
        if isinstance(_v, int) and _v > 0:
            return _v
        if isinstance(_v, str) and _v.strip().isdigit():
            return int(_v)
    return _infer_status(e)


def _entry_blocked(entry: dict) -> bool:
    """审计条目是否算「拦截」——口径唯一实现在 stat_scope.is_blocked()。

    审计条目不带 status_code，用 _infer_status() 反推（action=block 或
    rule=egress_denied -> 403），再交给同一条判定式。审计页 KPI、可视化页
    c_block、趋势红柱三处共用本函数，避免同页自相矛盾。
    """
    return is_blocked(entry.get("action"), _infer_status(entry))


def _normalize_usage(u) -> tuple:
    """上游 usage -> (prompt, completion, cached_read, cache_creation)，非负 int。

    兼容 OpenAI(prompt/completion_tokens) 与 Anthropic(input/output_tokens)。
    cached 取 prompt_tokens_details.cached_tokens（OpenAI/DeepSeek）或
    cache_read_input_tokens（Anthropic）；creation 取 cache_creation_input_tokens
    （Anthropic；OpenAI 系无此档）；取不到记 0（历史行亦为 0，计费按全价算）。
    非流式取完整 JSON 的 usage；流式由中间件 SSE 扫描提取（见 _parse_sse_usage_event）。
    """
    try:
        d = dict(u or {})
    except Exception:
        return 0, 0, 0, 0
    def _int(*keys):
        for k in keys:
            try:
                v = int(d.get(k) or 0)
            except (TypeError, ValueError):
                continue
            if v > 0:
                return v
        return 0
    p = _int("prompt_tokens", "input_tokens")
    c = _int("completion_tokens", "output_tokens")
    det = d.get("prompt_tokens_details")
    det = det if isinstance(det, dict) else {}
    try:
        cached = int(det.get("cached_tokens") or d.get("cache_read_input_tokens") or 0)
    except (TypeError, ValueError):
        cached = 0
    try:
        creation = int(d.get("cache_creation_input_tokens") or 0)
    except (TypeError, ValueError):
        creation = 0
    return p, c, max(0, min(cached, p)), max(0, min(creation, p))


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
            out["p"], out["c"], out["h"], out["w"] = _normalize_usage(usage)
        # usage-only chunk（choices 为空）且客户端未主动要求 -> 剥离不转发
        if not client_wants_usage and obj.get("choices") == []:
            return b""
        return ev
    except Exception:
        return ev



# ---------------------------------------------------------------- 端点


# 静态/下载路由已迁移到 static_router（见 src/gateway/static_router.py）
# /health 已迁移到 health_router（见 src/gateway/health_router.py）


@app.get("/v1/models")
async def list_models(identity: Identity = Depends(_bind_identity_ctx)):
    """OpenAI compatible model list — 对外别名组 + 内网主力模型
    （model_policy.internal_models，可直连点名）+ 用户自注册模型。

    列表构造在 admin_api.build_public_models（与管理端 /models/public 单一真源，
    两端必然一致），末尾按 AI_GATEWAY_PUBLIC_MODELS 白名单收窄展示（空=全量；
    只影响可发现性，点名路由不受影响）。上游真实模型目录不再对外暴露
    （真实模型名请求仍兼容）。
    """
    return {"object": "list", "data": _admin_api.build_public_models()}


@app.post("/v1/models/register")

async def register_model(req: RegisterReq, request: Request, identity: Identity = Depends(_bind_identity_ctx)):
    """

    用户自定义模型注册：提交 name + endpoint + key，网关校验域名白名单后持久化。

    后续请求用 model=<name> 即可走该模型（仍过安全审查，不通过降级本地）。

    """
    session_id = extract_session_id(request.headers)
    owner = identity.name or "anonymous"
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
    await run_in_threadpool(log_entry, {"client_ip": _resolve_client_ip(request), "requested_model": "", "type": "model_register", "name": req.name, "owner": owner,

               "base_url": req.base_url, "api_mode": req.api_mode,
               "session_id": session_id,})

    return {"object": "model", "id": m.name, "gateway": m.public()}


@app.delete("/v1/models/{name}")

async def unregister_model(name: str, request: Request, identity: Identity = Depends(_bind_identity_ctx)):
    """注销用户自定义模型（仅本人或管理员可删；管理员 = key 名在 AI_GATEWAY_ADMIN_KEY_NAMES 里）"""
    store = get_store()
    m = store.get(name)
    if m is None:

        raise HTTPException(404, detail={"type": "not_found", "message": f"模型 {name} 不存在"})

    caller = identity.name or "anonymous"
    _admin_names = {n.strip() for n in os.getenv("AI_GATEWAY_ADMIN_KEY_NAMES", "").split(",") if n.strip()}
    is_admin = identity.name in _admin_names
    if not is_admin and m.owner != caller:

        raise HTTPException(403, detail={"type": "forbidden",

                                         "message": "只能注销本人注册的模型"})

    store.delete(name)
    await run_in_threadpool(log_entry, {"client_ip": _resolve_client_ip(request), "requested_model": "", "type": "model_unregister", "name": name, "owner": m.owner, "caller": caller})
    return {"object": "model", "id": name, "deleted": True}


def _err_provider(exc, prov) -> str:
    """错误信封 provider：RoutingError 自带（别名层失败填别名名）优先，
    否则沿用预解析的 prov（别名 503 曾误标 deepseek）。"""
    return (getattr(exc, "provider", "") or "") or (prov.name if prov else "")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, identity: Identity = Depends(_rate_limit_chat)):
    # F1: body 改手动解析（FastAPI 内部解析 = json.loads + pydantic 同步跑在
    # 事件循环上，1.17GB body 实测阻塞 2.2s）——上限 413 + 双阶段线程池。
    payload = await _json_body(request)
    try:
        req = await run_in_threadpool(ChatReq.model_validate, payload)
    except ValidationError as e:
        raise HTTPException(422, detail=e.errors()[:10])
    # workbuddy 约 6 成会话不发 X-Session-ID：回落首条消息指纹（见 _chat_session_id）
    session_id = extract_session_id(request.headers) or _chat_session_id(req.messages)
    _bundle = _extract_chat_bundle(req.messages)
    text = _bundle["text"]
    # F3: tools 定义（description/parameters）并入审查文本——曾是密文免审通道
    _tt = tools_text(getattr(req, "tools", None))
    if _tt:
        text = text + "\n" + _tt
    audit_preview = _bundle["audit_preview"]
    inline_text, inline_bad, inline_findings, inline_remote = await _inline_media_scan("chat", req.messages)
    if inline_text:
        text = text + "\n" + inline_text
    l2_blocks = _select_l2_scopes("chat", req.messages, text)
    _media_extra = {}
    if inline_bad:
        _media_extra["ocr_empty_and_image"] = True
    if inline_remote:
        _media_extra["remote_attachment"] = True
    decision, text_findings, l2_result = await _review_text(
        text, request, _media_extra or None,
        l2_chunks=l2_blocks, file_ctx={"text": inline_text} if inline_text else None,
        model=str(req.model or ""), session_hint=session_id)
    # 把内联附件预检 findings 写入审计（FindingsDetail 文件预检区），镜像 /v1/files/check。
    _note_file_findings(inline_findings)


    # block -> 默认自动降级到本地模型（routing.on_block=reject 时才 403）
    decision, downgraded = _block_to_fallback(decision)
    _mark_gw_action(request, decision)
    _mark_pre_upstream(request)
    _mentioned = _mentioned_data_filename(text)
    _session_name = "chat-inline-image" if (inline_text or inline_bad) else ("chat-mentioned-file:" + _mentioned if _mentioned else "")
    await run_in_threadpool(_mark_hit_context, session_id, _identity_bind_key(request), _session_name, _mentioned, decision)
    if decision.get("action") == "block":

        await run_in_threadpool(log_entry, {"client_ip": _resolve_client_ip(request), "requested_model": str(req.model or ""), "type": "chat", "action": "block", "rule": decision.get("name"), "text_preview": audit_preview, "l2": l2_result,
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
                        fb = _first_chunk_fb(prov)
                        if fb is not None:
                            lp, fb_decision = fb
                            try:

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

                                await run_in_threadpool(log_entry, {"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(req.model or ""),

                                    "type": "chat", "action": "route_local", "rule": "first_chunk_fallback",
                                    "provider": lp.name, "local": True, "model": lp.default_model,
                                    "text_preview": audit_preview, "downgraded_from": "upstream_error", "stream": True})

                                return

                            except Exception:

                                await log_stream_error(request, type_="chat", status=getattr(exc, "status_code", 0) or 502,
                                     action=decision.get("action"), rule=decision.get("name"),
                                     provider=prov, model=model, kind="first_chunk_fallback_failed",
                                     session_id=session_id, text_preview=audit_preview)

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
                                await run_in_threadpool(log_entry, {"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(req.model or ""), 

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
                            await log_stream_error(request, type_="chat", status=getattr(exc, "status_code", 0) or 502,
                                 action=decision.get("action"), rule=decision.get("name"),
                                 provider=prov, model=model, kind="upstream_fallback_failed",
                                 session_id=session_id, text_preview=audit_preview)
                            err = {"error": {"type": (getattr(exc, "type_", "") or "routing_error"), "provider": _err_provider(exc, prov), "message": str(exc) + f" (fallback failed: {e2})"}}
                            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode("utf-8")
                            yield b"data: [DONE]\n\n"
                            return

                    # non-fallback error
                    await log_stream_error(request, type_="chat", status=getattr(exc, "status_code", 0) or 502,
                         action=decision.get("action"), rule=decision.get("name"),
                         provider=prov, model=model, kind="upstream",
                         session_id=session_id, text_preview=audit_preview)
                    err = {"error": {"type": (getattr(exc, "type_", "") or "routing_error"), "provider": _err_provider(exc, prov), "message": str(exc)}}
                    yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode("utf-8")
                    yield b"data: [DONE]\n\n"

                except Exception as exc:

                    # 兜底：任何异常都不允许逃出生成器，否则客户端看到的是被掐断的流
                    await log_stream_error(request, type_="chat", status=502,
                         action=decision.get("action"), rule=decision.get("name"),
                         provider=prov, model=model, kind="internal",
                         session_id=session_id, text_preview=audit_preview)
                    err = {"error": {"type": "internal_error", "message": f"gateway stream error: {exc}"}}
                    yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode("utf-8")
                    yield b"data: [DONE]\n\n"


            await run_in_threadpool(log_entry, {"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(req.model or ""), 

                "type": "chat", "action": decision.get("action"), "rule": decision.get("name"),
                "provider": prov.name, "local": prov.local, "model": model,
                "text_preview": audit_preview, "layer": "L2" if l2_result and decision.get("name") in ("l2_confidential", "l2_unavailable", "l2_injection") else "L1",
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

                        raise await _upstream_error_detail_response(exc, msg, _ctx_overflow, prov, decision, request, type_="chat")

                except HTTPException:

                    raise

                except Exception as e2:

                    await log_upstream_failure(request, type_="chat", status=exc.status_code,

                                          action=decision.get("action"), rule=decision.get("name"),
                                          provider=prov, model=model, text_preview=audit_preview, session_id=session_id)

                    raise HTTPException(exc.status_code, detail={"type": (getattr(exc, "type_", "") or "routing_error"), "provider": _err_provider(exc, prov), "message": str(exc) + f" (fallback failed: {e2})"})

            else:

                await log_upstream_failure(request, type_="chat", status=exc.status_code,

                                      action=decision.get("action"), rule=decision.get("name"),
                                      provider=prov, model=model, text_preview=audit_preview, session_id=session_id)

                raise HTTPException(exc.status_code, detail={"type": (getattr(exc, "type_", "") or "routing_error"), "provider": _err_provider(exc, prov), "message": str(exc)})


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
        await run_in_threadpool(log_entry, {"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(req.model or ""), 

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

        await log_upstream_failure(request, type_="chat", status=exc.status_code,

                              action=decision.get("action"), rule=decision.get("name"),
                              provider=prov, model=model, text_preview=audit_preview, session_id=session_id)

        raise HTTPException(exc.status_code, detail={"type": (getattr(exc, "type_", "") or "routing_error"), "provider": _err_provider(exc, prov), "message": str(exc)})


@app.post("/v1/embeddings")

async def embeddings(request: Request, identity: Identity = Depends(_rate_limit_embed)):
    # 2026-10-19 收口：与 chat 主链路一致 —— body 上限 + 线程池解析 + _review_text。
    # 此前是内联 L1 副本：不跑 L2 灰区、无文件名污染、inspect_text 同步跑事件循环、
    # 无 body 上限（EmbeddingReq 裸解析）⇒ 分叉面全部补齐。
    body = await _json_body(request)
    try:
        req = await run_in_threadpool(EmbeddingReq.model_validate, body)
    except ValidationError as e:
        raise HTTPException(422, detail=e.errors()[:10])
    raw = req.input
    text = " ".join(raw) if isinstance(raw, list) else str(raw or "")
    session_id = extract_session_id(request.headers)
    decision, _text_findings, l2_result = await _review_text(
        text, request, model=str(req.model or ""))

    decision, downgraded = _block_to_fallback(decision)
    _mark_gw_action(request, decision)
    _mark_pre_upstream(request)
    if decision.get("action") == "block":

        await run_in_threadpool(log_entry, {"client_ip": _resolve_client_ip(request), "requested_model": str(req.model or ""),
                   "type": "embedding", "action": "block", "rule": decision.get("name"),
                   "text_preview": text[:200], "l2": l2_result, "session_id": session_id})
        raise HTTPException(403, detail={"type": "policy_violation", "rule": decision.get("name")})


    payload = req.model_dump()
    payload["input"] = raw
    try:

        prov = None
        model = ""
        prov, model, notes = resolve(decision, payload, request.headers, kind="embedding")
        result = await route_embeddings(payload, decision, request.headers)

    except RoutingError as exc:

        await log_upstream_failure(request, type_="embedding", status=exc.status_code,

                              action=decision.get("action"), rule=decision.get("name"),
                              provider=prov, model=model, text_preview=text[:200])

        raise HTTPException(exc.status_code, detail={"type": (getattr(exc, "type_", "") or "routing_error"), "provider": _err_provider(exc, prov), "message": str(exc)})


    result["gateway"] = _gateway_meta(decision, prov, model, notes, l2_result, downgraded)
    await run_in_threadpool(log_entry, {"client_ip": _resolve_client_ip(request), "requested_model": str(req.model or ""), 

        "type": "embedding", "action": decision.get("action"), "rule": decision.get("name"),
        "provider": prov.name, "local": prov.local, "model": model, "text_preview": text[:200],
        "l2": l2_result,

    })
    return JSONResponse(result)


# ---------------------------------------------------------------- Anthropic Messages（ccswitch / Claude Code）

# ccswitch 把 ANTHROPIC_BASE_URL 指向本网关后，Claude Code 会发

# POST {base}/v1/messages（Anthropic 协议，可能流式）。审查与降级逻辑同 chat。


@app.post("/v1/messages")

async def anthropic_messages(request: Request, identity: Identity = Depends(_rate_limit_messages)):
    session_id = extract_session_id(request.headers)
    body = await _json_body(request)
    text = anthropic_text(body)
    audit_preview = _audit_preview_anthropic(body, text)
    inline_text, inline_bad, inline_findings, inline_remote = await _inline_media_scan("anthropic", body)
    if inline_text:
        text = text + "\n" + inline_text
    l2_blocks = _select_l2_scopes("anthropic", body, text)
    _media_extra = {}
    if inline_bad:
        _media_extra["ocr_empty_and_image"] = True
    if inline_remote:
        _media_extra["remote_attachment"] = True
    decision, text_findings, l2_result = await _review_text(
        text, request, _media_extra or None,
        l2_chunks=l2_blocks, file_ctx={"text": inline_text} if inline_text else None,
        model=str((body or {}).get("model") or ""))
    # 把内联附件预检 findings 写入审计（FindingsDetail 文件预检区），镜像 /v1/files/check。
    _note_file_findings(inline_findings)


    decision, downgraded = _block_to_fallback(decision)
    _mark_gw_action(request, decision)
    _mark_pre_upstream(request)
    _mentioned = _mentioned_data_filename(text)
    await run_in_threadpool(_mark_session_hit, session_id, decision, "messages-inline-image" if (inline_text or inline_bad) else ("messages-mentioned-file:" + _mentioned if _mentioned else ""))
    await run_in_threadpool(_mark_taint_hit, _identity_bind_key(request), _mentioned, decision)
    if decision.get("action") == "block":

        await run_in_threadpool(log_entry, {"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(body.get("model") or ""), "type": "messages", "action": "block", "rule": decision.get("name"), "text_preview": audit_preview,
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

            await log_upstream_failure(request, type_="messages", status=exc.status_code,

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
                    fb = _first_chunk_fb(prov)
                    if fb is not None:
                        lp, fb_decision = fb
                        try:

                            async for chunk in route_messages_stream(body, fb_decision, request.headers):

                                sent = True
                                yield chunk

                            await run_in_threadpool(log_entry, {"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(body.get("model") or ""),

                                "type": "messages", "action": "route_local", "rule": "first_chunk_fallback",

                                "provider": lp.name, "local": True, "text_preview": audit_preview,

                                "downgraded_from": "upstream_error", "stream": True})

                            return

                        except Exception:

                            await log_stream_error(request, type_="messages", status=getattr(exc, "status_code", 0) or 502,
                                 action=decision.get("action"), rule=decision.get("name"),
                                 provider=prov, model=model, kind="first_chunk_fallback_failed",
                                 session_id=session_id, text_preview=audit_preview)
                await log_stream_error(request, type_="messages", status=getattr(exc, "status_code", 0) or 502,
                     action=decision.get("action"), rule=decision.get("name"),
                     provider=prov, model=model, kind="upstream",
                     session_id=session_id, text_preview=audit_preview)
                yield _sse({"type": "error", "error": {"type": "gateway_error", "message": str(exc)},
                "session_id": session_id,}, "error")

            except Exception as exc:

                await log_stream_error(request, type_="messages", status=502,
                     action=decision.get("action"), rule=decision.get("name"),
                     provider=prov, model=model, kind="internal",
                     session_id=session_id, text_preview=audit_preview)
                yield _sse({"type": "error", "error": {"type": "gateway_error", "message": f"gateway stream error: {exc}"},
                "session_id": session_id,}, "error")


        await run_in_threadpool(log_entry, {"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(body.get("model") or ""), 

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

        await log_upstream_failure(request, type_="messages", status=exc.status_code,

                              action=decision.get("action"), rule=decision.get("name"),
                              provider=prov, model=model, text_preview=audit_preview, session_id=session_id)

        raise HTTPException(exc.status_code, detail={"type": (getattr(exc, "type_", "") or "routing_error"),

                                                     "provider": _err_provider(exc, prov), "message": str(exc),
                                                     "session_id": session_id,})


    result["gateway"] = _meta(prov, model, notes)
    await run_in_threadpool(log_entry, {"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(body.get("model") or ""), 

        "type": "messages", "action": decision.get("action"), "rule": decision.get("name"),
        "provider": result["gateway"]["provider"], "local": result["gateway"]["local"], "model": model,
        "text_preview": audit_preview, "downgraded_from": downgraded, "filename": _mentioned,

    })
    return JSONResponse(result)


@app.post("/v1/messages/count_tokens")

async def anthropic_count_tokens(request: Request, identity: Identity = Depends(_bind_identity_ctx)):
    """Claude Code 偶尔调用的 token 估算端点；网关层面给近似值即可"""
    body = await _json_body(request)
    text = anthropic_text(body)

    # 中文约 1.5-2 token/字，保守按 len/3 估算，仅用于客户端预算
    return {"input_tokens": max(1, (len(text) + 2) // 3)}


# ---------------------------------------------------------------- OpenAI Responses（ccswitch / Codex CLI）

# Codex 的 wire_api="responses"：POST {base}/v1/responses。放行直通上游，

# 拦截/降级时转 chat.completions 打本地模型，再转回 Responses 格式。


def _codex_session_id(body):
    """codex 等 responses 客户端不发会话头：用对话首条 user 消息做指纹当 session key。

    实测（2026-09-20 dump）：codex 每轮全量重发 input、无 previous_response_id，
    首条 user 消息（environment_context+首问）在对话内不变；指纹只取用户真问题，
    剥掉 environment_context / system-reminder 等易变样板（工作目录、git 状态等），
    避免这些波动导致同一对话被判定成不同会话、会话粘性失效。
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
            # 指纹只取用户真问题：剥掉 environment_context / system-reminder 等
            # 易变样板（工作目录、git 状态、平台信息），避免这些波动导致同一对话
            # 被判成不同会话、会话粘性失效。L1/L2 审查仍用全文，不受此影响。
            first = re.sub(r"<environment_context\b.*?</environment_context>", " ", first, flags=re.S)
            first = re.sub(r"<system-reminder\b.*?</system-reminder>", " ", first, flags=re.S)
            if first.strip():
                return "codex-" + hashlib.sha256(first.encode("utf-8", "replace")).hexdigest()[:16]
    except Exception:
        pass
    return None


@app.post("/v1/responses")

async def responses_api(request: Request, identity: Identity = Depends(_rate_limit_messages)):
    body = await _json_body(request)

    # codex 不发 X-Session-ID：回落对话指纹（见 _codex_session_id）
    session_id = extract_session_id(request.headers) or _codex_session_id(body)
    text = responses_text(body)
    audit_preview = _audit_preview_responses(body, text)
    inline_text, inline_bad, inline_findings, inline_remote = await _inline_media_scan("responses", body)
    if inline_text:
        text = text + "\n" + inline_text
    l2_blocks = _select_l2_scopes("responses", body, text)
    file_ctx = extract_responses_filectx(body, text)
    if inline_text:
        file_ctx["text"] = inline_text
    _media_extra = {}
    if inline_bad:
        _media_extra["ocr_empty_and_image"] = True
    if inline_remote:
        _media_extra["remote_attachment"] = True
    decision, text_findings, l2_result = await _review_text(
        text, request, _media_extra or None,
        l2_chunks=l2_blocks, file_ctx=file_ctx, model=str(body.get("model") or ""),
        session_hint=session_id)
    # 把内联附件预检 findings 写入审计（FindingsDetail 文件预检区），镜像 /v1/files/check。
    _note_file_findings(inline_findings)


    decision, downgraded = _block_to_fallback(decision)
    _mark_gw_action(request, decision)
    _mark_pre_upstream(request)
    if responses_should_mark(decision.get("name"), decision.get("action"), file_ctx):
        _mark_file = file_ctx.get("filename") or "responses-inline"
        await run_in_threadpool(_mark_hit_context,
            session_id,
            _identity_bind_key(request),
            _mark_file,
            file_ctx.get("filename") or _mark_file,
            decision)
    if decision.get("action") == "block":

        await run_in_threadpool(log_entry, {"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(body.get("model") or ""), "type": "responses", "action": "block", "rule": decision.get("name"), "text_preview": audit_preview,
        "session_id": session_id,})
        raise HTTPException(403, detail={"type": "policy_violation", "rule": decision.get("name"),

                                         "message": "请求因机密策略被拦截，未转发至外网"})


    try:

        prov = None
        model = ""
        prov, model, notes = resolve(decision, {"model": str(body.get("model") or "")}, request.headers, kind="chat")

    except RoutingError as exc:

        await log_upstream_failure(request, type_="responses", status=exc.status_code,

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
                    fb = _first_chunk_fb(prov)
                    if fb is not None:
                        lp, fb_decision = fb
                        try:

                            async for chunk in route_responses_stream(body, fb_decision, request.headers):

                                sent = True
                                yield chunk

                            await run_in_threadpool(log_entry, {"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(body.get("model") or ""),

                                "type": "responses", "action": "route_local", "rule": "first_chunk_fallback",

                                "provider": lp.name, "local": True, "text_preview": audit_preview,

                                "downgraded_from": "upstream_error", "stream": True, "session_id": session_id})

                            return

                        except Exception:

                            await log_stream_error(request, type_="responses", status=getattr(exc, "status_code", 0) or 502,
                                 action=decision.get("action"), rule=decision.get("name"),
                                 provider=prov, model=model, kind="first_chunk_fallback_failed",
                                 session_id=session_id, text_preview=audit_preview)
                # 降级不可用/也失败：显式发 response.failed 事件收尾，Codex 前端可见，不再静默断流
                await log_stream_error(request, type_="responses", status=getattr(exc, "status_code", 0) or 502,
                     action=decision.get("action"), rule=decision.get("name"),
                     provider=prov, model=model, kind="upstream",
                     session_id=session_id, text_preview=audit_preview)
                yield _sse({"type": "response.failed", "response": {"status": "failed", "error": {"message": str(exc)}}, "session_id": session_id}, "error")

            except Exception as exc:

                await log_stream_error(request, type_="responses", status=502,
                     action=decision.get("action"), rule=decision.get("name"),
                     provider=prov, model=model, kind="internal",
                     session_id=session_id, text_preview=audit_preview)
                yield _sse({"type": "response.failed", "response": {"error": {"message": f"gateway stream error: {exc}"}}, "session_id": session_id,}, "error")


        await run_in_threadpool(log_entry, {"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(body.get("model") or ""), 

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

        await log_upstream_failure(request, type_="responses", status=exc.status_code,

                              action=decision.get("action"), rule=decision.get("name"),
                              provider=prov, model=model, session_id=session_id)

        raise HTTPException(exc.status_code, detail={"type": (getattr(exc, "type_", "") or "routing_error"),

                                                     "provider": _err_provider(exc, prov), "message": str(exc),
                                                     "session_id": session_id,})


    gw = _gateway_meta(decision, prov, model, notes, l2_result, downgraded)
    gw["findings"] = text_findings
    result["gateway"] = gw
    await run_in_threadpool(log_entry, {"l2": l2_result, "client_ip": _resolve_client_ip(request), "requested_model": str(body.get("model") or ""), 

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
    _max = int(os.getenv("AI_GATEWAY_FILES_CHECK_MAX_BYTES", str(25 * 1024 * 1024)))
    data = await file.read(_max + 1)
    if len(data) > _max:
        raise HTTPException(413, detail={"type": "file_too_large", "message": f"文件超过 {_max // (1024*1024)}MB 上限"})
    filename = file.filename or "unknown"

    # 异步线程池，避免阻塞事件循环（Excel/PDF 解析 20-100ms）
    file_info = await run_in_threadpool(inspect_file, data, filename)
    # M3（2026-10-19）：不再切前 5000 字符——inspect_file 输出本身已有上限（文本 50KB /
    # xlsx 15KB），切片只会让 >5000 字符后的机密关键词免审（垫长前置即可绕过）。
    text = file_info.get("text", "")
    text_findings = inspect_text(text)
    _note_risk(text_findings)
    session_id = extract_session_id(request.headers)
    session_conf = await run_in_threadpool(_is_session_confidential, session_id)
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
    # 与 _review_text 同口径：解码层独有关键词 L2 判不了（只见编码串）⇒ 直接转本地，
    # 否则 base64 包一层机密文件的预检会误报「审查通过：可走外网」
    _enc = _encoded_keyword_decision(text_findings)
    if _enc and decision.get("action") == "allow":
        decision = _enc
    if decision.get("action") != "allow":
        request.state.gw_block_reason = f"l1:{decision.get('name') or 'rule'}"


    # L2 小模型二次判定（文件场景：L1 为 allow 时触发，支持分块聚合应对长文档）
    # 2026-10-13 起对齐 _review_text：加灰区门槛（risk_score >= 20 才跑 L2），
    # 与 chat/messages/responses 的 L2 触发语义一致。
    l2_result = None
    if decision.get("action") == "allow" and (_l2_always() or _l2_gray_trigger(text_findings)):

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

        await log_upstream_failure(request, type_="file", status=exc.status_code,

                              action=decision.get("action"), rule=decision.get("name"),
                              provider=prov, model=model, filename=filename, ext=file_info.get("ext"), session_id=session_id)

        raise HTTPException(exc.status_code, detail={"type": (getattr(exc, "type_", "") or "routing_error"), "message": str(exc)})


    await run_in_threadpool(log_entry, {"client_ip": _resolve_client_ip(request), "requested_model": "", 

        "type": "file", "filename": filename, "ext": file_info.get("ext"),
        "action": decision.get("action"), "rule": decision.get("name"),
        "provider": prov.name, "local": prov.local, "model": model,
        "override_denied": notes.get("override_denied") if notes else None,
        "findings": file_info.get("findings"), "l2": l2_result,
        "layer": "L2" if l2_result and decision.get("name") in ("l2_confidential", "l2_unavailable", "l2_injection") else "L1",
        "downgraded_from": downgraded,
        "session_id": session_id,

    })
    await run_in_threadpool(_mark_hit_context, session_id, _identity_bind_key(request), filename, filename, decision)


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
                payload = await _json_body(request)
            except HTTPException:
                raise
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
        "session": {"confidential": await run_in_threadpool(_is_session_confidential, session_id)},

    })
    if decision.get("action") != "allow":
        request.state.gw_block_reason = f"l1:{decision.get('name') or 'rule'}"

    decision, _ = _block_to_fallback(decision)
    _mark_gw_action(request, decision)
    _mark_pre_upstream(request)


    # 非 chat 端点无法"降级到本地模型"（本地大模型没有 /v1/images 之类接口），

    # 所以审查不通过只能拒绝，绝不静默发给外网。
    if decision.get("action") != "allow":

        await run_in_threadpool(log_entry, {"client_ip": _resolve_client_ip(request), "requested_model": "", "type": "egress_blocked", "path": path, "action": decision.get("action"),

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

        raise HTTPException(502, detail={"type": (getattr(exc, "type_", "") or "routing_error"), "provider": _err_provider(exc, prov), "message": str(exc),
        "session_id": session_id,})


    await run_in_threadpool(log_entry, {"client_ip": _resolve_client_ip(request), "requested_model": "", "type": "egress", "path": path, "provider": prov.name, "local": prov.local,

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
  /* 不引外部字体（内网无出口客户端 @import 是 render-blocking 依赖，安全产品登录页不自带外网依赖）；
     font-family 已有 system-ui 兜底。深浅两套：手动切深色跟随系统偏好（登录页是独立模板，不走 data-theme） */
  :root{--bg:#F8FAFC;--panel:#FFFFFF;--line:#E2E8F0;--line-strong:#CBD5E1;--muted:#64748B;--text:#0F172A;--accent:#1E40AF;--accent-600:#1E3A8A;--err:#DC2626}
  @media (prefers-color-scheme: dark){
    :root{--bg:#0B1220;--panel:#111A2C;--line:#2A3A55;--line-strong:#3B4E70;--muted:#8CA3C7;--text:#E6EEFB;--accent:#3B82F6;--accent-600:#2563EB;--err:#F87171}
    .card{box-shadow:0 8px 30px rgba(0,0,0,.5)}
    input{background:#0B1220}
    input::placeholder{color:#5B7396}
  }
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
        raise HTTPException(429, "登录尝试过于频繁，请 5 分钟后重试")
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
        secure=True,  # 2026-10-21 起网关只讲 https（uvicorn --ssl-certfile），cookie 不走明文
        path="/",
    )
    return resp



def _audit_key_name_map() -> dict:
    """token_masked -> 登记名：读时 enrichment 用（审计写时只存脱敏 key，反解不出名）。

    正向建表：mask_key(key_plain) -> name；模糊条目（含 *）的 mask 无意义，跳过。
    历史行/新行、Redis/MySQL 全覆盖，零 schema 变更；fail-open。"""
    try:
        from .admin_store import get_admin_store, mask_key as _mask_key
        m = {}
        for k in get_admin_store().list_api_keys_raw() or []:
            kp = (k.get("key_plain") or "").strip()
            nm = (k.get("name") or "").strip()
            if not kp or "*" in kp or not nm:
                continue
            m.setdefault(_mask_key(kp), nm)
        return m
    except Exception:
        return {}


def _enrich_audit_key_name(rows) -> None:
    """就地补 key_name（未知/未登记留空，调用方按 token_masked 回退展示）。"""
    mp = _audit_key_name_map()
    if not mp:
        return
    for e in rows or []:
        try:
            if isinstance(e, dict) and not e.get("key_name"):
                e["key_name"] = mp.get(e.get("token_masked") or "", "")
        except Exception:
            pass


def _audit_row_match_q(e, ql) -> bool:
    """审计行全文搜索单行判定（列表接口与 CSV 导出共用一套口径）。"""
    # “local/external”只是前端渲染标签（local 布尔），落库字段里没有这个串；
    # 搜这几个词时按路由归属匹配，否则 external 永远搜不到（与表格 Tag 口径一致：缺 local 视为 external）。
    q_route = None
    if ql in ("external", "外部", "外发"):
        q_route = False
    elif ql in ("local", "本地"):
        q_route = True
    if q_route is not None:
        v = e.get("local")
        if v is None:
            v = e.get("local_flag")          # MySQL 行用 local_flag（热窗行用 local）
        _is_local = v is True or v in (1, "1", "true", "True")
        if _is_local == q_route:
            return True
    for k in ("text_preview", "filename", "requested_model", "model", "provider",
              "rule", "client_ip", "type", "token_masked", "key_name", "session_id"):
        v = e.get(k)
        if v and ql in str(v).lower():
            return True
    # 规则名按中文显示名一起匹配：用户搜「L2 判密」要能命中 rule=l2_confidential
    if ql in rule_match_text(e.get("rule") or "").lower():
        return True
    # l2 判定 JSON（含影子 shadow_*）：搜 shadow_degraded / CONFIDENTIAL 等可定位 L2 记录
    try:
        import json as _js
        if ql in _js.dumps(e.get("l2") or {}, ensure_ascii=False).lower():
            return True
    except Exception:
        pass
    return False


def _audit_shadow_mismatch(e) -> bool:
    """主影判定不一致（影子验收筛选用；无影子/主判定降级的不算）。"""
    l2 = e.get("l2")
    if not isinstance(l2, dict):
        return False
    sl = l2.get("shadow_label")
    return sl in ("CONFIDENTIAL", "NORMAL") and not l2.get("degraded") and sl != l2.get("label")


def _audit_filter_rows(rows, *, ip="", token="", action="", provider="", rule="", type_="",
                       q="", shadow="", since_dt=None, until_dt=None, status=""):
    """审计行统一筛选（列表接口与 CSV 导出共用，避免两处口径漂移）。

    ⚠️ 筛选项是**叠加**（AND）关系：时间窗必须作用在已按 action/rule 等筛过的集合上。
    早前实现从 all_entries 重算时间窗，把 action 过滤整个冲掉 → 筛「本地路由」
    却看到放行行（顶层 total 也与 mysql KPI 对不上）。
    """
    out = list(rows or [])
    if ip:
        ip = ip.strip()
        if ip:
            out = [e for e in out if ip in (e.get("client_ip") or "")]
    if token:
        token = token.strip()
        if token:
            _tl = token.lower()
            out = [e for e in out if _tl in str(e.get("token_masked") or "").lower()
                   or _tl in str(e.get("key_name") or "").lower()]
    if action:
        action = action.strip()
        if action:
            out = [e for e in out if e.get("action") == action]
    if provider:
        provider = provider.strip()
        if provider:
            out = [e for e in out if (e.get("provider") or "") == provider]
    if rule:
        rule = rule.strip()
        if rule:
            out = [e for e in out if (e.get("rule") or "") == rule]
    if type_:
        type_ = type_.strip()
        if type_:
            out = [e for e in out if (e.get("type") or "") == type_]
    # 时间窗：解析不出时间的行在有窗口时丢弃（与旧口径一致）
    if since_dt or until_dt:
        tmp = []
        for e in out:
            t = _parse_window_ts(e.get("time") or e.get("ts") or "")
            if not t:
                continue
            if since_dt and t < since_dt:
                continue
            if until_dt and t > until_dt:
                continue
            tmp.append(e)
        out = tmp
    if q:
        ql = q.strip().lower()
        if ql:
            out = [e for e in out if _audit_row_match_q(e, ql)]
    if (shadow or "").strip() == "mismatch":
        out = [e for e in out if _audit_shadow_mismatch(e)]
    # 状态码：支持精确值（403/429/502/503…）与类（2xx/3xx/4xx/5xx）；类不匹配
    # upstream_error 留痕的失败行不会漏（按 _entry_status 统一解析）。
    _st = (status or "").strip().lower()
    if _st:
        if _st.endswith("xx"):
            _lo = int(_st[0]) * 100
            out = [e for e in out if _lo <= _entry_status(e) <= _lo + 99]
        else:
            try:
                _st = int(_st)
                out = [e for e in out if _entry_status(e) == _st]
            except ValueError:
                pass
    return out


# 审计列表路由已迁移到 audit_router（见 src/gateway/audit_router.py）

@app.get("/admin", dependencies=[Depends(admin_page_auth)])
async def admin_legacy_redirect():
    """旧审计 HTML 页已退役，统一跳转 Admin Console SPA。"""
    return RedirectResponse("/admin/app", status_code=302)


# csv/audit/status 路由已迁移到 audit_router（见 src/gateway/audit_router.py）

def _parse_window_ts(v, now=None, is_until=False, default=None, naive_as_utc=True):
    """时间窗参数 -> UTC 时基朴素 datetime（唯一实现）。

    存储侧一律显式 UTC 落库（gmtime/utcnow，不依赖宿主 TZ），比较两侧必须同基：
    - datetime 实例 / ISO 带显式偏移（Z、+08:00）：精确换算到 UTC
    - "1h"/"7d" 相对窗：以 utcnow() 为基准
    - 朴素串（无偏移）：naive_as_utc=True（解析存储值）按 UTC 墙钟原样；
      naive_as_utc=False（用户输入）按北京墙钟解释（-8h），与展示口径一致
    - is_until + 纯日期 = 当日 23:59:59.999999（对应时基）。解析失败/空 = default
    """
    if v is None or (isinstance(v, str) and not v.strip()):
        return default

    def _naive(dt):
        return dt if naive_as_utc else dt - timedelta(hours=8)

    def _aware(dt):
        return dt.astimezone(timezone.utc).replace(tzinfo=None)

    if isinstance(v, datetime):
        return _aware(v) if v.tzinfo is not None else _naive(v)
    v = str(v).strip()
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if v.endswith("h") or v.endswith("d"):
        try:
            n_ = int(v[:-1])
            return now - (timedelta(hours=n_) if v.endswith("h") else timedelta(days=n_))
        except (TypeError, ValueError):
            pass
    try:
        if "T" in v:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return _aware(dt) if dt.tzinfo is not None else _naive(dt)
        if len(v) == 10:
            dt = datetime.strptime(v, "%Y-%m-%d")
            if is_until:
                dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
            return _naive(dt)
        if " " in v:
            dt = datetime.fromisoformat(v)
            return _aware(dt) if dt.tzinfo is not None else _naive(dt)
        return _naive(datetime.strptime(v, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return default



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
from . import health_router as _health_router
app.include_router(_health_router.router)
from . import static_router as _static_router
app.include_router(_static_router.router)
from . import metrics_router as _metrics_router
app.include_router(_metrics_router.router)
from . import audit_router as _audit_router
app.include_router(_audit_router.router)


class _AdminStaticFiles(StaticFiles):
    """管理台静态资源缓存头。

    assets/ 下全是内容哈希文件名（index-XXX.js / Page-XXX.js）⇒ immutable 长缓存：
    换构建即换 hash，浏览器不可能拿到旧 chunk（文件名变了就是新资源）。
    其余（index.html 等入口）no-cache：入口必须每次重取，否则旧入口会引用
    已被新构建删掉的旧 chunk → 404（同 /admin/app 路由注释）。
    """

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if path.startswith("assets/")
            else "no-cache"
        )
        return resp


# SPA 静态资源（/static/admin/assets/*）；index.html 由 /admin/app 路由托管。
# 目录存在才挂载，避免未构建环境下启动报错。
_app_static_dir = os.path.join("static", "admin")
if os.path.isdir(_app_static_dir):
    app.mount("/static/admin", _AdminStaticFiles(directory=_app_static_dir), name="admin_static")


@app.get("/admin/app", response_class=HTMLResponse, dependencies=[Depends(admin_page_auth)])
async def admin_console_spa():
    """Admin Console SPA（React 构建产物 static/admin/）。

    index.html 恒 no-cache（hashed 资源可长缓存，入口不可）：npm run build 换 chunk
    hash 后浏览器必须重取入口，否则旧 index-*.js 去要已删的旧 chunk → 控制台 404。
    """
    p = os.path.join("static", "admin", "index.html")
    if os.path.exists(p):
        return FileResponse(p, headers={"Cache-Control": "no-cache"})
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
    _ident = None
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
        await run_in_threadpool(log_entry, {
            "client_ip": _resolve_client_ip(request),
            "requested_model": "",
            "type": "ip_blocked",
            "action": "block",
            "rule": "key_blacklist",
            "path": path,
        })
        try:
            # 拉黑的 key 仍是登记身份：不带上 key_name/key_id 的话 KEY 统计里会多出
            # 一个 "(unknown)" 桶（2026-10-20 实测：wb 被拉黑后的 4 次 403 全归到它）
            log_request_async(
                _admin_api.get_admin_store(),
                client_ip=_resolve_client_ip(request),
                key_name=getattr(_ident, "name", "") or "",
                key_id=getattr(_ident, "key_id", None) or 0,
                action="block",
                status_code=403,
                blocked_reason="key_blacklist",
            )
        except Exception:
            pass
        # 双写包装：detail 与其余错误统一口径（type+中文 message），error 保 OpenAI
        # SDK 兼容（SDK 认 error 信封）——两边都是同一事实，改文案要同步
        return JSONResponse(
            {"detail": {"type": "key_blocked", "message": "该 key 已被拉黑（key_blacklist），请联系管理员"},
             "error": {"type": "key_blocked", "message": "client key is blacklisted"}},
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
        _p_tok, _c_tok, _h_tok, _w_tok = _normalize_usage(getattr(request.state, "gw_usage", None))
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
            _usage = {"p": _p_tok, "c": _c_tok, "h": _h_tok, "w": _w_tok, "logged": False}
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
                        cached_tokens=_usage.get("h", 0),
                        cache_creation_tokens=_usage.get("w", 0),
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
            if response.status_code == 401:
                # 鉴权层拒掉的请求没有身份、也没有内容：不算受理流量，不落明细
                # （否则 KEY 榜多一个 "(unknown)" 桶，还被记成 allow）。失败鉴权
                # 只在 uvicorn access log 留痕，需要长期留存再单独写审计。
                return response
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
                cached_tokens=_h_tok,
                cache_creation_tokens=_w_tok,
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

