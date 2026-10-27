"""运行指标路由：/admin/metrics、/admin/metrics/timeseries。"""
import json

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

# admin_auth/admin_page_auth 定义在 main.py，避免循环导入，路由内懒加载
from .circuit_breaker import get_breaker
from .metrics import GATEWAY_VERSION, get_metrics
from .metrics_ring import get_ring
from .audit_store import get_audit_store

router = APIRouter()


def _admin_auth(request: Request):
    from .main import admin_auth as _aa
    return _aa(request)


def _admin_page_auth(request: Request):
    from .main import admin_page_auth as _apa
    return _apa(request)


@router.get("/admin/metrics")
async def metrics_endpoint(_ip: str = Depends(_admin_auth)):
    return Response(content=get_metrics().render(), media_type=f"text/plain; version={GATEWAY_VERSION}")


@router.get("/admin/metrics/timeseries", dependencies=[Depends(_admin_page_auth)])
async def metrics_timeseries(recent: int = 0):
    ring = get_ring()
    panel = get_metrics().export_panel()
    try:
        panel["circuit_providers"] = {p: s.get("state")
                                      for p, s in get_breaker().snapshot().items()}
    except Exception:
        panel["circuit_providers"] = {}
    try:
        _ast = get_audit_store()
        panel["audit"] = {"backend": _ast.backend(), "mysql": _ast.mysql_status()}
    except Exception:
        panel["audit"] = {"backend": "unknown", "mysql": "unknown"}
    panel["boot_ts"] = round(get_metrics()._start, 1)
    _last = next((p for p in reversed(ring.points) if "l2_up" in p), None)
    panel["l2_up"] = _last["l2_up"] if _last else None
    panel["ring"] = ring.status()
    ticks = list(ring.points)
    if recent > 0:
        ticks = ticks[-recent:]
    return JSONResponse({"interval_s": ring.tick_s, "ticks": ticks, "panel": panel})