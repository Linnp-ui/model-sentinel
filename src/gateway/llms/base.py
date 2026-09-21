"""
BaseLLM — 上游 provider 线协议基类（参考 LiteLLM 的 BaseConfig / transformation.py 设计）
src/gateway/llms/base.py

对齐 LiteLLM 的三个核心约定：
  1. 网关内部统一格式 = OpenAI chat.completions（LiteLLM 同样以 openai 格式为中心）；
  2. 每种上游线协议（api_mode）一个 handler 类，实现 transform_request / transform_response，
     上游差异被封装在 handler 内，routing.py 不出现 `if api_mode == ...` 分支；
  3. 新增一种上游协议 = 新增一个 BaseLLM 子类 + 在 registry.py 登记，
     chat / embeddings / messages / responses 四条链路自动生效。

注意：本包不得 import .providers（避免循环依赖）；需要 Provider 类型时用 TYPE_CHECKING。
例外：允许 import ``..provider_keys``（平级纯策略模块，只读 env，不反向依赖本包）——
上游凭据模式的判定只允许有一处实现。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, Optional

# 平级模块，不反向依赖本包（不是 .providers，无循环依赖）。模式判定的唯一来源：
# 同语义若有第二份实现必然漂移，所以这里不自己读 env。
from ..provider_keys import is_gateway_only

if TYPE_CHECKING:  # pragma: no cover
    from ..providers import Provider


class BaseLLM(ABC):
    """单个上游线协议的适配器。一个类 = 一种 api_mode（LiteLLM: 一个 provider Config）。"""

    mode: str = ""

    # ---------------------------------------------------------------- URL

    @abstractmethod
    def get_complete_url(self, base_url: str, kind: str = "chat", custom_path: str = "",
                         *, model: str = "", stream: bool = False) -> str:
        """
        由 provider 的 base_url 拼出具体端点。
        kind: chat | embedding | proxy（proxy 用于 /v1/* 兜底转发，custom_path 形如 "models"）
        """

    # ---------------------------------------------------------------- 请求头

    @abstractmethod
    def get_headers(self, api_key: str, extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """按上游协议生成鉴权与必要的固定头。"""

    # ---------------------------------------------------------------- 请求/响应变换

    @abstractmethod
    def transform_request(
        self, body: Dict[str, Any], model: str, *, stream: bool = False
    ) -> Dict[str, Any]:
        """
        网关标准体（OpenAI chat 格式，已含 model/max_tokens）-> 上游线格式。
        LiteLLM 对应 transform_request(pydantic_obj)。
        """

    @abstractmethod
    def transform_response(self, data: Dict[str, Any], model: str) -> Dict[str, Any]:
        """
        上游线格式响应 -> 网关标准体（OpenAI chat 格式）。
        LiteLLM 对应 transform_response(model_response)。
        """

    # ---------------------------------------------------------------- 流式

    @property
    def stream_passthrough(self) -> bool:
        """
        True  = 上游 SSE 与网关标准格式一致，字节流直通（openai 兼容系）；
        False = 走 transform_stream_line 逐行转换（如 anthropic events -> openai chunks）。
        """
        return True

    def transform_stream_line(self, line: str, model: str) -> Optional[bytes]:
        """
        仅 stream_passthrough=False 时被调用。
        输入上游 SSE 的一行（aiter_lines），返回要下发的标准格式 SSE 字节块；返回 None 则跳过。
        """
        return None

    # ---------------------------------------------------------------- 环境校验

    def validate_environment(self, prov: "Provider", ukey: str) -> Optional[str]:
        """
        转发前校验该 provider 是否具备真实转发条件（LiteLLM 同名方法）。
        返回 None 表示可转发；返回字符串则作为 503 错误信息。

        - 内网 provider：无需密钥。
        - 外网 provider：**本次请求解析到凭据**（``ukey`` 非空）才能转发。
          判据**不**含 ``prov.configured``（T44 修）：那是「网关侧有库存」，
          不是「本请求有权用」；混用会让未登记身份白吃公司额度（生产实测）。
        - ``ukey`` 由 ``routing.upstream_key()`` 独占决定：``gateway_only`` 下
          只可能是网关持有的凭据（客户端自带 ``X-Upstream-Api-Key`` 直接 400），
          ``passthrough`` 下还接受客户端自带（灰度期不断既有 BYOK 用法）。
        - 报文不再引导客户端携带 ``X-Upstream-Api-Key``（凭据归口，方案 §4.2 C4）。
        """
        if prov.local:
            return None
        if ukey:
            return None
        if is_gateway_only():
            return (
                f"网关未配置 {prov.name} 凭据，请联系管理员"
                f"（{prov.api_key_env or '无'}；不再支持随请求携带上游密钥）"
            )
        return (
            f"provider {prov.name} 未配置密钥（{prov.api_key_env or '无'}，"
            f"可随请求携带 X-Upstream-Api-Key）"
        )
