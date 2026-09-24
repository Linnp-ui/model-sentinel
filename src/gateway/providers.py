"""
Provider Registry — 外网/内网模型注册表
src/gateway/providers.py

职责：
  - 从 routing.yaml 读取 provider 定义 + 路由默认策略
  - base_url / api_key_env 支持 ${ENV_VAR} 与 ${ENV_VAR:默认值} 展开
  - 按 mtime 热重载（改配置无需重启）
  - 密钥只从 env 读取，配置文件里永远只存变量名

约定：local: true 的 provider 视为内网，内容不出境。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .llms.registry import get_llm_handler

DEFAULT_ROUTING_FILE = Path(
    os.getenv("AI_GATEWAY_ROUTING_PATH") or (Path(__file__).parent.parent.parent / "routing.yaml")
)

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    """递归展开字符串里的 ${VAR} / ${VAR:default}"""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.getenv(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


@dataclass
class Provider:
    name: str
    base_url: str = ""
    api_key_env: str = ""
    api_key_value: str = ""         # 用户自定义模型直存密钥（与 api_key_env 二选一）
    api_mode: str = "openai"        # openai | anthropic
    kind: str = "chat"              # chat | embedding
    default_model: str = ""
    embedding_model: str = ""
    timeout: float = 60.0
    local: bool = False             # True = 内网，内容不出境
    extra_headers: Dict[str, str] = field(default_factory=dict)

    @property
    def api_key(self) -> str:
        if self.api_key_value:
            return self.api_key_value
        if not self.api_key_env:
            return ""
        return os.getenv(self.api_key_env, "").strip()

    @property
    def configured(self) -> bool:
        """内网 provider 无需密钥；外网 provider 必须配 key 才能真实转发。

        P1（T44）：密钥来源仍是 env（``api_key_env``）。P2 接入密钥池后，这里的判据
        由「env 有 key」扩展为「网关侧有可用 key（池 → env）」，消费点
        （``/health.providers.*.configured``、``routing._has_key``）语义同步扩展；
        调用方不需要知道 key 从哪来 —— 取 key 一律走 ``provider_keys.gateway_key()``。
        """
        return self.local or bool(self.api_key)

    def endpoint(self, kind: str = "chat", custom_path: str = "", *, model: str = "", stream: bool = False) -> str:
        """
        kind: chat | embedding | proxy（proxy 用于 /v1/* 兜底转发）
        custom_path 形如 "images/generations"，只在 kind=proxy 时生效
        """
        base = (self.base_url or "").rstrip("/")
        if not base:
            return ""
        # 端点拼接规则由 api_mode 对应的 BaseLLM handler 决定（见 llms/ 包）
        return get_llm_handler(self.api_mode).get_complete_url(base, kind, custom_path, model=model, stream=stream)

    def headers(self, api_key_override: str = "") -> Dict[str, str]:
        """
        api_key_override：本次请求**实际使用**的上游密钥，由 ``routing.upstream_key()``
        统一算出（P1 起它可能是客户端自带的 BYOK，也可能是网关持有的池/env 凭据；
        ``gateway_only`` 下**只可能**是后者）。网关自己的鉴权 key 与它是两回事。

        **不再回落**（T44 修，2026-09-17 生产实测）：外网 provider 只认
        ``api_key_override``，空串即「本请求无权/无凭据」，由调用方按
        ``on_missing_key`` 给 503。原 ``or self.api_key`` 会在空串时静默用上 env
        里的公司凭据 ⇒ 未登记身份绕过准入白吃公司额度。
        内网 provider 反而**必须忽略** override：``gateway_only`` 下
        ``upstream_key()`` 返回的是默认外网 provider 的 key，透传会把公司凭据
        写进 vLLM 的请求头与访问日志。
        """
        if self.local:
            key = self.api_key
        else:
            key = api_key_override.strip()
        # 鉴权头格式由 api_mode 对应的 BaseLLM handler 决定（见 llms/ 包）
        return get_llm_handler(self.api_mode).get_headers(key, self.extra_headers)

    def model_for(self, kind: str = "chat") -> str:
        if kind == "embedding":
            return self.embedding_model or self.default_model
        return self.default_model


_PROVIDER_FIELDS = {f.name for f in fields(Provider)}


@dataclass
class RoutingConfig:
    default_external: str = "openai"
    default_local: str = "vllm_local"
    default_embedding: str = "bge_m3"
    on_block: str = "fallback_local"      # fallback_local | reject
    on_missing_key: str = "mock"          # mock | reject
    providers: Dict[str, Provider] = field(default_factory=dict)

    def get(self, name: str | None) -> Provider | None:
        if not name:
            return None
        return self.providers.get(name)

    def local_provider(self) -> Provider | None:
        return self.get(self.default_local) or self._first(local=True, kind="chat")

    def external_provider(self) -> Provider | None:
        return self.get(self.default_external) or self._first(local=False, kind="chat")

    def embedding_provider(self) -> Provider | None:
        return self.get(self.default_embedding) or self._first(local=True, kind="embedding")

    def _first(self, local: bool, kind: str) -> Provider | None:
        for p in self.providers.values():
            if p.local == local and p.kind == kind:
                return p
        return None


# ---------------------------------------------------------------- 加载

def _builtin_providers() -> Dict[str, Provider]:
    """routing.yaml 缺失/缺字段时的兜底，保证内网永远可用"""
    return {
        "openai": Provider(
            name="openai",
            base_url="https://api.openai.com/v1",
            api_key_env="OPENAI_API_KEY",
            default_model="gpt-4o-mini",
            embedding_model="text-embedding-3-small",
        ),
        "vllm_local": Provider(
            name="vllm_local",
            base_url=os.getenv("OLLAMA_BASE_URL", "http://10.0.0.10:8000/v1"),
            api_key_env="OLLAMA_API_KEY",
            default_model="qwen2.5:7b",
            local=True,
            timeout=120,
        ),
    }


_CACHE: Dict[str, Any] = {"mtime": -1.0, "path": None, "config": None}


def load_routing(path: str | Path | None = None) -> RoutingConfig:
    """按 mtime 热重载；读取失败时回落到内置配置，保证网关不会因配置写错而整体不可用"""
    p = Path(path or os.getenv("AI_GATEWAY_ROUTING_PATH") or DEFAULT_ROUTING_FILE)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        if _CACHE["config"] is None:
            cfg = RoutingConfig(providers=_builtin_providers())
            _CACHE.update(mtime=-1.0, path=p, config=cfg)
        return _CACHE["config"]

    if _CACHE["config"] is not None and _CACHE["path"] == p and _CACHE["mtime"] == mtime:
        return _CACHE["config"]

    cfg = RoutingConfig(providers={})
    file_provs: Dict[str, Provider] = {}
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict):
            for k, v in (raw.get("routing") or {}).items():
                if hasattr(cfg, k) and isinstance(v, (str, int, float, bool)):
                    setattr(cfg, k, v)
            for name, item in (raw.get("providers") or {}).items():
                item = _expand_env(item or {})
                item = {k: v for k, v in item.items() if k in _PROVIDER_FIELDS}
                file_provs[name] = Provider(name=name, **item)
    except Exception as exc:  # 配置出错不能让网关挂掉
        cfg.load_error = str(exc)  # type: ignore[attr-defined]

    if file_provs:
        # 文件是单一事实源：文件里删掉的 provider 必须真消失
        # （旧实现以 _builtin_providers() 为基底合并，builtin 里的 openai 删不掉）
        cfg.providers = file_provs
    else:
        # 文件缺失/损坏/无 providers 段：内置兜底，保证内网永远可用
        cfg.providers = dict(_builtin_providers())

    _CACHE.update(mtime=mtime, path=p, config=cfg)
    return cfg


def invalidate_cache() -> None:
    _CACHE.update(mtime=-1.0, path=None, config=None)


def _routing_path() -> Path:
    return Path(os.getenv("AI_GATEWAY_ROUTING_PATH") or DEFAULT_ROUTING_FILE)


def load_raw() -> Dict[str, Any]:
    """routing.yaml 原始文档（**不展开 ${VAR}**）——admin 读改写用，保住 env 占位符。"""
    try:
        data = yaml.safe_load(_routing_path().read_text(encoding="utf-8")) or {}
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}


def save_providers(providers: Dict[str, Dict[str, Any]]) -> RoutingConfig:
    """校验 providers 并原子重写 routing.yaml（只动 providers 段，routing 段原样保留）。

    密钥只写 env 变量名（api_key_env）：api_key_env 非法 / 出现明文密钥一律拒绝。
    校验失败不动原文件；写成功后 mtime 热重载即时生效。
    """
    from .llms.registry import SUPPORTED_MODES

    for name, item in (providers or {}).items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(name)):
            raise ValueError(f"provider 名不合法：{name!r}（仅字母/数字/_/-，≤64）")
        item = dict(item or {})
        unknown = [k for k in item if k not in _PROVIDER_FIELDS]
        if unknown:
            raise ValueError(f"provider {name} 含未知字段：{unknown}")
        if not str(item.get("base_url") or "").strip():
            raise ValueError(f"provider {name} 缺 base_url")
        mode = str(item.get("api_mode") or "openai").lower()
        if mode not in SUPPORTED_MODES:
            raise ValueError(f"provider {name} api_mode 不支持：{mode!r}（支持 {', '.join(SUPPORTED_MODES)}）")
        if str(item.get("kind") or "chat") not in ("chat", "embedding"):
            raise ValueError(f"provider {name} kind 不合法：{item.get('kind')!r}")
        key_env = str(item.get("api_key_env") or "").strip()
        if key_env and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key_env):
            raise ValueError(f"provider {name} api_key_env 必须是大写 env 变量名：{key_env!r}")
        if str(item.get("api_key_value") or "").strip():
            raise ValueError(f"provider {name}：禁止明文密钥（api_key_value 仅用户注册模型内存放）")

    raw = load_raw()
    raw["providers"] = {
        str(name): {k: v for k, v in (item or {}).items() if v is not None}
        for name, item in (providers or {}).items()
    }
    p = _routing_path()
    text = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    invalidate_cache()
    return load_routing()
