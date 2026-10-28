"""事件循环不得被同步 I/O 卡死（「网关无响应」回归测试）。

背景（2026-10-27 用户报「网关有时候会无响应」）：/v1 的 async 路径上存在
同步外部 I/O（audit append 同步打 Redis、_bind_identity_ctx 同步查 MySQL、
session_store 同步 Redis）。本机 MySQL/Redis 是共享 docker 容器（MySQL 与
ragflow 共用）——任一抖动时，事件循环被同步调用整体卡住 ⇒ 不只是当前请求
慢，/v1 + /health + 管理台全部无响应，直到超时。socket_timeout=2 只限定
单次调用，不救「卡的是整个循环」。

口径（AGENTS.md 原注）：「审计 append() 同步打 Redis……观察项，见到抖动
再上 threadpool」——用户报无响应 = 见到抖动，本文件即该触发条件的回归锁。

反馈环：monkeypatch 让某处同步 I/O sleep 2s（模拟 Redis/MySQL 抖动），
同时打 /health —— 若 /health 被拖到 >1s，说明事件循环被卡（红）；
修复（挪出事件循环）后 /health 应恒 <1s（绿）。

运行: .venv/bin/python -m pytest tests/test_loop_responsive.py -v

⚠️ 必须用 `with TestClient(app) as client:`（共享 portal = 生产同款单事件循环）。
不用 with 时 starlette 每请求新建 portal/loop ⇒ 同步 sleep 卡不到别的请求，
本文件会假绿（2026-10-27 调试实录：无 with 时 /health 恒 <10ms）。
"""
import threading
import time

from fastapi.testclient import TestClient

from src.gateway import audit_store as _audit_store_mod
from src.gateway.main import app

KEY = "sk_shGD8A0lZpIJbcGavN8z6GBcSl-DGIsecJqgF4r0k_o"
H = {"Authorization": f"Bearer {KEY}"}


def _health_elapsed(client) -> float:
    t0 = time.time()
    r = client.get("/health")
    assert r.status_code == 200
    return time.time() - t0


def _chat_threaded(client, result: dict):
    """后台线程发一个走 allow 全链路（mock 上游）的 chat 请求。"""
    body = {"model": "AB-flash", "messages": [{"role": "user", "content": "你好"}]}
    result["resp"] = client.post("/v1/chat/completions", headers=H, json=body)


def test_health_stays_responsive_during_audit_append_stall(monkeypatch):
    """审计 append 卡 2s（Redis 抖动）时，/health 必须 <1s 响应。"""
    real = _audit_store_mod.AuditStore.append

    def blocked(self, entry, *a, **k):
        time.sleep(2)
        return real(self, entry, *a, **k)

    monkeypatch.setattr(_audit_store_mod.AuditStore, "append", blocked)
    result = {}
    with TestClient(app) as client:
        t = threading.Thread(target=_chat_threaded, args=(client, result))
        t.start()
        time.sleep(0.8)  # 等请求跑到收尾（mock 上游毫秒级，此时正卡在 append）
        elapsed = _health_elapsed(client)
        t.join()
    assert result["resp"].status_code == 200
    assert elapsed < 1.0, f"事件循环被同步审计写入卡住：/health 耗时 {elapsed:.2f}s"


def test_health_stays_responsive_during_weekly_cap_query_stall(monkeypatch):
    """_bind_identity_ctx 的 MySQL 单行查询卡 2s（MySQL 抖动）时，/health 必须 <1s。

    该查询在**每个** /v1 请求的入口依赖里同步跑，且与 admin 端点共享
    pool_size=2+max_overflow=2 的 4 连接池——池耗尽时 connect() 还能再等 30s。
    """
    from src.gateway import admin_store as _admin_mod
    store = _admin_mod.get_admin_store()
    real = store.get_key_weekly_cap

    def blocked(kid):
        time.sleep(2)
        return real(kid)

    monkeypatch.setattr(store, "get_key_weekly_cap", blocked)
    result = {}
    with TestClient(app) as client:
        t = threading.Thread(target=_chat_threaded, args=(client, result))
        t.start()
        time.sleep(0.8)  # 入口依赖正卡在 cap 查询
        elapsed = _health_elapsed(client)
        t.join()
    assert result["resp"].status_code == 200
    assert elapsed < 1.0, f"事件循环被同步 MySQL 查询卡住：/health 耗时 {elapsed:.2f}s"
