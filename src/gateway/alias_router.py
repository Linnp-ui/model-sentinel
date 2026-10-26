"""对外模型别名组动态路由（如 Auto-AB）。

- /v1/models 只暴露别名组；客户端 model=别名 时按组内候选链路由。
- 候选链来自 Admin Console 配置（admin_store.alias_group/alias_member，5s TTL 缓存）。
- 请求内失败立即切下一候选；熔断（键 = provider::model，复用 CircuitBreaker，与点名请求
  共用同一份状态）打开的候选跳过，即"非偶然性错误"记忆；全部候选不可用则抛最后一个错误
  （上游 4xx 透明透传原则）。
- 选头（打哪个候选）可被「复杂度路由」改写：别名组开了 intent 时，main._review_text 用
  内网小模型判本轮复杂度，alias_candidates 据此把「够用且最省」的候选提到链首
  （见 intent_router.py）。判定失败/未开启时顺序与历史逐字相同。
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Dict, List, Optional

from .admin_store import get_admin_store
from .circuit_breaker import get_breaker, key_for

# 请求级别名上下文（同 task 内可读；FastAPI 每请求独立 task，互不泄漏）
_alias_ctx: "ContextVar[Optional[dict]]" = ContextVar("alias_ctx", default=None)


def ctx_start(name: str) -> None:
    _alias_ctx.set({"name": name, "failover": []})


def ctx_record_failover(from_ref: str) -> None:
    c = _alias_ctx.get()
    if c is not None:
        c["failover"].append(from_ref)


def ctx_now() -> Optional[dict]:
    """模块级封装（main.py / routing.py 共用，避免重复定义）。"""
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
    """组内可用候选：跳过熔断打开的、跳过本请求已失败的，再按复杂度路由选头。

    排序契约：返回链首 = 本次要打的候选；其余保持优先级顺序做故障切换。
    ``idx`` 仍是组内原始下标（``skip`` 也按原始下标算，语义与旧版一致）；
    复杂度判定结果由 ``main._review_text`` 写进 intent_router 的请求级 ContextVar，
    这里只读 —— 判不出来（None）时顺序与旧版逐字相同。
    """
    breaker = get_breaker()
    _tried = {(t or "").lower() for t in ((_alias_ctx.get() or {}).get("failover") or [])}
    out: List[Dict[str, Any]] = []
    for i, c in enumerate(group.get("candidates") or []):
        if i < skip:
            continue
        if f"{c['provider']}/{c['model']}".lower() in _tried:
            # 本请求内已试过且失败：不再进链（route_chat 的故障切换靠这条推进，
            # 不再用 skip 推算，避免「选头换位后 skip 会误跳健康候选」）
            continue
        cb_key = _cb_key(c["provider"], c["model"])
        if not breaker.allow(cb_key):
            continue
        out.append({"provider": c["provider"], "model": c["model"], "idx": i,
                    "tier": c.get("tier") or 2})
    from .intent_router import ctx_level, select
    return select(out, ctx_level())


def _cb_key(provider: str, model: str) -> str:
    return key_for(provider, model)


def record_alias_ok(provider: str, model: str, local: bool = False) -> None:
    """别名候选成功：记录「最终生效的候选」（供审计/meta 展示）。

    熔断记账统一在 routing 的上游尝试处（键 = provider::model），这里不再重复记账
    ——同一次失败记两遍会让阈值提前一倍触发。
    """
    ctx_set_current(provider, model, local)
