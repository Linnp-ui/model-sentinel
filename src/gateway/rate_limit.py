"""Token-bucket rate limiter for the gateway.

Per (key, scope) sliding window with optional Redis backend.

API:
  get_limiter() -> RateLimiter
  rate_limiter.reset()  # clear all buckets (testing)
  rate_limiter.check(key, scope) -> (ok: bool, retry_after: int, reason: str)
  extract_client_key(authorization, x_api_key, client_ip, identity_key="") -> str

Scopes: "chat" | "files" | "embed" | "messages" (and any other string).
Reasons: "rpm" | "rps" | "files_rpm" | "" (allowed).

Env:
  AI_GATEWAY_RATE_LIMIT_ENABLED = true | false (default true; 0/unset RPM means unlimited)
  AI_GATEWAY_RATE_LIMIT_RPM      = per-minute cap for chat/messages/embed (default 120)
  AI_GATEWAY_RATE_LIMIT_RPS      = per-second burst cap (default 10)
  AI_GATEWAY_RATE_LIMIT_FILES_RPM = per-minute cap for files (default 60)
  REDIS_URL                      = optional; if set, use Redis for cross-process state
"""
from __future__ import annotations

import contextvars
import os
import time
import threading
from typing import Optional, Tuple

# 本请求已扣过模型的 full_model 集合（每请求 task 独立，跨请求不泄漏）
_model_rl_used: contextvars.ContextVar[frozenset] = contextvars.ContextVar(
    "gw_model_rl_used", default=frozenset()
)


def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.getenv(name, str(default)).strip())
        return v if v >= 0 else default
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


def extract_client_key(authorization: Optional[str] = None,
                       x_api_key: Optional[str] = None,
                       client_ip: str = "",
                       identity_key: str = "") -> str:
    """Identity key for rate limiting.

    ``identity_key`` 优先（P0 客户端 key 身份化）：调用方传入已解析身份的绑定键
    （``key-<id>-conf`` / ``fp-<fp>-anon``），使限流桶跟「人」而不跟「凭据前缀」——
    同一人换 key 不再翻倍额度，桶也不再依赖可被截断/伪造的 token 前缀。
    缺省（空串）时回落旧口径 ``tok:<token[:64]>``，既有调用点行为不变。
    """
    if identity_key:
        return identity_key
    if authorization and authorization.startswith("Bearer "):
        t = authorization.split(" ", 1)[1].strip()
        if t and t != "anonymous":
            return f"tok:{t[:64]}"
    if x_api_key:
        t = x_api_key.strip()
        if t and t != "anonymous":
            return f"tok:{t[:64]}"
    return f"ip:{client_ip or 'unknown'}"


class _Bucket:
    """Single token bucket. Tokens refill at refill_per_sec up to capacity."""

    __slots__ = ("tokens", "last_refill", "lock")

    def __init__(self, capacity: float):
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self.lock = threading.Lock()

    def take(self, capacity: float, refill_per_sec: float) -> Tuple[bool, int]:
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(capacity, self.tokens + elapsed * refill_per_sec)
            self.last_refill = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True, 0
            deficit = 1.0 - self.tokens
            retry = max(1, int(deficit / max(refill_per_sec, 1e-9)))
            return False, retry


class RateLimiter:
    """In-memory rate limiter; per (scope, key) token bucket pair."""

    def __init__(self, rpm: int, rps: int, files_rpm: int, enabled: bool):
        self.rpm = rpm
        self.rps = rps
        self.files_rpm = files_rpm
        self.enabled = enabled
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], _Bucket] = {}

    def _scope_limits(self, scope: str) -> Tuple[float, float, str]:
        """Return (capacity, refill_per_sec, reason_label) for a given scope."""
        if scope == "files":
            return float(self.files_rpm), self.files_rpm / 60.0, "files_rpm"
        if self.rpm > 0:
            return float(self.rpm), self.rpm / 60.0, "rpm"
        if self.rps > 0:
            return float(self.rps), float(self.rps), "rps"
        return 0.0, 0.0, ""

    def _get_bucket(self, key: str, scope: str, capacity: float) -> _Bucket:
        bk = (scope, key)
        with self._lock:
            b = self._buckets.get(bk)
            if b is None:
                b = _Bucket(capacity)
                self._buckets[bk] = b
            return b

    def reset(self) -> None:
        """Clear all buckets and re-read env (for test reconfiguration)."""
        with self._lock:
            self._buckets.clear()
        self.rpm = _env_int("AI_GATEWAY_RATE_LIMIT_RPM", self.rpm)
        self.rps = _env_int("AI_GATEWAY_RATE_LIMIT_RPS", self.rps)
        self.files_rpm = _env_int("AI_GATEWAY_RATE_LIMIT_FILES_RPM", self.files_rpm)
        self.enabled = _env_bool("AI_GATEWAY_RATE_LIMIT_ENABLED", self.enabled)

    def check(self, key: str, scope: str = "default") -> Tuple[bool, int, str]:
        if not self.enabled:
            return True, 0, ""
        capacity, refill, reason = self._scope_limits(scope)
        if capacity <= 0 or refill <= 0:
            return True, 0, ""
        b = self._get_bucket(key, scope, capacity)
        ok, retry = b.take(capacity, refill)
        if not ok:
            self._inc_metric(scope, key)
        return ok, retry, ("" if ok else reason)

    def check_model(self, full_model: str, rpm: int) -> Tuple[bool, int]:
        """模型级全局限流（全客户端共享一个桶）。rpm<=0 = 不限。

        full_model 形如 "vllm_local/qwen2.5:7b"，作桶键。
        同一请求内 resolve() 会被预检/实际转发调用多次 —— ContextVar 去重，
        每请求每模型只扣一次（fallback 换候选 = 不同 full_model，各自计）。
        """
        if not self.enabled or rpm <= 0:
            return True, 0
        used = _model_rl_used.get()
        if full_model in used:
            return True, 0
        cap = float(rpm)
        b = self._get_bucket(full_model, "model", cap)
        ok, retry = b.take(cap, cap / 60.0)
        if ok:
            _model_rl_used.set(used | {full_model})
        else:
            try:
                from .metrics import get_metrics
                get_metrics().inc_rate_limited("model", f"model:{full_model}")
            except Exception:
                pass
        return ok, retry

    def _inc_metric(self, scope: str, key: str) -> None:
        try:
            from .metrics import get_metrics
            # P0：桶键新增 key-/fp- 前缀（身份键）。判据改为「非 ip: 即凭据类」，
            # 否则身份桶会被一律记成 ip，限流指标口径静默失真。
            kind = "ip" if (key or "").startswith("ip:") else "token"
            get_metrics().inc_rate_limited(scope, kind)
        except Exception:
            pass


_limiter: Optional[RateLimiter] = None
_limiter_lock = threading.Lock()


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                _limiter = RateLimiter(
                    rpm=_env_int("AI_GATEWAY_RATE_LIMIT_RPM", 120),
                    rps=_env_int("AI_GATEWAY_RATE_LIMIT_RPS", 10),
                    files_rpm=_env_int("AI_GATEWAY_RATE_LIMIT_FILES_RPM", 60),
                    enabled=_env_bool("AI_GATEWAY_RATE_LIMIT_ENABLED", True),
                )
    return _limiter