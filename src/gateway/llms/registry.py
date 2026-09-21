"""
Provider handler 注册表（参考 LiteLLM 的 provider dispatch / custom_llm_provider 设计）
src/gateway/llms/registry.py

api_mode 字符串 -> BaseLLM handler 实例。
新增上游线协议：实现 BaseLLM 子类后在此登记一行即可，routing.py 零改动。
"""
from __future__ import annotations

from .anthropic_llm import AnthropicLLM
from .base import BaseLLM
from .openai_compat import OpenAICompatLLM

_HANDLERS: dict[str, BaseLLM] = {
    OpenAICompatLLM.mode: OpenAICompatLLM(),
    AnthropicLLM.mode: AnthropicLLM(),
}

SUPPORTED_MODES = tuple(_HANDLERS)


def get_llm_handler(api_mode: str | None) -> BaseLLM:
    """按 api_mode 取 handler；未知模式直接报错（配置错误应尽早暴露）。"""
    mode = (api_mode or "openai").lower()
    handler = _HANDLERS.get(mode)
    if handler is None:
        raise ValueError(
            f"unsupported api_mode: {api_mode!r}（支持: {', '.join(SUPPORTED_MODES)}）"
        )
    return handler
