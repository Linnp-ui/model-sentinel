"""P2: 4-label metric enrichment — additive, doesn't break existing dashboards."""
from __future__ import annotations
import os
import threading
import time
from collections import defaultdict
from contextvars import ContextVar
from typing import Dict, List, Tuple

GATEWAY_VERSION = "0.3.0"

# P2: per-request HTTP status, set by middleware, read by log_entry().
# Default 0 means "not yet known" (caller didn't run in HTTP context, e.g. test).
_CURRENT_STATUS: ContextVar[int] = ContextVar("current_http_status", default=0)


def set_current_status(code: int) -> None:
    """Called by main.py middleware after response. Cheap."""
    try:
        _CURRENT_STATUS.set(int(code))
    except (TypeError, ValueError):
        pass


def get_current_status() -> int:
    return _CURRENT_STATUS.get()


class Metrics:
    _instance = None
    _lock = threading.Lock()
    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()
            return cls._instance

    def _init(self):
        # legacy 5-axis counter — kept for back-compat (existing dashboards)
        self._requests: Dict[Tuple[str, str, str, str, str], int] = defaultdict(int)
        # P2: 7-axis counter — type, action, rule, provider, model, local, status_code
        self._requests_v2: Dict[Tuple[str, str, str, str, str, str, str], int] = defaultdict(int)
        self._circuit_state: Dict[Tuple[str, str], int] = defaultdict(int)
        self._circuit_short_circuit: int = 0
        self._circuit_tripped: int = 0
        self._rate_limited: Dict[Tuple[str, str], int] = defaultdict(int)
        self._ocr_pages_total: int = 0
        self._ocr_errors_total: int = 0
        self._ocr_latency_buckets = [100, 500, 1000, 2000, 5000, 10000]
        self._ocr_latency_counts: List[int] = [0] * (len(self._ocr_latency_buckets) + 1)
        self._ocr_latency_sum: float = 0.0
        self._ocr_latency_total: int = 0
        self._l2_buckets = [50, 100, 200, 300, 500, 1000, 2000]
        self._l2_counts: Dict[str, List[int]] = defaultdict(lambda: [0] * (len(self._l2_buckets) + 1))
        self._l2_sum: Dict[str, float] = defaultdict(float)
        self._l2_total: Dict[str, int] = defaultdict(int)
        self._l2_skipped_total: int = 0
        self._l2_sampled_total: int = 0
        self._l2_degraded_total: int = 0   # P0-2：L2 未产出可信判定（不是「判定为正常」）
        self._l2_cached_total: int = 0     # P1-c：缓存命中的 L2 结果（无真实调用，不入直方图）
        self._l2_scope_calls: Dict[str, int] = defaultdict(int)   # P2：每 scope 实际送审次数
        self._l2_scope_hits: Dict[str, int] = defaultdict(int)    # P2：每 scope 判密命中次数
        # 3.3 建议引擎规则失败（原为裸 except: pass ⇒ 静默丢整组建议）
        self._suggest_rule_errors: Dict[str, int] = defaultdict(int)
        self._override_denied: int = 0
        self._fallback_total: int = 0
        self._stream_errors: Dict[Tuple[str, str, str], int] = defaultdict(int)  # T43: type/provider/status
        # T44/P1：上游凭据来源观测 —— passthrough 归零是切 gateway_only 的判据（D4）
        self._upstream_key_passthrough: int = 0
        self._upstream_key_rejected: int = 0
        self._upstream_key_exhausted: Dict[str, int] = defaultdict(int)  # key = provider
        self._tokens: Dict[str, int] = defaultdict(int)
        self._in_flight: int = 0
        self._gw_internal_sum: float = 0.0
        self._gw_internal_total: int = 0
        self._upstream_sum: float = 0.0
        self._upstream_total: int = 0
        self._start = time.time()
        self._lock2 = threading.Lock()

    def inc_request(self, type_: str, action: str, rule: str, provider: str, local: bool) -> None:
        """Legacy 5-axis counter. New code should call inc_request_v2."""
        with self._lock2:
            key = (type_, action, rule or "", provider or "", "1" if local else "0")
            self._requests[key] += 1

    def inc_request_v2(
        self, type_: str, action: str, rule: str,
        provider: str, model: str, local: bool, status_code: int = 0,
    ) -> None:
        """P2: 7-axis counter — {type, action, rule, provider, model, local, status_code}.

        status_code==0 means "unknown" (e.g. test path or middleware didn't run).
        Cardinality budget: type(~10) * action(~4) * rule(~20) * provider(~10) *
        model(~30) * local(2) * status_code(~20) ≈ 96M, 但实际唯一组合 < 5k.
        """
        with self._lock2:
            key = (
                type_ or "", action or "", rule or "",
                provider or "", model or "",
                "1" if local else "0",
                str(int(status_code or 0)),
            )
            self._requests_v2[key] += 1

    def observe_l2(self, label: str, latency_ms: float) -> None:
        with self._lock2:
            bucket = len(self._l2_buckets)
            for i, b in enumerate(self._l2_buckets):
                if latency_ms <= b:
                    bucket = i
                    break
            self._l2_counts[label][bucket] += 1
            self._l2_sum[label] += latency_ms
            self._l2_total[label] += 1

    def inc_l2_skipped(self) -> None:
        with self._lock2:
            self._l2_skipped_total += 1

    def inc_l2_sampled(self) -> None:
        with self._lock2:
            self._l2_sampled_total += 1

    def inc_l2_degraded(self) -> None:
        with self._lock2:
            self._l2_degraded_total += 1

    def inc_l2_cached(self) -> None:
        with self._lock2:
            self._l2_cached_total += 1

    def inc_l2_scope_call(self, scope: str) -> None:
        """P2：某个 scope 的块真的被送进了小模型。"""
        with self._lock2:
            self._l2_scope_calls[str(scope or "unknown")] += 1

    def inc_l2_scope_hit(self, scope: str) -> None:
        """P2：某个 scope 的块被判密。"""
        with self._lock2:
            self._l2_scope_hits[str(scope or "unknown")] += 1

    def begin_request(self) -> None:
        with self._lock2:
            self._in_flight += 1

    def end_request(self) -> None:
        with self._lock2:
            self._in_flight = max(0, self._in_flight - 1)

    def in_flight(self) -> int:
        return self._in_flight

    def inc_override_denied(self) -> None:
        with self._lock2:
            self._override_denied += 1

    def inc_fallback(self) -> None:
        with self._lock2:
            self._fallback_total += 1

    def observe_gateway_internal(self, latency_ms: float) -> None:
        with self._lock2:
            self._gw_internal_sum += latency_ms
            self._gw_internal_total += 1

    def observe_upstream(self, latency_ms: float) -> None:
        with self._lock2:
            self._upstream_sum += latency_ms
            self._upstream_total += 1
    def inc_stream_error(self, type_: str, provider: str, status_code: int = 0) -> None:
        """T43：流式路径在「首行已落库」之后才失败的次数。

        这类失败**不计** requests_v2（补写行走 log_entry(count=False)，见 main.py），
        所以必须独立计数，否则流式 5xx 在 dashboard 错误率里仍恒 0%。
        维度：type / provider / status_code（上游真实码；网关内部异常记 502）。
        """
        with self._lock2:
            self._stream_errors[(str(type_ or ""), str(provider or ""),
                                 str(int(status_code or 0)))] += 1

    def inc_upstream_key_passthrough(self) -> None:
        """P1/D4：客户端自带上游凭据的请求数（**网关兜底取到的不计**）。"""
        with self._lock2:
            self._upstream_key_passthrough += 1

    def inc_upstream_key_rejected(self) -> None:
        """P1：gateway_only 下客户端仍携带 X-Upstream-Api-Key，被 400 拒绝的次数。"""
        with self._lock2:
            self._upstream_key_rejected += 1

    def inc_upstream_key_exhausted(self, provider: str = "") -> None:
        """P1：无可用网关凭据（未配 / 全冷却）而拒发请求的次数（池空 fail-loud）。"""
        with self._lock2:
            self._upstream_key_exhausted[str(provider or "")] += 1

    def inc_ocr_pages(self, n: int = 1) -> None:
        if n <= 0:
            return
        with self._lock2:
            self._ocr_pages_total += int(n)

    def inc_ocr_errors(self) -> None:
        with self._lock2:
            self._ocr_errors_total += 1

    def observe_ocr_latency(self, latency_ms: float) -> None:
        with self._lock2:
            bucket = len(self._ocr_latency_buckets)
            for i, b in enumerate(self._ocr_latency_buckets):
                if latency_ms <= b:
                    bucket = i
                    break
            self._ocr_latency_counts[bucket] += 1
            self._ocr_latency_sum += latency_ms
            self._ocr_latency_total += 1

    def inc_circuit_short(self) -> None:
        with self._lock2:
            self._circuit_short_circuit += 1

    def inc_circuit_trip(self) -> None:
        with self._lock2:
            self._circuit_tripped += 1

    def inc_rate_limited(self, scope: str, kind: str) -> None:
        with self._lock2:
            key = (scope or "default", kind or "ip")
            self._rate_limited[key] = self._rate_limited.get(key, 0) + 1

    def inc_suggest_rule_error(self, rule: str) -> None:
        """3.3 建议规则计算失败的次数（原为裸 except: pass ⇒ 静默丢整组建议）。"""
        with self._lock2:
            self._suggest_rule_errors[str(rule or "unknown")] += 1

    def set_circuit_state(self, provider: str, state: str) -> None:
        with self._lock2:
            self._circuit_state[(provider, state)] += 1

    @staticmethod
    def _escape(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    def add_tokens(self, kind: str, count: int) -> None:
        if not count or count <= 0:
            return
        with self._lock2:
            self._tokens[kind] += int(count)

    def render(self) -> str:
        with self._lock2:
            lines: List[str] = []
            lines.append("# HELP gateway_info Static info")
            lines.append("# TYPE gateway_info gauge")
            lines.append(f'gateway_info{{version="{GATEWAY_VERSION}"}} 1')
            lines.append("")
            uptime = time.time() - self._start
            lines.append("# HELP gateway_uptime_seconds Process uptime in seconds")
            lines.append("# TYPE gateway_uptime_seconds gauge")
            lines.append(f"gateway_uptime_seconds {uptime:.2f}")
            lines.append("")
            lines.append("# HELP gateway_circuit_tripped_total Total times circuit transitioned to OPEN")
            lines.append("# TYPE gateway_circuit_tripped_total counter")
            lines.append(f"gateway_circuit_tripped_total {self._circuit_tripped}")
            lines.append("")
            lines.append("# HELP gateway_rate_limited_total Total 429 responses from rate limiter")
            lines.append("# TYPE gateway_rate_limited_total counter")
            for (scope, kind), cnt in sorted(self._rate_limited.items()):
                lines.append(f'gateway_rate_limited_total{{scope="{scope}",kind="{kind}"}} {cnt}')
            lines.append("")
            lines.append("# HELP gateway_ocr_pages_total Total PDF pages OCRed")
            lines.append("# TYPE gateway_ocr_pages_total counter")
            lines.append(f"gateway_ocr_pages_total {self._ocr_pages_total}")
            lines.append("")
            lines.append("# HELP gateway_ocr_errors_total Total OCR library/timeout errors")
            lines.append("# TYPE gateway_ocr_errors_total counter")
            lines.append(f"gateway_ocr_errors_total {self._ocr_errors_total}")
            lines.append("")
            lines.append("# HELP gateway_ocr_latency_ms OCR latency in milliseconds")
            lines.append("# TYPE gateway_ocr_latency_ms histogram")
            cumulative = 0
            for i, c in enumerate(self._ocr_latency_counts):
                if i < len(self._ocr_latency_buckets):
                    cumulative += c
                    lines.append(f'gateway_ocr_latency_ms_bucket{{le="{self._ocr_latency_buckets[i]}"}} {cumulative}')
                else:
                    cumulative += c
                    lines.append(f'gateway_ocr_latency_ms_bucket{{le="+Inf"}} {cumulative}')
            if self._ocr_latency_total > 0:
                lines.append(f"gateway_ocr_latency_ms_sum {self._ocr_latency_sum:.2f}")
                lines.append(f"gateway_ocr_latency_ms_count {self._ocr_latency_total}")
            lines.append("")
            lines.append("# HELP gateway_circuit_short_circuit_total Total requests short-circuited by OPEN circuit")
            lines.append("# TYPE gateway_circuit_short_circuit_total counter")
            lines.append(f"gateway_circuit_short_circuit_total {self._circuit_short_circuit}")
            lines.append("")
            lines.append("# HELP gateway_circuit_state_transitions Circuit state transitions by provider")
            lines.append("# TYPE gateway_circuit_state_transitions counter")
            for (provider, state), count in sorted(self._circuit_state.items()):
                lines.append(
                    f'gateway_circuit_state_transitions{{provider="{self._escape(provider)}",state="{self._escape(state)}"}} {count}'
                )
            lines.append("")
            # P2: 4-label metric (replaces single-line older 5-axis one with full 7-axis).
            # Both kept for back-compat; v2 is the new authoritative counter.
            lines.append("# HELP gateway_requests_total (legacy) 5-axis counter; kept for old dashboards")
            lines.append("# TYPE gateway_requests_total counter")
            for (type_, action, rule, provider, local), count in sorted(self._requests.items()):
                lines.append(
                    f'gateway_requests_total{{type="{self._escape(type_)}",action="{self._escape(action)}",'
                    f'rule="{self._escape(rule)}",provider="{self._escape(provider)}",local="{local}"}} {count}'
                )
            lines.append("")
            lines.append("# HELP gateway_requests_v2_total (P2) Requests by type/action/rule/provider/model/local/status_code")
            lines.append("# TYPE gateway_requests_v2_total counter")
            for (type_, action, rule, provider, model, local, status), count in sorted(self._requests_v2.items()):
                lines.append(
                    f'gateway_requests_v2_total{{type="{self._escape(type_)}",action="{self._escape(action)}",'
                    f'rule="{self._escape(rule)}",provider="{self._escape(provider)}",'
                    f'model="{self._escape(model)}",local="{local}",status_code="{status}"}} {count}'
                )
            lines.append("")
            lines.append("# HELP gateway_override_denied_total Total override denials (policy L1/L2 forced local)")
            lines.append("# TYPE gateway_override_denied_total counter")
            lines.append(f"gateway_override_denied_total {self._override_denied}")
            lines.append("")
            lines.append("# HELP gateway_fallback_total Total fallbacks to local (prompt_too_long / first_chunk)")
            lines.append("# TYPE gateway_fallback_total counter")
            lines.append(f"gateway_fallback_total {self._fallback_total}")
            lines.append("")
            lines.append("# HELP gateway_stream_errors_total Stream failures occurring AFTER the audit first row was written (T43); excluded from requests_v2")
            lines.append("# TYPE gateway_stream_errors_total counter")
            for (_se_t, _se_p, _se_s), _se_c in sorted(self._stream_errors.items()):
                lines.append(f'gateway_stream_errors_total{{type="{self._escape(_se_t)}",provider="{self._escape(_se_p)}",status_code="{_se_s}"}} {_se_c}')
            lines.append("")
            lines.append("# HELP gateway_upstream_key_passthrough_total Requests whose upstream credential came from the CLIENT (P1; must be 0 for 7d before switching to gateway_only)")
            lines.append("# TYPE gateway_upstream_key_passthrough_total counter")
            lines.append(f"gateway_upstream_key_passthrough_total {self._upstream_key_passthrough}")
            lines.append("")
            lines.append("# HELP gateway_upstream_key_rejected_total X-Upstream-Api-Key rejected because upstream credentials are gateway-held (P1)")
            lines.append("# TYPE gateway_upstream_key_rejected_total counter")
            lines.append(f"gateway_upstream_key_rejected_total {self._upstream_key_rejected}")
            lines.append("")
            lines.append("# HELP gateway_upstream_key_exhausted_total Requests denied because no gateway-held upstream credential was available (P1)")
            lines.append("# TYPE gateway_upstream_key_exhausted_total counter")
            for _pk_p, _pk_c in sorted(self._upstream_key_exhausted.items()):
                lines.append(f'gateway_upstream_key_exhausted_total{{provider="{self._escape(_pk_p)}"}} {_pk_c}')
            lines.append("")
            lines.append("# HELP gateway_tokens_total Token counts (sum of upstream reported)")
            lines.append("# TYPE gateway_tokens_total counter")
            for kind, count in sorted(self._tokens.items()):
                lines.append(f'gateway_tokens_total{{kind="{self._escape(kind)}"}} {count}')
            lines.append("")
            lines.append("")
            lines.append("# HELP gateway_l2_latency_ms L2 classification latency in milliseconds")
            lines.append("# TYPE gateway_l2_latency_ms histogram")
            for label, counts in sorted(self._l2_counts.items()):
                cumulative = 0
                for i, c in enumerate(counts):
                    if i < len(self._l2_buckets):
                        cumulative += c
                        lines.append(
                            f'gateway_l2_latency_ms_bucket{{label="{self._escape(label)}",'
                            f'le="{self._l2_buckets[i]}"}} {cumulative}'
                        )
                    else:
                        cumulative += c
                        lines.append(
                            f'gateway_l2_latency_ms_bucket{{label="{self._escape(label)}",le="+Inf"}} {cumulative}'
                        )
            for label in sorted(self._l2_counts.keys()):
                if self._l2_total[label] > 0:
                    lines.append(f'gateway_l2_latency_ms_sum{{label="{self._escape(label)}"}} {self._l2_sum[label]:.2f}')
                    lines.append(f'gateway_l2_latency_ms_count{{label="{self._escape(label)}"}} {self._l2_total[label]}')
            lines.append("# HELP gateway_l2_skipped_total L2 calls skipped via whitelist exemption")
            lines.append("# TYPE gateway_l2_skipped_total counter")
            lines.append(f"gateway_l2_skipped_total {self._l2_skipped_total}")
            lines.append("# HELP gateway_l2_sampled_total L2 calls forced by whitelist sampling")
            lines.append("# TYPE gateway_l2_sampled_total counter")
            lines.append(f"gateway_l2_sampled_total {self._l2_sampled_total}")
            lines.append("# HELP gateway_l2_degraded_total L2 degraded exits (chunk-level + aggregate), NOT a request count; per-request rate = requests_v2 where rule=l2_unavailable")
            lines.append("# TYPE gateway_l2_degraded_total counter")
            lines.append(f"gateway_l2_degraded_total {self._l2_degraded_total}")
            lines.append("# HELP gateway_l2_cached_total L2 reviews served entirely from the in-process result cache (all blocks hit; no upstream call)")
            lines.append("# TYPE gateway_l2_cached_total counter")
            lines.append(f"gateway_l2_cached_total {self._l2_cached_total}")
            lines.append("# HELP gateway_l2_scope_calls_total L2 scope blocks submitted for review (P2)")
            lines.append("# TYPE gateway_l2_scope_calls_total counter")
            for _s, _c in sorted(self._l2_scope_calls.items()):
                lines.append(f'gateway_l2_scope_calls_total{{scope="{self._escape(_s)}"}} {_c}')
            lines.append("# HELP gateway_l2_scope_hits_total L2 scope blocks judged CONFIDENTIAL (P2)")
            lines.append("# TYPE gateway_l2_scope_hits_total counter")
            for _s, _c in sorted(self._l2_scope_hits.items()):
                lines.append(f'gateway_l2_scope_hits_total{{scope="{self._escape(_s)}"}} {_c}')
            lines.append("# HELP gateway_internal_ms Gateway-internal latency (L1+L2 processing) in milliseconds")
            lines.append("# TYPE gateway_internal_ms summary")
            if self._gw_internal_total > 0:
                lines.append(f"gateway_internal_ms_sum {self._gw_internal_sum:.2f}")
                lines.append(f"gateway_internal_ms_count {self._gw_internal_total}")
            lines.append("# HELP gateway_upstream_ms Upstream model call latency in milliseconds")
            lines.append("# TYPE gateway_upstream_ms summary")
            if self._upstream_total > 0:
                lines.append(f"gateway_upstream_ms_sum {self._upstream_sum:.2f}")
                lines.append(f"gateway_upstream_ms_count {self._upstream_total}")
                lines.append("# HELP gateway_suggest_rule_errors_total Suggestion-engine rule failures (3.3), by rule name")
            lines.append("# TYPE gateway_suggest_rule_errors_total counter")
            for _r, _c in sorted(self._suggest_rule_errors.items()):
                lines.append(f'gateway_suggest_rule_errors_total{{rule="{self._escape(_r)}"}} {_c}')
            return "\n".join(lines) + "\n"

    def export_panel(self) -> dict:
        """JSON-friendly aggregation of all metrics for the /admin/metrics/panel dashboard."""
        with self._lock2:
            total = 0
            by_action: Dict[str, int] = defaultdict(int)
            by_type: Dict[str, int] = defaultdict(int)
            by_status: Dict[str, int] = defaultdict(int)
            by_rule: Dict[str, int] = defaultdict(int)
            by_provider: Dict[str, int] = defaultdict(int)
            by_model: Dict[str, int] = defaultdict(int)
            for (type_, action, rule, provider, model, _local, status), cnt in self._requests_v2.items():
                total += cnt
                by_action[action or "unknown"] += cnt
                by_type[type_ or "unknown"] += cnt
                by_status[str(status)] += cnt
                if rule:
                    by_rule[rule] += cnt
                if provider:
                    by_provider[provider] += cnt
                if model:
                    by_model[model] += cnt
            by_status_class: Dict[str, int] = defaultdict(int)
            for s, cnt in by_status.items():
                if not s.isdigit() or s == "0":
                    by_status_class["unknown"] += cnt
                else:
                    c = int(s) // 100
                    by_status_class[f"{c}xx" if 1 <= c <= 5 else "other"] += cnt
            l2_edges = self._l2_buckets
            l2_buckets = [0] * (len(l2_edges) + 1)
            l2_sum = 0.0
            l2_count = 0
            for _label, counts in self._l2_counts.items():
                for i, c in enumerate(counts):
                    l2_buckets[i] += c
                l2_sum += self._l2_sum[_label]
                l2_count += self._l2_total[_label]
            ocr_sum = self._ocr_latency_sum
            ocr_count = self._ocr_latency_total
            circuit_states: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
            for (p, s), c in self._circuit_state.items():
                circuit_states[p][s] += c
            return {
                "in_flight": self._in_flight,
                "version": GATEWAY_VERSION,
                "uptime_s": round(time.time() - self._start, 1),
                "total_requests": total,
                "override_denied": self._override_denied,
                "fallback": self._fallback_total,
                "stream_errors": sum(self._stream_errors.values()),
                "stream_errors_by_type": {"%s|%s|%s" % _k: _v for _k, _v in self._stream_errors.items()},
                "upstream_key_passthrough": self._upstream_key_passthrough,
                "upstream_key_rejected": self._upstream_key_rejected,
                "upstream_key_exhausted": sum(self._upstream_key_exhausted.values()),
                "rate_limited_total": sum(self._rate_limited.values()),
                "ocr_pages": self._ocr_pages_total,
                "ocr_errors": self._ocr_errors_total,
                "tokens_total": sum(self._tokens.values()),
                "by_action": dict(by_action),
                "by_type": dict(by_type),
                "by_status_class": dict(by_status_class),
                "top_rules": sorted(by_rule.items(), key=lambda x: -x[1])[:10],
                "top_providers": sorted(by_provider.items(), key=lambda x: -x[1])[:10],
                "top_models": sorted(by_model.items(), key=lambda x: -x[1])[:10],
                "l2_bucket_edges": list(l2_edges),
                "l2_buckets": l2_buckets,
                "l2_avg_ms": round(l2_sum / l2_count, 1) if l2_count else 0,
                "l2_count": l2_count,
                "l2_skipped_total": self._l2_skipped_total,
                "l2_sampled_total": self._l2_sampled_total,
                "l2_degraded_total": self._l2_degraded_total,
                "gw_internal_avg_ms": round(self._gw_internal_sum / self._gw_internal_total, 1) if self._gw_internal_total else 0,
                "gw_internal_total": self._gw_internal_total,
                "upstream_avg_ms": round(self._upstream_sum / self._upstream_total, 1) if self._upstream_total else 0,
                "upstream_total": self._upstream_total,
                "l2_scope_calls": dict(self._l2_scope_calls),
                "l2_scope_hits": dict(self._l2_scope_hits),
                "ocr_bucket_edges": list(self._ocr_latency_buckets),
                "ocr_buckets": list(self._ocr_latency_counts),
                "ocr_avg_ms": round(ocr_sum / ocr_count, 1) if ocr_count else 0,
                "ocr_count": ocr_count,
                "circuit": {
                    "tripped": self._circuit_tripped,
                    "short_circuit": self._circuit_short_circuit,
                    "state_transitions": {p: dict(states) for p, states in circuit_states.items()},
                },
                "tokens": dict(self._tokens),
            }


_m: Metrics | None = None


def get_metrics() -> Metrics:
    global _m
    if _m is None:
        _m = Metrics()
    return _m

