"""客户端身份模型（P0：客户端 key 身份化）。

目标态下客户端 API key 只承担四件事 —— 鉴权、归因、限流/配额、审计与策略分级；
**永不作为上游 provider 凭据**（出境凭据归密钥池 `provider_keys` 管）。

本模块是**纯本地**的：不发起任何网络调用，只做登记表查询。

绑定键口径（限流分桶）：

=================  ==================================================
有 ``key_id``       ``key-<12 位零填充 id>-conf``   例 ``key-000000000042-conf``
无 ``key_id``       ``fp-<sha256 前 16 位>-anon``   例 ``fp-a1b2c3d4e5f60718-anon``
=================  ==================================================

**字符集约束**：绑定键只含 ``[A-Za-z0-9-]``，方案文档里写的 ``key:<key_id>``（冒号）
不可用。（P1-1 涉密标记已于 2026-09-18 移除，绑定键现仅用于限流分桶。）

2026-09-20 收紧后未登记身份一律 401：`Identity` 只来自登记表（`source="registry"`）。
"""
from __future__ import annotations

import hashlib
import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "Identity",
    "identity_v2_enabled",
    "token_fp",
    "bind_key",
    "identity_from_entry",
    "local_identity",
    "current_identity",
    "set_current_identity",
]

_TRUE_VALUES = ("1", "true", "yes", "on")


def identity_v2_enabled() -> bool:
    """P0 回滚开关。``false`` 时调用方回落旧口径（裸 token / ``tok:`` 前缀桶）。"""
    return os.getenv("AI_GATEWAY_IDENTITY_V2", "true").strip().lower() in _TRUE_VALUES


def token_fp(token: str) -> str:
    """``sha256(token)`` 前 16 位十六进制。**永不落明文**。"""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()[:16]


def bind_key(key_id: Optional[int], fp: str) -> str:
    """身份绑定键：限流分桶与涉密标记共用。字符集约束见模块 docstring。"""
    try:
        if key_id:
            return "key-%012d-conf" % int(key_id)
    except (TypeError, ValueError):
        pass
    return "fp-%s-anon" % ((fp or "0")[:16] or "0")


@dataclass(frozen=True)
class Identity:
    """一个通过鉴权的调用方身份。

    本对象**不持有凭据明文**（只有 `token_fp`），可以安全地进日志/审计/repr。
    """

    key_id: Optional[int]
    name: str
    owner: str
    source: str          # registry（2026-09-20 收紧后唯一来源）
    token_fp: str

    @property
    def bind_key(self) -> str:
        """限流桶键 / 涉密绑定键（同一口径：跟「人」不跟「凭据」）。"""
        return bind_key(self.key_id, self.token_fp)

    @property
    def is_registered(self) -> bool:
        return self.key_id is not None


def identity_from_entry(token: str, entry) -> Identity:
    """登记表命中。``entry`` = ``find_key_entry()`` 的 ``(kid, name, via_fuzzy)``。"""
    kid = entry[0] if entry else 0
    name = (entry[1] if entry else "") or ""
    try:
        key_id: Optional[int] = int(kid) or None
    except (TypeError, ValueError):
        key_id = None
    return Identity(
        key_id=key_id,
        name=name,
        owner="",
        source="registry",
        token_fp=token_fp(token),
    )


def local_identity(token: str, entry=None) -> Optional[Identity]:
    """**纯本地**解析：仅登记表。命中不了返回 ``None``（不发网络请求）。

    中间件用它做「尽力注入」；完整判定由 `main.auth()` 负责。
    2026-09-20 收紧：env dev key / 上游验签兜底已删，未登记一律无身份。
    """
    token = (token or "").strip()
    if not token:
        return None
    if entry is not None:
        if len(entry) > 1 and entry[1]:
            return identity_from_entry(token, entry)
    else:
        try:
            from .admin_store import get_admin_store
            _kid, _name, _via = get_admin_store().find_key_entry(token)
            if _name:
                return identity_from_entry(token, (_kid, _name, _via))
        except Exception:
            pass
    return None


# ---------------------------------------------------------------- 传播（P1）

# 身份从 HTTP 层传到 routing 层：routing 是**纯函数层**（拿不到 ``request``），
# 而「是否允许吃公司额度」必须看身份，所以用 ContextVar 送一程。
#
# 与 ``main._bind_identity()`` 注释里「不用 ContextVar」不矛盾 —— 那条讲的是
# **中间件 ↔ handler**（分属不同 context，父读不到子的 set）。这里是 handler →
# routing 的**向下调用**，同一 task：StreamingResponse 的生成器由 anyio task group
# 启动，context 在创建时复制，能读到 set 后的值。
#
# 读不到时返回 ``None`` ⇒ 准入 fail-closed（回落空密钥 ⇒ 503），不会出现
# 「没身份却用上公司额度」的路径。
_CURRENT_IDENTITY: ContextVar[Optional[Identity]] = ContextVar("gw_current_identity", default=None)


def set_current_identity(identity: Optional[Identity]):
    return _CURRENT_IDENTITY.set(identity)


def current_identity() -> Optional[Identity]:
    """当前请求的身份；调用栈没经过 ``auth()`` 时为 ``None``。"""
    return _CURRENT_IDENTITY.get()
