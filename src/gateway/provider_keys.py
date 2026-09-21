"""上游 provider 凭据池（P1：只读 env；P2 接 DB 池 + Fernet 加密）。

职责边界（读前必读）：

- 客户端 API key **永不**作为上游凭据（见 ``identity.py`` 模块 docstring）；本模块是
  唯一的「网关侧上游密钥」来源。
- P1 阶段只有 env 一层（``routing.yaml`` 的 ``api_key_env`` ⇒ ``Provider.api_key``）——
  **不建表、不落库**。P2 才加 ``provider_key`` 表（多把/权重/cooldown），届时
  ``gateway_key()`` 的读取顺序变为「DB 池 → env → 空」，**调用方签名不变**。
- 准入策略（§16.1 裁决）：**只有具名登记身份**可以吃公司额度，判据
  ``identity.is_registered``（⇔ ``key_id is not None``）。**不可拿 ``owner`` 判**
  （``identity_from_entry()`` 的 owner 恒为空串，会让所有登记身份 fail-closed）、
  ``tier`` 只作可选二次收紧。BYOK（客户端自带 key）用他自己的额度，不走这里。
- 模式开关 ``AI_GATEWAY_UPSTREAM_KEY_MODE``：``passthrough``（默认，灰度期）|
  ``gateway_only``。**取值拼错一律回落 passthrough** —— 静默切进 ``gateway_only``
  会让未配凭据的部署整条出境路径当天全量 503（方案 §11 I10），属危险默认。
- 凭据明文**永不**进日志/审计/指标 label/``/health``/``/admin/api/providers``；
  指标 label 只用 provider 名。
"""
from __future__ import annotations

import os
from typing import Any

__all__ = [
    "MODE_ENV",
    "MODE_PASSTHROUGH",
    "MODE_GATEWAY_ONLY",
    "upstream_key_mode",
    "is_gateway_only",
    "may_use_gateway_key",
    "gateway_key",
    "note_passthrough",
    "note_rejected",
    "note_exhausted",
]

MODE_ENV = "AI_GATEWAY_UPSTREAM_KEY_MODE"
MODE_PASSTHROUGH = "passthrough"
MODE_GATEWAY_ONLY = "gateway_only"


def upstream_key_mode() -> str:
    """``passthrough``（默认）| ``gateway_only``。见模块 docstring 的「危险默认」说明。"""
    raw = (os.getenv(MODE_ENV) or "").strip().lower()
    return MODE_GATEWAY_ONLY if raw == MODE_GATEWAY_ONLY else MODE_PASSTHROUGH


def is_gateway_only() -> bool:
    return upstream_key_mode() == MODE_GATEWAY_ONLY


def may_use_gateway_key(identity: Any) -> bool:
    """是否允许该身份使用网关持有的上游凭据（= 吃公司额度）。

    判据固定为 ``is_registered``；拿 ``owner``/``tier`` 当门会误伤（见模块 docstring）。
    身份不可得（``None``，例如调用栈没经过 ``auth()``）一律 **fail-closed**。
    """
    if identity is None:
        return False
    return bool(getattr(identity, "is_registered", False))


def gateway_key(prov: Any) -> str:
    """网关侧该 provider 当前可用的凭据（P1：env 一层）。无则返回空串。"""
    if prov is None:
        return ""
    return (getattr(prov, "api_key", "") or "").strip()


def _metrics():
    from .metrics import get_metrics

    return get_metrics()


def note_passthrough() -> None:
    """灰度观测：客户端自带上游凭据的请求数。

    D4 的切换判据 —— ``gateway_upstream_key_passthrough_total`` 连续 7 天为 0 后方可切
    ``gateway_only``。**网关兜底取到的 key 不计入**（否则该数永远归不了零）。
    """
    try:
        _metrics().inc_upstream_key_passthrough()
    except Exception:
        pass


def note_rejected() -> None:
    """``gateway_only`` 下客户端仍携带 ``X-Upstream-Api-Key`` 的请求数（期望随客户端改造归零）。"""
    try:
        _metrics().inc_upstream_key_rejected()
    except Exception:
        pass


def note_exhausted(provider: str) -> None:
    """池空（`AI_GATEWAY_UPSTREAM_KEY_COOLDOWN` 全冷却 / 未配凭据）被拒的次数。"""
    try:
        _metrics().inc_upstream_key_exhausted(str(provider or ""))
    except Exception:
        pass
