"""健康检查路由：/health（状态条与外部探针共用）。"""
from fastapi import APIRouter

from .policy import load_policy
from .provider_probe import get_liveness
from .routing import load_routing

router = APIRouter()


@router.get("/health")
def health():
    return _health_payload()


def _health_payload() -> dict:
    cfg = load_routing()
    return {
        "status": "ok",
        "policy_count": len(load_policy()),
        "providers": {n: {"local": p.local, "kind": p.kind, "configured": p.configured} for n, p in cfg.providers.items()},
        "provider_liveness": get_liveness(),
        "default_external": cfg.default_external,
        "default_local": cfg.default_local,
        "on_block": cfg.on_block,
    }