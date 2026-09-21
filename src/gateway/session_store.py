"""Per-session state for confidential-content tracking.

Goal: once a session uploads a confidential file (route_local), all subsequent
chat / messages / responses requests on the same session are also forced to
route_local — even if the new request has no triggering keywords.

Backed by Redis (key: gateway:session:<session_id>); falls back to in-memory
dict if REDIS_URL is not set or Redis is down. 30-minute TTL (configurable).
(P1-1 的 client 身份标记已于 2026-09-18 整体移除：与黑名单建议引擎的
route_local 计数形成误报回路，上线前决定不启用。)
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Optional

try:
    import redis
except ImportError:
    redis = None

DEFAULT_TTL_SECONDS = int(os.getenv("AI_GATEWAY_SESSION_TTL", "1800"))  # 30 min
KEY_PREFIX = "gateway:session:"


def _normalize_session_id(sid: Optional[str]) -> Optional[str]:
    """Validate + normalize session_id from a request header.

    Accept: alphanumeric, dash, underscore, dot; 8-128 chars.
    Returns None for anything else (caller should treat as no session).
    """
    if not sid:
        return None
    sid = sid.strip()
    if len(sid) < 8 or len(sid) > 128:
        return None
    for ch in sid:
        if not (ch.isalnum() or ch in "-_."):
            return None
    return sid


class SessionStore:
    """Singleton store mapping session_id -> {confidential: bool, files: [...], ts: int}."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()
            return cls._instance

    def _init(self):
        self._mem: dict[str, dict] = {}
        self._mem_lock = threading.Lock()
        self._r = None
        self._r_url: Optional[str] = None
        self._r_disabled = False
        self._try_connect()

    def _try_connect(self):
        url = os.getenv("REDIS_URL", "").strip()
        if not url or redis is None:
            return
        try:
            r = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
            r.ping()
            self._r = r
            self._r_url = url
        except Exception:
            self._r = None
            self._r_disabled = True

    def _key(self, sid: str) -> str:
        return f"{KEY_PREFIX}{sid}"

    def mark_confidential(self, sid: str, file_meta: Optional[dict] = None) -> None:
        """Mark a session as having confidential content. TTL restarts on each call."""
        sid = _normalize_session_id(sid)
        if not sid:
            return
        entry = {"confidential": True, "ts": int(time.time())}
        if file_meta:
            entry.setdefault("files", []).append({
                "name": file_meta.get("name") or file_meta.get("filename"),
                "rule": file_meta.get("rule"),
                "action": file_meta.get("action"),
                "ts": entry["ts"],
            })
        if self._r is not None:
            try:
                self._r.set(self._key(sid), json.dumps(entry, ensure_ascii=False), ex=DEFAULT_TTL_SECONDS)
                return
            except Exception:
                self._r = None
                self._r_disabled = True
        # Memory fallback
        with self._mem_lock:
            existing = self._mem.get(sid, {})
            files = list(existing.get("files", [])) + entry.get("files", [])
            self._mem[sid] = {"confidential": True, "ts": entry["ts"], "files": files}

    def is_confidential(self, sid: Optional[str]) -> bool:
        """Return True if this session has been marked confidential (within TTL)."""
        sid = _normalize_session_id(sid)
        if not sid:
            return False
        if self._r is not None:
            try:
                raw = self._r.get(self._key(sid))
                if raw is None:
                    return False
                data = json.loads(raw)
                return bool(data.get("confidential"))
            except Exception:
                self._r = None
                self._r_disabled = True
        # Memory fallback
        with self._mem_lock:
            entry = self._mem.get(sid)
            if not entry:
                return False
            if int(time.time()) - entry.get("ts", 0) > DEFAULT_TTL_SECONDS:
                self._mem.pop(sid, None)
                return False
            return bool(entry.get("confidential"))

    def backend(self) -> str:
        if self._r is not None:
            return f"redis ({self._r_url})"
        if self._r_disabled:
            return "memory (redis disabled)"
        return "memory (no REDIS_URL)"


_store: Optional[SessionStore] = None


def get_session_store() -> SessionStore:
    global _store
    if _store is None:
        _store = SessionStore()
    return _store


def extract_session_id(headers: Any) -> Optional[str]:
    """Pull session id from common header names. Returns None if invalid."""
    if headers is None:
        return None
    # request.headers is case-insensitive
    for name in ("x-session-id", "x-gateway-session-id", "x-conversation-id"):
        v = headers.get(name) if hasattr(headers, "get") else None
        if v:
            return _normalize_session_id(v)
    return None
