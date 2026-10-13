"""provider 主动存活探测：后台循环 GET {base_url}/models（零 token，不发起真实模型调用），
缓存每个 provider 的 up/latency/status/error。

背景（2026-10-10）：vLLM 驱动错配致 8000/8001/8002 全挂，但 /health 与状态条只报
``configured``（base_url 在不在），90 分钟无告警。本模块补上「端点通不通」这层。

设计：探测与读缓存分离。后台任务每 N 秒打一轮（asyncio.to_thread，不阻塞事件循环），
``get_liveness()`` 是纯内存读，``_health_payload()``（/health 与状态条 SSE 同源）直接
拼进响应——/health 因此保持快，且模型挂了状态条会亮红。

key 解析复用 admin_api._models_list_key（同一套 T44 三级规则），探测逻辑与
POST /admin/api/providers/check 逐字段一致，手动按钮与后台探测口径不漂移。
"""
import asyncio
import os
import threading
import time

import httpx

from .providers import load_routing
from .routing import upstream_proxy

# 探测间隔（默认 30s，比状态条 60s 推送更密，模型挂了最快 30s 内亮灯）
_INTERVAL_S = float(os.getenv("AI_GATEWAY_PROVIDER_PROBE_INTERVAL", "30"))
# 单 provider 超时（连不通时每个等这么久；6 个全挂最多 ~30s 一轮，在 worker 线程不卡事件循环）
_TIMEOUT_S = float(os.getenv("AI_GATEWAY_PROVIDER_PROBE_TIMEOUT", "5"))

_liveness: dict = {}
_lock = threading.Lock()
_task = None


def probe_one(prov, timeout: float = _TIMEOUT_S) -> dict:
    """单 provider：GET {base_url}/models 测连通+鉴权+延迟。口径同 /providers/check。"""
    # 懒 import 避免模块级拉进整个 admin_api（其模块级又 import 一堆重依赖）
    from .admin_api import _models_list_key

    entry: dict = {"name": prov.name, "ok": False, "local": bool(prov.local)}
    base = (prov.base_url or "").rstrip("/")
    if not base:
        entry["error"] = "未配置 base_url"
        return entry
    key, src = _models_list_key(prov, "")
    entry["key_source"] = src
    t0 = time.monotonic()
    try:
        resp = httpx.get(f"{base}/models", headers=prov.headers(key),
                         timeout=timeout, trust_env=False, proxy=upstream_proxy())
        entry["latency_ms"] = round((time.monotonic() - t0) * 1000)
        entry["status"] = resp.status_code
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("data") if isinstance(data, dict) else data
            entry["ok"] = True
            entry["model_count"] = len([i for i in (items or []) if i])
        else:
            entry["error"] = f"上游 HTTP {resp.status_code}"
    except Exception as e:
        entry["latency_ms"] = round((time.monotonic() - t0) * 1000)
        entry["error"] = repr(e)[:200]
    entry["checked_at"] = time.time()
    return entry


def _round() -> None:
    """打一轮：全部 provider 顺序探测，写入缓存。在 worker 线程跑（不占事件循环）。"""
    cfg = load_routing()
    results = {}
    for name, prov in cfg.providers.items():
        results[name] = probe_one(prov)
    with _lock:
        _liveness.update(results)


def get_liveness() -> dict:
    """纯内存读缓存（/health 与状态条 SSE 调它，不主动打网络）。"""
    with _lock:
        return {k: dict(v) for k, v in _liveness.items()}


async def _loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_round)
        except Exception:
            # 探测本身失败不致命：保留上一轮缓存，下轮重试
            pass
        await asyncio.sleep(_INTERVAL_S)


def _disabled() -> bool:
    """探测是否关闭。生产默认开；测试态自动关（零真实出境，别在 pytest 里打外网/内网）。
    ① 显式 env AI_GATEWAY_PROVIDER_PROBE=false/0/off/no；
    ② 测试态识别：TestClient 的 client.host 是合成串 testclient，只会出现在测试
       的 admin allowlist（conftest 注入），据此判定测试态。生产 allowlist 无此串。"""
    if os.getenv("AI_GATEWAY_PROVIDER_PROBE", "").strip().lower() in ("0", "false", "off", "no"):
        return True
    if "testclient" in os.getenv("AI_GATEWAY_ADMIN_IP_ALLOWLIST", ""):
        return True
    return False


def start() -> None:
    """在 startup 钩子里调：起后台循环 + 立即打首轮（别让首屏等一个间隔）。测试态 no-op。"""
    global _task
    if _disabled():
        return
    loop = asyncio.get_running_loop()
    loop.create_task(asyncio.to_thread(_round))
    if _task is None:
        _task = loop.create_task(_loop())
