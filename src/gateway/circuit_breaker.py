from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Dict, Optional


class CircuitBreaker:
    """Per-provider circuit breaker.

    States:
        CLOSED      - normal, requests pass through
        OPEN        - all requests fail-fast with CircuitOpenError
        HALF_OPEN   - allow 1 probe request to test recovery

    Configurable via env (defaults are sane for upstream LLM APIs):
        AI_GATEWAY_CB_FAILURE_THRESHOLD    (default 5)  consecutive failures to open
        AI_GATEWAY_CB_WINDOW_SECONDS        (default 60) sliding window for failure count
        AI_GATEWAY_CB_RECOVERY_SECONDS      (default 30) OPEN -> HALF_OPEN cooldown
        AI_GATEWAY_CB_SUCCESS_THRESHOLD     (default 2)  successes in HALF_OPEN to close

    Thread-safe. Lock granularity: one lock per provider (dict of locks).
    """

    STATE_CLOSED = "closed"
    STATE_OPEN = "open"
    STATE_HALF_OPEN = "half_open"

    _instance: Optional["CircuitBreaker"] = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()
            return cls._instance

    def _init(self):
        self._lock = threading.RLock()
        # provider_name -> state dict
        self._states: Dict[str, dict] = {}
        # failure timestamps (deque) for sliding window
        self._failures: Dict[str, deque] = {}
        # half-open success counter
        self._half_open_successes: Dict[str, int] = {}
        # snapshot for /admin
        self._snapshot_lock = threading.Lock()

    def _get(self, provider: str) -> dict:
        s = self._states.get(provider)
        if s is None:
            s = {
                "state": self.STATE_CLOSED,
                "opened_at": 0.0,
                "last_failure": 0.0,
                "failure_count_window": 0,
            }
            self._states[provider] = s
        return s

    def _threshold(self) -> int:
        return int(os.getenv("AI_GATEWAY_CB_FAILURE_THRESHOLD", "5"))

    def _window(self) -> int:
        return int(os.getenv("AI_GATEWAY_CB_WINDOW_SECONDS", "60"))

    def _recovery(self) -> int:
        return int(os.getenv("AI_GATEWAY_CB_RECOVERY_SECONDS", "30"))

    def _success_threshold(self) -> int:
        return int(os.getenv("AI_GATEWAY_CB_SUCCESS_THRESHOLD", "2"))

    def state(self, provider: str) -> str:
        with self._lock:
            s = self._get(provider)
            if s["state"] == self.STATE_OPEN:
                # auto transition to half-open after recovery
                if time.time() - s["opened_at"] >= self._recovery():
                    s["state"] = self.STATE_HALF_OPEN
                    s["half_open_successes"] = 0
            return s["state"]

    def allow(self, provider: str) -> bool:
        """Return True if request may proceed. Updates OPEN->HALF_OPEN."""
        with self._lock:
            s = self._get(provider)
            if s["state"] == self.STATE_OPEN:
                if time.time() - s["opened_at"] >= self._recovery():
                    s["state"] = self.STATE_HALF_OPEN
                    s["half_open_successes"] = 0
                else:
                    return False
            return True

    def record_success(self, provider: str) -> None:
        with self._lock:
            s = self._get(provider)
            prev_state = s["state"]
            if s["state"] == self.STATE_HALF_OPEN:
                s["half_open_successes"] = s.get("half_open_successes", 0) + 1
                if s["half_open_successes"] >= self._success_threshold():
                    s["state"] = self.STATE_CLOSED
                    s["opened_at"] = 0.0
                    s["half_open_successes"] = 0
                    self._failures[provider] = deque()
                    self._notify(provider, prev_state, s["state"])
            elif s["state"] == self.STATE_CLOSED:
                # gradual decay
                fq = self._failures.get(provider)
                if fq and time.time() - fq[0] > self._window():
                    self._failures[provider] = deque()

    def record_failure(self, provider: str) -> None:
        with self._lock:
            s = self._get(provider)
            now = time.time()
            s["last_failure"] = now
            prev_state = s["state"]
            if s["state"] == self.STATE_HALF_OPEN:
                # probe failed -> reopen
                s["state"] = self.STATE_OPEN
                s["opened_at"] = now
                s["half_open_successes"] = 0
                self._notify(provider, prev_state, s["state"])
                return
            # CLOSED: append to sliding window
            fq = self._failures.setdefault(provider, deque())
            fq.append(now)
            # trim
            while fq and now - fq[0] > self._window():
                fq.popleft()
            s["failure_count_window"] = len(fq)
            if len(fq) >= self._threshold():
                s["state"] = self.STATE_OPEN
                s["opened_at"] = now
                self._notify(provider, prev_state, s["state"])

    def _notify(self, provider: str, prev: str, new: str) -> None:
        """Hook for metrics side effects (lazy import to avoid circular)."""
        if prev == new:
            return
        try:
            from .metrics import get_metrics
            m = get_metrics()
            m.set_circuit_state(provider, new)
            if new == self.STATE_OPEN:
                m.inc_circuit_trip()
        except Exception:
            pass

    def snapshot(self) -> Dict[str, dict]:
        with self._lock:
            out = {}
            for name, s in self._states.items():
                out[name] = {
                    "state": s["state"],
                    "opened_at": s["opened_at"],
                    "last_failure": s["last_failure"],
                    "failure_count_window": s.get("failure_count_window", 0),
                    "half_open_successes": s.get("half_open_successes", 0),
                }
            return out

    def set_state(self, provider: str, state: str) -> dict:
        """admin 手动置位：open=立即熔断（进恢复倒计时），closed=复位该 provider。"""
        if state not in (self.STATE_OPEN, self.STATE_CLOSED):
            raise ValueError("state must be open|closed")
        with self._lock:
            s = self._get(provider)
            prev = s["state"]
            if state == self.STATE_OPEN:
                s["state"] = self.STATE_OPEN
                s["opened_at"] = time.time()
                s["half_open_successes"] = 0
            else:
                self.reset(provider)
                s = self._get(provider)
            self._notify(provider, prev, s["state"])
            return dict(s)

    def reset(self, provider: Optional[str] = None) -> None:
        with self._lock:
            if provider:
                self._states.pop(provider, None)
                self._failures.pop(provider, None)
                self._half_open_successes.pop(provider, None)
            else:
                self._states.clear()
                self._failures.clear()
                self._half_open_successes.clear()


class CircuitOpenError(Exception):
    """Raised when the circuit for a provider is OPEN and recovery cooldown is not yet elapsed."""
    def __init__(self, provider: str, retry_after: float = 0.0):
        self.provider = provider
        self.retry_after = retry_after
        super().__init__(f"circuit open for {provider} (retry after {retry_after:.1f}s)")


def get_breaker() -> CircuitBreaker:
    return CircuitBreaker()
