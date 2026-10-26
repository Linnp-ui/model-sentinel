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

import hashlib
import json
import os
import threading
import time
from typing import Any, Optional

from urllib.parse import urlsplit

try:
    import redis
except ImportError:
    redis = None

DEFAULT_TTL_SECONDS = int(os.getenv("AI_GATEWAY_SESSION_TTL", "1800"))  # 30 min
KEY_PREFIX = "gateway:session:"
TAINT_PREFIX = "gateway:taint:"
STICKY_PREFIX = "gateway:stickytier:"
# 粘性档位键的 Redis TTL：调用方窗口（env）+ 余量；过期即惰性失效，不依赖它判窗口
_STICKY_KEY_TTL = 90 * 60


def now_ok(ts: int, window_s: int) -> bool:
    return ts > 0 and time.time() - ts <= window_s


def _taint_key(key_id: str, filename: str) -> Optional[str]:
    """taint 键：key 指纹 + 小写文件名。任一非法即 None（调用方跳过，不写全局）。"""
    if not key_id or not filename:
        return None
    fn = filename.strip().lower()
    if not fn or len(fn) > 256:
        return None
    kid = hashlib.sha256(key_id.encode("utf-8")).hexdigest()[:16]
    return f"{TAINT_PREFIX}{kid}:{fn}"


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

    def mark_tainted_file(self, key_id: str, filename: str, rule: str = "") -> None:
        """记被污染文件名（同 key 下曾判机密）。key/文件名非法即静默跳过；失败不影响主链路。

        按 key 存集合（`gateway:taintset:<kid>`）：mention 提取常带中文前缀
        （"帮我看看X.xlsx"整体命中），写是干净上传名、读是污染 mention，
        精确匹配永远对不上，故读侧做双向后缀匹配。
        """
        tk = _taint_key(key_id, filename)
        if not tk:
            return
        kid = tk.split(":")[2]
        now = int(time.time())
        if self._r is not None:
            try:
                raw = self._r.get(f"{TAINT_PREFIX}set:{kid}")
                data = json.loads(raw) if raw else {}
            except Exception:
                self._r = None
                self._r_disabled = True
                data = None
            if data is not None:
                try:
                    fn = tk.rsplit(":", 1)[1]
                    data = {k: v for k, v in data.items()
                            if now - v.get("ts", 0) <= DEFAULT_TTL_SECONDS}
                    data[fn] = {"rule": rule, "ts": now}
                    if len(data) > 200:
                        for k in sorted(data, key=lambda k: data[k].get("ts", 0))[:len(data) - 200]:
                            data.pop(k, None)
                    self._r.set(f"{TAINT_PREFIX}set:{kid}", json.dumps(data, ensure_ascii=False),
                                ex=DEFAULT_TTL_SECONDS)
                    return
                except Exception:
                    self._r = None
                    self._r_disabled = True
        # Memory fallback
        with self._mem_lock:
            taint = self._mem.setdefault("__taintset__", {})
            data = taint.setdefault(kid, {})
            for k in [k for k, v in data.items() if now - v.get("ts", 0) > DEFAULT_TTL_SECONDS]:
                data.pop(k, None)
            data[tk.rsplit(":", 1)[1]] = {"rule": rule, "ts": now}
            if len(data) > 200:
                for k in sorted(data, key=lambda k: data[k].get("ts", 0))[:len(data) - 200]:
                    data.pop(k, None)

    def is_tainted_file(self, key_id: str, mention: str) -> str:
        """同 key 下 mention 是否命中被污染文件名（双向后缀匹配，TTL 内）。

        返回命中的库内文件名，未命中返回 ""（falsy 即 miss，调用方可直接当布尔用）。
        """
        tk = _taint_key(key_id, mention)
        if not tk:
            return ""
        kid, men = tk.split(":")[2], tk.rsplit(":", 1)[1]
        data = None
        if self._r is not None:
            try:
                raw = self._r.get(f"{TAINT_PREFIX}set:{kid}")
                data = json.loads(raw) if raw else {}
            except Exception:
                self._r = None
                self._r_disabled = True
        if data is None:
            with self._mem_lock:
                data = dict(self._mem.get("__taintset__", {}).get(kid, {}))
        now = int(time.time())
        for fn, v in data.items():
            if now - v.get("ts", 0) > DEFAULT_TTL_SECONDS:
                continue
            if men == fn or men.endswith(fn) or fn.endswith(men):
                return fn
        return ""

    def backend(self) -> str:
        if self._r is not None:
            _u = urlsplit(self._r_url or "")
            _hp = (_u.hostname or "") + (f":{_u.port}" if _u.port else "")
            return f"redis ({_u.scheme}://{_hp}{_u.path})"
        if self._r_disabled:
            return "memory (redis disabled)"
        return "memory (no REDIS_URL)"

    # ---- 复杂度路由档位粘性（2026-10-26，见 intent_router._apply_sticky）----
    # 独立 Redis 键：不能挂 gateway:session:<sid> —— mark_confidential 整份覆盖写，
    # 会把档位冲掉。窗口 = 距上次使用（每次使用刷新），由调用方按 env 判，这里只管存取。

    def mark_sticky_tier(self, sid: str, tier: int) -> None:
        """记会话已达档位（只升不降：与已有值取 max，时间戳刷新 = 窗口随使用重置）。"""
        try:
            tier = int(tier)
        except (TypeError, ValueError):
            return
        sid = _normalize_session_id(sid)
        if not sid or tier not in (1, 2, 3):
            return
        now = int(time.time())
        try:
            if self._r is not None:
                key = f"{STICKY_PREFIX}{sid}"
                try:
                    raw = self._r.get(key)
                    prev = json.loads(raw).get("tier", 0) if raw else 0
                except Exception:
                    prev = 0
                self._r.set(key, json.dumps({"tier": max(tier, prev), "ts": now}),
                            ex=_STICKY_KEY_TTL)
                return
        except Exception:
            self._r = None
            self._r_disabled = True
        with self._mem_lock:
            d = self._mem.setdefault("__sticky_tier__", {})
            prev = d.get(sid) or {}
            d[sid] = {"tier": max(tier, int(prev.get("tier") or 0)), "ts": now}

    def get_sticky_tier(self, sid: Optional[str], window_s: int):
        """窗口内（window_s 秒，相对上次使用）返回 (tier, ts)；关（<=0）/无/过期 ⇒ None。"""
        if window_s <= 0:
            return None
        sid = _normalize_session_id(sid)
        if not sid:
            return None
        if self._r is not None:
            try:
                raw = self._r.get(f"{STICKY_PREFIX}{sid}")
                if raw:
                    d = json.loads(raw)
                    if now_ok(d.get("ts", 0), window_s):
                        return (int(d.get("tier") or 0), int(d.get("ts") or 0))
                return None
            except Exception:
                self._r = None
                self._r_disabled = True
        with self._mem_lock:
            d = (self._mem.get("__sticky_tier__") or {}).get(sid)
            if not d or not now_ok(d.get("ts", 0), window_s):
                if d:
                    (self._mem.get("__sticky_tier__") or {}).pop(sid, None)
                return None
            return (int(d.get("tier") or 0), int(d.get("ts") or 0))


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
