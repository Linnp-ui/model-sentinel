"""静态/下载路由：/, favicon, ca.crt, ca.crl。"""
import os

from fastapi import APIRouter
from fastapi.responses import FileResponse, RedirectResponse, Response

router = APIRouter()


@router.get("/")
async def index():
    return RedirectResponse("/admin")


@router.get("/favicon.ico")
@router.get("/favicon.png")
async def favicon():
    p = os.path.join(os.path.dirname(__file__), "../../static/favicon.png")
    if os.path.exists(p):
        return FileResponse(p, media_type="image/png")
    return Response(status_code=404)


@router.get("/ca.crt")
async def ca_cert():
    p = os.path.join(os.path.expanduser("~"), ".config", "gateway", "tls", "ca.crt")
    if os.path.exists(p):
        return FileResponse(p, media_type="application/x-x509-ca-cert", filename="gateway-internal-ca.crt")
    return Response(status_code=404)


@router.get("/ca.crl")
async def ca_crl():
    p = os.path.join(os.path.expanduser("~"), ".config", "gateway", "tls", "ca.crl")
    if os.path.exists(p):
        return FileResponse(p, media_type="application/pkix-crl")
    return Response(status_code=404)