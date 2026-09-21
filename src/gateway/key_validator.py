# -*- coding: utf-8 -*-
"""Key Validator — 未登记 apikey 向 DeepSeek 验证（有效即放行，无需预登记，不做硬切）。

鉴权链：登记表（精确/模糊）-> env AI_GATEWAY_DEV_API_KEY -> 本验证器 -> 401。
对上游 GET {deepseek_base}/models 的结果映射：
- 200        -> 有效（正缓存，默认 1h）
- 401 / 403  -> 无效（负缓存，默认 60s）
- 其他状态/网络异常 -> 按 AI_GATEWAY_KEYVAL_FAIL_MODE 处置：
                        open（默认）=放行，closed=拒绝
                      放行只做短缓存（TTL_FAILOPEN，默认 60s）——上游抖动一次
                      不能把未登记 key 信任整整 1 小时。

缓存按 sha256(token)，不落明文；进程内 LRU 上限 MAX_CACHE。
"""
from __future__ import annotations

import hashlib
import logging
import os
import time

import httpx

log = logging.getLogger("gateway.key_validator")

TTL_VALID = float(os.getenv("AI_GATEWAY_KEYVAL_TTL_VALID", "3600"))
TTL_INVALID = float(os.getenv("AI_GATEWAY_KEYVAL_TTL_INVALID", "60"))
# fail-open 放行的缓存时长：远短于 TTL_VALID，避免上游抖动放大信任窗口
TTL_FAILOPEN = float(os.getenv("AI_GATEWAY_KEYVAL_TTL_FAILOPEN", "60"))
TIMEOUT = float(os.getenv("AI_GATEWAY_KEYVAL_TIMEOUT", "5"))
MAX_CACHE = 1000

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"

_cache: dict = {}  # sha256(token) -> (valid: bool, expire: float)


def _deepseek_base_url() -> str:
    from src.gateway import providers
    try:
        cfg = providers.load_routing()
        prov = cfg.get("deepseek")
        if prov is not None and prov.base_url:
            return str(prov.base_url).rstrip("/")
    except Exception:
        pass
    return DEFAULT_BASE_URL


def _remember(token_hash: str, valid: bool, now: float,
              ttl: float | None = None) -> None:
    if ttl is None:
        ttl = TTL_VALID if valid else TTL_INVALID
    _cache[token_hash] = (valid, now + ttl)
    if len(_cache) > MAX_CACHE:
        expired = [k for k, (_, exp) in _cache.items() if exp <= now]
        for k in expired:
            _cache.pop(k, None)
        while len(_cache) > MAX_CACHE:
            _cache.pop(next(iter(_cache)), None)


def validate(token: str) -> bool:
    """token 是否为有效 DeepSeek apikey（带缓存）。上游不可判定时 fail-open。"""
    token = (token or "").strip()
    if not token:
        return False
    th = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = time.time()
    hit = _cache.get(th)
    if hit and hit[1] > now:
        return hit[0]
    base = _deepseek_base_url()
    fail_closed = os.getenv("AI_GATEWAY_KEYVAL_FAIL_MODE", "open").lower() == "closed"
    try:
        with httpx.Client(timeout=TIMEOUT, trust_env=False) as client:
            r = client.get(base + "/models", headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 200:
            valid = True
        elif r.status_code in (401, 403):
            valid = False
        else:
            log.warning("key validator: unexpected status %s from %s/models (fail-%s)",
                        r.status_code, base, "closed" if fail_closed else "open")
            _remember(th, not fail_closed, now,
                      ttl=None if fail_closed else TTL_FAILOPEN)
            return not fail_closed
    except Exception as e:
        log.warning("key validator: upstream unreachable %s (fail-%s): %s",
                    base, "closed" if fail_closed else "open", str(e)[:120])
        _remember(th, not fail_closed, now,
                  ttl=None if fail_closed else TTL_FAILOPEN)
        return not fail_closed
    _remember(th, valid, now)
    return valid
