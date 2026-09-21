"""对外模型别名（ext-flash / ext-pro）动态路由。

- /v1/models 只暴露别名组；客户端 model=别名 时按组内候选链路由。
- 候选链来自 Admin Console 配置（admin_store.alias_group/alias_member，5s TTL 缓存）。
- 请求内失败立即切下一候选；熔断（per provider::model，复用 CircuitBreaker）打开的候选跳过，
  即"非偶然性错误"记忆；全部候选不可用则抛最后一个错误（上游 4xx 透明透传原则）。
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Dict, List, Optional

from .admin_store import get_admin_store
from .circuit_breaker import get_breaker

# 请求级别名上下文（同 task 内可读；FastAPI 每请求独立 task，互不泄漏）
_alias_ctx: "ContextVar[Optional[dict]]" = ContextVar("alias_ctx", default=None)


def ctx_start(name: str) -> None:
    _alias_ctx.set({"name": name, "failover": []})


def ctx_record_failover(from_ref: str) -> None:
    c = _alias_ctx.get()
    if c is not None:
        c["failover"].append(from_ref)


def ctx_get() -> Optional[dict]:
    return _alias_ctx.get()


def ctx_set_current(provider: str, model: str, local: bool) -> None:
    """记录最终成功的候选（供 main.py 覆盖 meta/audit 展示）。"""
    c = _alias_ctx.get()
    if c is not None:
        c["current"] = {"provider": provider, "model": model, "local": bool(local)}


def find_alias(req_model: str) -> Optional[Dict[str, Any]]:
    """model 名 -> 别名组（大小写不敏感）；非别名返回 None。"""
    key = str(req_model or "").strip().lower()
    if not key:
        return None
    routes = get_admin_store().get_alias_routes()
    return routes.get(key)


def alias_candidates(group: Dict[str, Any], skip: int = 0) -> List[Dict[str, Any]]:
    """组内可用候选（跳过熔断打开的前缀成员）。skip: 已尝试的候选数。"""
    breaker = get_breaker()
    out: List[Dict[str, Any]] = []
    for i, c in enumerate(group.get("candidates") or []):
        if i < skip:
            continue
        cb_key = _cb_key(c["provider"], c["model"])
        if not breaker.allow(cb_key):
            continue
        out.append({"provider": c["provider"], "model": c["model"], "idx": i})
    return out


def _cb_key(provider: str, model: str) -> str:
    return f"{provider}::{model}"


def record_alias_result(provider: str, model: str, ok: bool, local: bool = False) -> None:
    breaker = get_breaker()
    if ok:
        breaker.record_success(_cb_key(provider, model))
        ctx_set_current(provider, model, local)
    else:
        breaker.record_failure(_cb_key(provider, model))
