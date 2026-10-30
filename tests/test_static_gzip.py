"""静态资源 gzip + 缓存头（管理台首屏 561KB JS 原样传输 → gzip 后约 190KB）。

背景 2026-10-20：web/admin 拆包后首屏仍要传 561KB（内网也吃带宽），
故在网关侧开 GZipMiddleware；assets/ 是内容哈希文件名 ⇒ immutable 长缓存。

SSE 安全：starlette 的 GZipMiddleware 默认排除 text/event-stream，且流式响应
整体不压缩 ⇒ 管理台状态条（/admin/api/status/stream）推送不受缓冲影响。
"""
import glob
import os

from fastapi.testclient import TestClient
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES, GZipMiddleware

from src.gateway.main import app

client = TestClient(app)

# 只看大于 GZipMiddleware minimum_size(1024) 的产物：小于阈值的 chunk（如 Arrow）本就不压
_ASSETS = [p for p in sorted(glob.glob(os.path.join("static", "admin", "assets", "*.js")))
           if os.path.getsize(p) >= 1024]
_INDEX = os.path.join("static", "admin", "index.html")


def test_gzip_middleware_installed_and_excludes_sse():
    mws = [m for m in app.user_middleware if m.cls is GZipMiddleware]
    assert mws, "GZipMiddleware 未挂载"
    assert mws[0].kwargs.get("compresslevel") == 6
    assert "text/event-stream" in DEFAULT_EXCLUDED_CONTENT_TYPES


def test_static_asset_gzipped_and_immutable():
    assert _ASSETS, "static/admin/assets 下无 js（先在 web/admin 执行 npm run build）"
    p = _ASSETS[0]
    name = os.path.basename(p)
    r = client.get(f"/static/admin/assets/{name}", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    assert r.headers.get("cache-control") == "public, max-age=31536000, immutable"
    # 压缩后体积必须小于原文件（否则压缩没生效）
    assert int(r.headers.get("content-length", "0")) < os.path.getsize(p)
    # httpx 自动解压 ⇒ 内容与原文件一致（不能压坏）
    with open(p, "rb") as f:
        assert r.content == f.read()


def test_static_asset_identity_not_encoded():
    assert _ASSETS
    name = os.path.basename(_ASSETS[0])
    r = client.get(f"/static/admin/assets/{name}", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers


def test_static_entry_no_cache():
    if not os.path.exists(_INDEX):
        return  # 未构建环境无入口文件
    r = client.get("/static/admin/index.html")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"
