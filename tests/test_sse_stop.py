"""status_stream 见 sse_stop 立即正常收尾（systemd ExecStop 先发 SIGUSR1）。

回归 2026-10-13：SSE 长连接拖 uvicorn 优雅退出不完成，systemd 等满 90s
stop 超时 SIGKILL，浏览器端报 net::ERR_INCOMPLETE_CHUNKED_ENCODING。
修法 = unit ExecStop 先 kill -USR1 $MAINPID + 生成器见事件正常 return
（chunked 终止块照发，EventSource 静默重连）。
"""
import asyncio
import json

import src.gateway.admin_api as admin_api
from src.gateway.admin_api import status_stream


def test_sse_stream_returns_on_stop_event(monkeypatch):
    monkeypatch.setattr(admin_api, "_status_snapshot", lambda: {"health": {"status": "ok"}})

    async def run():
        resp = await status_stream()
        it = resp.body_iterator
        first = await it.__anext__()
        assert first.startswith("data: ")
        assert json.loads(first[len("data: "):])["health"]["status"] == "ok"
        admin_api.sse_stop.set()
        try:
            await it.__anext__()
            return False  # 还在出流 = 没收尾
        except StopAsyncIteration:
            return True

    try:
        assert asyncio.run(run())
    finally:
        admin_api.sse_stop.clear()
