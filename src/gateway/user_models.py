"""
用户自定义模型注册表
src/gateway/user_models.py

允许最终用户通过 /v1/models/register 自助注册自己的外网模型（endpoint + key），
网关审查通过后转发到该模型。安全约束：
  - 仅允许公司出口白名单内的域名（AI_GATEWAY_EGRESS_DOMAINS），防止绕过审查外发到私建 endpoint
  - 用户模型恒为 local=False（外网），审查不通过时仍按策略降级本地，不会绕过审查
  - 密钥持久化：有 REDIS_URL 时存 Redis（不入库、不进 routing.yaml），否则仅内存（重启丢失）
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .providers import Provider

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_DEFAULT_EGRESS_DOMAINS = (
    "api.openai.com,api.deepseek.com,api.anthropic.com,"
    "dashscope.aliyuncs.com,open.bigmodel.cn,api.perplexity.ai,"
    "api.mistral.ai,generativelanguage.googleapis.com"
)


def egress_domains() -> List[str]:
    raw = os.getenv("AI_GATEWAY_EGRESS_DOMAINS", _DEFAULT_EGRESS_DOMAINS)
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def _host_allowed(host: str) -> bool:
    host = (host or "").lower()
    for d in egress_domains():
        if host == d or host.endswith("." + d):
            return True
    return False


@dataclass
class UserModel:
    name: str
    base_url: str
    api_key: str
    api_mode: str = "openai"          # openai | anthropic
    default_model: str = ""
    owner: str = "anonymous"
    created_at: float = field(default_factory=time.time)

    def to_provider(self) -> Provider:
        return Provider(
            name=self.name,
            base_url=self.base_url,
            api_key_value=self.api_key,
            api_mode=self.api_mode,
            default_model=self.default_model or "",
            local=False,               # 用户模型恒为外网，受审查与降级约束
        )

    def public(self) -> Dict[str, Any]:
        masked = self.api_key
        if len(masked) > 6:
            masked = masked[:4] + "..." + masked[-4:]
        return {
            "name": self.name,
            "base_url": self.base_url,
            "api_mode": self.api_mode,
            "default_model": self.default_model,
            "owner": self.owner,
            "api_key_masked": masked,
            "created_at": self.created_at,
        }


class UserModelStore:
    def __init__(self) -> None:
        self._mem: Dict[str, UserModel] = {}
        self._redis = None
        self._redis_key = "gateway:user_models"
        url = os.getenv("REDIS_URL")
        if url:
            try:
                import redis  # type: ignore

                self._redis = redis.from_url(url, decode_responses=True)
                self._load_from_redis()
            except Exception:
                self._redis = None

    def _load_from_redis(self) -> None:
        try:
            data = self._redis.hgetall(self._redis_key)  # type: ignore[union-attr]
            for name, raw in (data or {}).items():
                self._mem[name] = UserModel(**json.loads(raw))
        except Exception:
            pass

    def register(self, m: UserModel) -> None:
        self._mem[m.name] = m
        if self._redis is not None:
            try:
                self._redis.hset(self._redis_key, m.name, json.dumps(asdict(m)))  # type: ignore[union-attr]
            except Exception:
                pass

    def get(self, name: str) -> Optional[UserModel]:
        return self._mem.get(name)

    def delete(self, name: str) -> bool:
        if name in self._mem:
            del self._mem[name]
            if self._redis is not None:
                try:
                    self._redis.hdel(self._redis_key, name)  # type: ignore[union-attr]
                except Exception:
                    pass
            return True
        return False

    def list(self) -> List[UserModel]:
        return list(self._mem.values())

_STORE = UserModelStore()


def get_store() -> UserModelStore:
    return _STORE


def validate(name: str, base_url: str, api_key: str, api_mode: str) -> Optional[str]:
    """返回错误信息字符串；None 表示校验通过"""
    if not _NAME_RE.match(name or ""):
        return "name 仅允许字母数字/下划线/横线，长度 1-64"
    if get_store().get(name):
        return f"模型名 {name} 已存在"
    if api_mode not in ("openai", "anthropic"):
        return "api_mode 仅支持 openai / anthropic"
    if not base_url or not base_url.startswith("https://"):
        return "base_url 必须以 https:// 开头"
    host = urlparse(base_url).hostname or ""
    if not _host_allowed(host):
        return f"域名 {host} 不在公司出口白名单（AI_GATEWAY_EGRESS_DOMAINS）"
    if not api_key:
        return "api_key 不能为空"
    return None


def find_provider(name: str | None) -> Optional[Provider]:
    """供 routing.resolve 调用：静态 provider 找不到时回退到用户自定义模型"""
    m = get_store().get(name) if name else None
    return m.to_provider() if m else None
