"""M3 统计服务：异步明细落库、3.3 策略建议规则引擎、明细过期清理。

需求映射：
- 3.1 模型调用量·时间周期-人员(IP)：stats_overview/stats_group/stats_daily
- 3.2 拦截量统计：stats_group(dim='action') + blocked 维度
- 3.3 调节策略建议：build_suggestions() 规则引擎，支持一键应用（加黑名单）
- 明细落库不阻塞主链路：单线程 ThreadPoolExecutor fire-and-forget；
  store 故障静默吞掉（绝不影响 /v1/* 转发）。
- 保留策略：默认 90 天（AI_GATEWAY_LOG_RETENTION_DAYS 可调），机会式清理
  （每次落库时检查，至多每 6 小时执行一次，无后台线程）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .stat_scope import is_abnormal

_log = logging.getLogger("gateway.stats")

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gw-stats")
_purge_lock = threading.Lock()
_last_purge_ts: float = 0.0
_PURGE_INTERVAL_S = 6 * 3600

# 3.3 建议阈值：运行时读 env（便于调参与测试），默认值见 _thresholds()
def _env_num(new: str, old: str, default, cast):
    """新 env 名优先，旧名兜底；取值非法时**留痕**并回落默认。

    触发口径由「拦截」改为「异常处置 = 拦截 ∪ 本地路由」（B 案，2026-09-16）后
    语义变了。继续叫 `*_BLOCK*` 会让变量名说谎，但旧名出现在 AGENTS.d/08 与 3 个
    测试文件里，硬改名会让它们静默失效 ⇒ 保留兜底读取。
    """
    raw = os.getenv(new)
    if raw is None or raw == "":
        raw = os.getenv(old)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        _log.warning("建议阈值 %s（旧名 %s）取值非法：%r ⇒ 回落默认 %r",
                     new, old, raw, default)
        return default


def _thresholds() -> dict:
    return {
        # 规则1 拉黑：异常处置次数（原 AI_GATEWAY_SUGGEST_BLOCK_THRESHOLD）
        "violation": _env_num("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD",
                              "AI_GATEWAY_SUGGEST_BLOCK_THRESHOLD", 20, int),
        "volume": int(os.getenv("AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD", "1000")),
        "error_rate": float(os.getenv("AI_GATEWAY_SUGGEST_ERROR_RATE", "0.10")),
        # 2026-09-16 修复②：原为硬编码 20（其余阈值都可配，唯独它不能）
        "error_min_calls": int(os.getenv("AI_GATEWAY_SUGGEST_ERROR_MIN_CALLS", "20")),
        "kt_black_calls": int(os.getenv("AI_GATEWAY_KEYTIER_MIN_CALLS_BLACK", "20")),
        "kt_black_min_violations": _env_num("AI_GATEWAY_KEYTIER_MIN_VIOLATIONS",
                                            "AI_GATEWAY_KEYTIER_MIN_BLOCKS", 5, int),
        "kt_violation_rate": _env_num("AI_GATEWAY_KEYTIER_VIOLATION_RATE",
                                      "AI_GATEWAY_KEYTIER_BLOCK_RATE", 0.30, float),
        "kt_white_calls": int(os.getenv("AI_GATEWAY_KEYTIER_MIN_CALLS_WHITE", "100")),
        "kt_revoke_violations": _env_num("AI_GATEWAY_KEYTIER_REVOKE_VIOLATIONS",
                                         "AI_GATEWAY_KEYTIER_REVOKE_BLOCKS", 2, int),
        "kt_window_hours": float(os.getenv("AI_GATEWAY_KEYTIER_WINDOW_HOURS", "720")),
    }


def _window(since_hours: float) -> tuple:
    until = time.strftime("%Y-%m-%d %H:%M:%S")
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - since_hours * 3600))
    return since, until


def log_request_async(
    store,
    *,
    client_ip: str,
    key_name: str = "",
    key_id: int = 0,
    model: str = "",
    provider: str = "",
    action: str = "allow",
    status_code: int = 0,
    blocked_reason: str = "",
    duration_ms: int = 0,
    gateway_internal_ms: int = 0,
    upstream_ms: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """fire-and-forget 落库：任何异常只吞不抛，绝不影响主链路。"""
    row = {
        "client_ip": client_ip or "",
        "key_name": key_name or "",
        "key_id": int(key_id) if key_id else None,
        "model": model or "",
        "provider": provider or "",
        "action": action or "allow",
        "status_code": int(status_code or 0),
        "blocked_reason": blocked_reason or "",
        "duration_ms": int(duration_ms or 0),
        "gateway_internal_ms": int(gateway_internal_ms or 0),
        "upstream_ms": int(upstream_ms or 0),
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
    }

    def _do():
        try:
            store.insert_request_log(row)
            _maybe_purge(store)
        except Exception:
            pass

    try:
        _executor.submit(_do)
    except Exception:
        pass


def _maybe_purge(store) -> None:
    """至多每 6 小时清理一次超保留期的明细。"""
    global _last_purge_ts
    now = time.time()
    if now - _last_purge_ts < _PURGE_INTERVAL_S:
        return
    with _purge_lock:
        if time.time() - _last_purge_ts < _PURGE_INTERVAL_S:
            return
        _last_purge_ts = time.time()
    try:
        days = float(os.getenv("AI_GATEWAY_LOG_RETENTION_DAYS", "90"))
        cutoff = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400)
        )
        store.purge_request_logs(cutoff)
    except Exception:
        pass


def peek_model_from_body(body: bytes) -> str:
    """从请求体提 model（JSON 且 <1MB 才尝试，失败返回空）。"""
    try:
        if not body or len(body) > 1_000_000:
            return ""
        data = json.loads(body)
        if isinstance(data, dict):
            return str(data.get("model") or "")
    except Exception:
        pass
    return ""


def peek_provider_from_response(resp) -> str:
    """从**响应**里取实际落点的 provider 名（`request_log.provider` 的唯一来源）。

    handler 侧有两条既有通道，本函数按优先级取：
      1. 响应头 ``X-Gateway-Provider`` —— chat/messages/responses **流式** + egress 直通
         （流式响应没有可读 body，只能看头）
      2. 响应体 ``gateway.provider`` —— **非流式**各端点；该值已含别名覆盖
         （handler 里的 ``_alias_ctx``），是「最终落点」而非「本该去哪」

    取不到返回 ""（如 403 黑名单拦截，本来就没有上游）。
    **不要**在这里调 ``routing.resolve()`` 反推 —— 它不知道别名候选与
    first_chunk_fallback，会报出错误归属，比空值更有害。

    为什么中间件不读 handler 的 prov：``@app.middleware("http")`` 底层是
    BaseHTTPMiddleware，``call_next`` 把下游跑在独立 task 里，ContextVar 只向下继承、
    下游的 set 传不回中间件（同族问题见 AGENTS.d/09 坑 U）。
    """
    try:
        h = (resp.headers.get("x-gateway-provider") or "").strip()
        if h:
            from urllib.parse import unquote
            return unquote(h)
    except Exception:
        pass
    try:
        body = getattr(resp, "body", b"") or b""
        if body:
            return provider_from_json_bytes(body)
    except Exception:
        pass
    return ""


def provider_from_json_bytes(body: bytes) -> str:
    """从响应体 JSON 取**实际落点**的 provider（缺失 / 非法 / 超大都返回 ""）。

    两个来源，按优先级：
      1. ``gateway.provider`` —— 正常响应（非流式各端点，已含别名覆盖）
      2. ``detail.provider`` / ``error.provider`` —— **错误响应**。上游 4xx/5xx 时
         handler 抛 HTTPException(detail={"type":"routing_error","provider":...})。
         「报错时到底出没出境」正是最需要归因的时刻，不能丢。

    独立成函数的原因：中间件在 BaseHTTPMiddleware 下拿不到 JSONResponse 的
    ``.body``（下游被包成 `_StreamingResponse`），只能自己消费 `body_iterator`
    再把同一份字节喂进来。
    """
    try:
        if not body or len(body) > 1_000_000:
            return ""
        data = json.loads(body)
        if not isinstance(data, dict):
            return ""
        gw = data.get("gateway")
        if isinstance(gw, dict) and gw.get("provider"):
            return str(gw["provider"])
        for key in ("detail", "error"):
            node = data.get(key)
            if isinstance(node, dict) and node.get("provider"):
                return str(node["provider"])
    except Exception:
        pass
    return ""


def _rule_failed(rule: str) -> None:
    """建议规则计算失败必须留痕（日志 + 指标）。

    历史：这里原是 4 个裸 `except Exception: pass` ⇒ store 查询失败时整组建议凭空
    消失，UI 显示「当前无建议：各指标均在阈值内」，与真实原因（查询挂了）无法区分
    （2026-09-16 修复③）。对齐 P0/P1 立下的「静默降级必须留痕（日志 + 指标）」。
    """
    _log.warning("build_suggestions 规则 %s 计算失败，该组建议本次缺失", rule,
                 exc_info=True)
    try:
        from .metrics import get_metrics
        get_metrics().inc_suggest_rule_error(rule)
    except Exception:
        pass


def suggestion_meta(window_hours: float = 24.0) -> dict:
    """建议口径的元信息（供 UI 明示双窗口与触发口径，避免标题误导）。

    规则 0（KEY 分级）用独立的 kt_window_hours（默认 720h=30 天，慢变量：分级决策
    不该被单日尖刺触发）；规则 1/2/3 用调用方传入的 window_hours（快变量）。
    两者不同 ⇒ 同一张卡片上并存两个时间窗，UI 必须写清楚而不是笼统标「最近 24h」。
    """
    th = _thresholds()
    kt_h = float(th["kt_window_hours"])
    w_h = float(window_hours)
    return {
        "trigger": "abnormal = block | route_local (B 案)",
        "window_hours": w_h,
        "key_tier_window_hours": kt_h,
        "has_dual_window": abs(kt_h - w_h) > 1e-6,
        "thresholds": {
            "violation": th["violation"],
            "volume": th["volume"],
            "error_rate": th["error_rate"],
            "error_min_calls": th["error_min_calls"],
            "kt_black_calls": th["kt_black_calls"],
            "kt_black_min_violations": th["kt_black_min_violations"],
            "kt_violation_rate": th["kt_violation_rate"],
            "kt_white_calls": th["kt_white_calls"],
            "kt_revoke_violations": th["kt_revoke_violations"],
        },
    }


def build_suggestions(store, window_hours: float = 24.0) -> list:
    """3.3 规则引擎：基于统计触发建议。返回建议列表（可带一键应用动作）。"""
    since, until = _window(window_hours)
    th = _thresholds()
    out = []

    try:
        by_ip = {g["label"]: g for g in store.stats_group(since, until, "client_ip")}
    except Exception:
        _rule_failed("volume_watch")
        by_ip = {}

    # 已在黑名单的 KEY 不重复建议
    try:
        blacklisted = {
            r["key_value"] for r in store.list_key_rules() if r.get("kind") == "black"
        }
    except Exception:
        _rule_failed("key_tier_blacklist_lookup")
        blacklisted = set()

    # key_name -> key 原文（用于一键拉黑；未登记 key 的拦截无法解析原文，跳过）
    try:
        name2key = {k["name"]: k["key_plain"] for k in store.list_api_keys_raw()}
    except Exception:
        _rule_failed("key_tier_name_lookup")
        name2key = {}

    # 规则 0：KEY 分级（30 天窗口口径；阈值 env 可调）
    rate_black_names: set = set()
    try:
        k_since, k_until = _window(th["kt_window_hours"])
        tiers = store.stats_key_tier(k_since, k_until)
        cur_rules = store.list_key_rules()
        white_kv2rule = {r["key_value"]: r["id"] for r in cur_rules if r.get("kind") == "white"}
        win_d = int(th["kt_window_hours"] / 24)
        for g in tiers:
            kn, calls = g["key_name"], g["calls"]
            blocks = g.get("blocks", 0)
            loc_n = g.get("locals", 0)
            # 兼容没有新字段的桩/旧后端：缺 violations 时按 blocks+locals 退算
            viol = g.get("violations", blocks + loc_n)
            kp = name2key.get(kn, "")
            if not kp or kp in blacklisted:
                continue
            rate = viol / calls if calls else 0.0
            mix = f"拦截 {blocks} + 本地路由 {loc_n}"
            if (calls >= th["kt_black_calls"] and viol >= th["kt_black_min_violations"]
                    and rate >= th["kt_violation_rate"]):
                rate_black_names.add(kn)
                out.append({
                    "id": f"ktblack:{kn}", "type": "key_rate_black", "severity": "high",
                    "key_name": kn,
                    "metric": f"{viol}/{calls} 异常处置（{rate:.0%}）/ {win_d} 天",
                    "message": (f"KEY（{kn}）近 {win_d} 天异常处置率 {rate:.0%}"
                                f"（{viol}/{calls}，{mix}），建议拉黑"),
                    "apply": {"kind": "black", "key_name": kn},
                })
            elif kp in white_kv2rule:
                if viol >= th["kt_revoke_violations"]:
                    out.append({
                        "id": f"ktrevoke:{kn}", "type": "key_white_revoke", "severity": "warn",
                        "key_name": kn,
                        "metric": f"白名单期异常处置 {viol} 次 / {win_d} 天",
                        "message": (f"白名单 KEY（{kn}）出现 {viol} 次异常处置（{mix}），"
                                    f"建议撤销白名单"),
                        "apply": {"kind": "unwhite", "rule_id": white_kv2rule[kp]},
                    })
            elif calls >= th["kt_white_calls"] and viol == 0:
                out.append({
                    "id": f"ktwhite:{kn}", "type": "key_whitelist", "severity": "info",
                    "key_name": kn,
                    "metric": f"{calls} 次调用零异常处置 / {win_d} 天",
                    "message": (f"KEY（{kn}）近 {win_d} 天 {calls} 次调用无一次异常处置"
                                f"（既无拦截也无本地路由），建议加白（跳 L2，抽样 5%）"),
                    "apply": {"kind": "white", "key_name": kn},
                })
    except Exception:
        _rule_failed("key_tier")

    # 规则 1：异常处置量超阈 -> 建议拉黑对应 KEY（一键应用）
    try:
        abn_rows = [r for r in store._log_rows(since, until)
                    if is_abnormal(r.get("action"), r.get("status_code"))]
        abn_by_name: dict = {}
        for r in abn_rows:
            kn = r.get("key_name", "")
            if kn:
                abn_by_name[kn] = abn_by_name.get(kn, 0) + 1
        for kn, cnt in sorted(abn_by_name.items(), key=lambda kv2: -kv2[1]):
            if kn in rate_black_names:
                continue
            kp = name2key.get(kn, "")
            if cnt >= th["violation"] and kp and kp not in blacklisted:
                out.append({
                    "id": f"blackkey:{kn}",
                    "type": "key_blacklist",
                    "severity": "high",
                    "key_name": kn,
                    "metric": f"{cnt} 次异常处置 / 最近 {int(window_hours)}h",
                    "message": (f"KEY（{kn}）在最近 {int(window_hours)} 小时内出现 {cnt} 次"
                                f"异常处置（拦截或本地路由），建议加入黑名单"),
                    "apply": {"kind": "black", "key_name": kn},
                })
    except Exception:
        _rule_failed("block_count")

    # 规则 2：单 IP 调用量超阈 → 关注（不自动拦截）
    for ip, g in sorted(by_ip.items(), key=lambda kv: -kv[1]["calls"]):
        if g["calls"] >= th["volume"]:
            out.append({
                "id": f"volume:{ip}",
                "type": "volume_watch",
                "severity": "info",
                "ip": ip,
                "metric": f"{g['calls']} 次调用 / 最近 {int(window_hours)}h",
                "message": f"IP {ip} 调用量较高（{g['calls']} 次），建议关注或配置限流",
                "apply": None,
            })

    # 规则 3：模型错误率超阈 → 排查 provider
    try:
        by_model = store.stats_group(since, until, "model")
        for g in by_model:
            if g["calls"] >= th["error_min_calls"] and g["errors"] / g["calls"] >= th["error_rate"]:
                out.append({
                    "id": f"errorrate:{g['label']}",
                    "type": "model_error_rate",
                    "severity": "warn",
                    "model": g["label"],
                    "metric": f"错误率 {g['errors']}/{g['calls']}",
                    "message": f"模型 {g['label']} 最近错误率 {g['errors']}/{g['calls']}（≥{th['error_rate']:.0%}），建议排查该 provider",
                    "apply": None,
                })
    except Exception:
        _rule_failed("model_error_rate")

    return out
