"""统计口径的唯一实现：拦截 / 本地路由 / 时间窗时基。

为什么单独一个模块：同一份语义曾散落在 5 处，判定式互不相同，导致同一页上
「拦截量」卡片与「趋势红柱」必然对不上（2026-09-14 生产实测）：

    admin_store._row_blocked      block* or route_local or 403
    admin_store.stats_key_tier    block* or 403
    main /admin/audit/entries     action == "block"
    main _analytics_aggregate     action == "block"
    main 趋势图 blk[]             action == "block"

口径只允许在这里改，其它入口一律调用本模块。

  拦截臂 block：action 以 block 开头（L1 规则 / L2 判定 / KEY 黑名单）
  拦截臂 403  ：status_code == 403（黑名单拒绝、egress 拒绝等「被拒」语义）
  本地路由    ：action == "route_local"（内容不出境的合规降级），单独计数，
                不计入拦截 —— 它不是拦截，是降级。
  异常处置    ：拦截 ∪ 本地路由（is_abnormal()），**只用于建议引擎的阈值触发**，
                不是展示口径 —— 界面上的拦截量必须仍走 is_blocked()。
                为什么建议引擎要取并集：见 is_abnormal() docstring（B 案 2026-09-16）。
"""
from __future__ import annotations

from datetime import datetime

__all__ = ["is_abnormal", "is_blocked", "is_local_route", "resolve_action",
           "to_local_naive"]


def _norm_action(action) -> str:
    return str(action or "").strip().lower()


def is_blocked(action, status_code=None) -> bool:
    """网关是否「拦截」了这个请求：block* 或 403。"""
    if _norm_action(action).startswith("block"):
        return True
    try:
        return int(status_code or 0) == 403
    except (TypeError, ValueError):
        return False


def is_local_route(action) -> bool:
    """是否走了合规本地路由（内容不出境降级）。"""
    return _norm_action(action) == "route_local"


def is_abnormal(action, status_code=None) -> bool:
    """是否「异常处置」= 拦截 ∪ 本地路由（B 案，2026-09-16 用户拍板）。

    只用于**建议引擎的阈值触发**，不是展示口径：界面上的「拦截量」必须仍走
    is_blocked()。两者混用会让 route_local 重新变成拦截 —— 那正是 2026-09-14
    那次「同一页上两个卡片必然对不上」的漂移源头。

    为什么建议引擎要取并集：生产 `on_block=fallback_local`（routing.yaml 默认）
    ⇒ L1/L2 判定命中后请求由本地模型承接（内容不出境），落库 action 是
    route_local 而不是 block（2026-09-16 实测 168h：block 1166 / route_local 214）。
    只看 is_blocked() 会整类漏掉「某 KEY 频繁触碰敏感内容」这个信号 ——
    而那正是分级拉黑建议要抓的东西。
    """
    return is_blocked(action, status_code) or is_local_route(action)


def resolve_action(reason, status_code) -> str:
    """由「判定原因 + 最终状态码」推出统计用的 action（allow / route_local / block）。

    写入侧的唯一实现：中间件落 request_log 时用它兜底，避免再出现
    「命中规则 = 被拦截」这种把降级算成拦截的口径漂移。

      403                -> block        （真被拒：L1 block 未降级 / 黑名单 / egress）
      有 reason 且非 403 -> route_local  （判定改变了处置，由本地模型承接）
      其余               -> allow

    注意：本函数只看「有没有原因」，不解析原因内容 —— 历史脏数据里
    `l2:无敏感信息` 这类值同样会被判成 route_local，那批数据由只读影响面
    报告处理（见 plans/2026-09-15-p1-stats-scope.md Task 10），不在这里猜。
    """
    try:
        if int(status_code or 0) == 403:
            return "block"
    except (TypeError, ValueError):
        pass
    return "route_local" if str(reason or "").strip() else "allow"

def to_local_naive(dt):
    """aware datetime -> 本地墙钟（去 tzinfo）；朴素时间原样返回。

    审计明细的 time 是 time.strftime("%Y-%m-%d %H:%M:%S")（本地墙钟），所以任何
    ISO 入参都必须先 astimezone() 落到本地再比。原实现直接 replace(tzinfo=None)，
    把 UTC 串当本地墙钟用，窗口整体偏一个时区（趋势桶跟着左移）。
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone().replace(tzinfo=None)
    return dt
