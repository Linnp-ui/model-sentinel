"""Model Policy — 管理后台模型配置层（需求 1.1 / 1.2 / 1.3）。
src/gateway/model_policy.py

- model_policy.yaml（仓库根，AI_GATEWAY_MODEL_POLICY_PATH 可覆盖）按 mtime 热重载，
  与 providers.py 同款模式。
- external_candidates: 1.2 多模型策略的候选表。状态驱动切换（仅非流式 chat）：
    报错/异常中断 -> 按 rank/价格顺序换下一个供应商模型
    上下文溢出    -> 切 context_length 最长的候选
- internal_models + load_balance_mode: 1.3（单内部模型，分流暂不实现，仅登记）
- review_model.threshold: 1.1 审查阈值，运行时生效（small_model.is_confidential 消费）
- fallback trace 用 ContextVar 记录本次请求的切换链，main.py 汇入响应 gateway meta。
"""
from __future__ import annotations

import contextvars
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

DEFAULT_MODEL_POLICY = Path(__file__).parent.parent.parent / "model_policy.yaml"

# 上下文溢出的典型上游报错特征（openai/deepseek/vllm/openrouter 汇总）
_OVERFLOW_PATTERNS = (
    "context length",
    "maximum context",
    "context_length_exceeded",
    "prompt too long",
    "prompt length",
    "max_num_tokens",
    "reduce the length",
    "too many tokens",
    "input length exceeds",
)


# ---------------------------------------------------------------------------
# Pydantic schema
# ---------------------------------------------------------------------------

class ExternalCandidate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str
    model: str
    price_per_1m_in: float = 0.0
    price_per_1m_out: float = 0.0
    context_length: int = 0
    rate: float = 1.0
    note: str = ""
    enabled: bool = True
    rank: int = 100


class InternalModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str
    model: str
    note: str = ""
    enabled: bool = True


class ModelLimit(BaseModel):
    """模型限流（全局每分钟上限，按最终解析的 provider/model 命中）。

    model 支持裸模型名（任意 provider）或 "provider/model"（须双匹配）。
    rpm<=0 或 enabled=false = 不限。
    """
    model_config = ConfigDict(extra="ignore")

    model: str
    rpm: int = Field(default=0, ge=0)
    enabled: bool = True
    note: str = ""


class ReviewModelCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    # --- P2：L2 视野（scope）配置。全部走 mtime 热重载 ⇒ 调档不需重启。 ---
    # scopes       参与判定的块（决定 action）；默认 ["last_user"] = 今天的行为。
    # chunk_chars  每块送审字符上限；budget_chars 全部块总预算，先到先用。
    scopes: List[str] = Field(default_factory=lambda: ["last_user"])
    chunk_chars: int = Field(default=1200, ge=1, le=20000)
    budget_chars: int = Field(default=5000, ge=0, le=200000)
    note: str = ""


class ModelPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore")

    external_candidates: List[ExternalCandidate] = Field(default_factory=list)
    internal_models: List[InternalModel] = Field(default_factory=list)
    model_limits: List[ModelLimit] = Field(default_factory=list)
    load_balance_mode: Literal["none", "user_hash", "load"] = "none"
    review_model: ReviewModelCfg = Field(default_factory=ReviewModelCfg)


# ---------------------------------------------------------------------------
# 加载（mtime 热重载）
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_cache: Dict[str, Any] = {"path": None, "mtime": None, "policy": None}


def _policy_path() -> Path:
    return Path(os.getenv("AI_GATEWAY_MODEL_POLICY_PATH", "") or DEFAULT_MODEL_POLICY)


def invalidate_model_policy_cache() -> None:
    with _lock:
        _cache.update({"path": None, "mtime": None, "policy": None})


def load_model_policy() -> ModelPolicy:
    p = _policy_path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = None
    with _lock:
        if (
            _cache["policy"] is not None
            and _cache["path"] == str(p)
            and _cache["mtime"] == mtime
        ):
            return _cache["policy"]
    if mtime is None:
        policy = ModelPolicy()
    else:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        doc = data.get("model_policy", data) if isinstance(data, dict) else {}
        policy = ModelPolicy.model_validate(doc or {})
    with _lock:
        _cache.update({"path": str(p), "mtime": mtime, "policy": policy})
    return policy


def save_model_policy(data: Dict[str, Any]) -> ModelPolicy:
    """校验并原子落盘。ValidationError 时不动原文件。"""
    policy = ModelPolicy.model_validate(data)
    p = _policy_path()
    payload = {"model_policy": policy.model_dump()}
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    invalidate_model_policy_cache()
    return policy


# ---------------------------------------------------------------------------
# 1.2 状态驱动切换的决策
# ---------------------------------------------------------------------------

def external_candidates() -> List[Dict[str, Any]]:
    """启用的外部候选，按 (rank, price_per_1m_in) 升序 —— 即"最便宜优先"。"""
    pol = load_model_policy()
    cands = [c.model_dump() for c in pol.external_candidates if c.enabled]
    cands.sort(key=lambda c: (c.get("rank", 100), c.get("price_per_1m_in", 0.0)))
    return cands


def classify_failure(status_code: int, message: str) -> str:
    """上游失败分类：'overflow'（上下文溢出）| 'error'（报错/异常中断/限流）。"""
    msg = (message or "").lower()
    if status_code == 400 and any(pat in msg for pat in _OVERFLOW_PATTERNS):
        return "overflow"
    if "context_length_exceeded" in msg or "max_num_tokens" in msg:
        return "overflow"
    return "error"


def pick_fallback_candidates(
    tried_keys: List[str],
    reason: str,
    current_ctx: int,
) -> List[Dict[str, Any]]:
    """给 routing 层的下一跳候选（有序）。tried_keys 形如 ['deepseek/deepseek-chat']。

    - overflow: 在未试过的候选里挑 context_length 严格大于当前(或当前未知视为 0)的，
      按上下文长度升序 —— 最小够用优先（同长度再按 rank/价格）
    - error:    未试过的候选按 (rank, price) 升序 —— 换其他供应商模型
    返回元素: {provider, model, context_length}
    """
    cands = [
        c for c in external_candidates()
        if f"{c['provider']}/{c['model']}" not in set(tried_keys)
    ]
    if reason == "overflow":
        bigger = [c for c in cands if c.get("context_length", 0) > current_ctx]
        bigger.sort(key=lambda c: (c.get("context_length", 0),
                                   c.get("rank", 100),
                                   c.get("price_per_1m_in", 0.0)))
        return bigger
    return cands


# ---------------------------------------------------------------------------
# 模型限流（routing.resolve 消费：最终 provider/model 的全局 RPM）
# ---------------------------------------------------------------------------

def model_rpm(provider_name: str, model: str) -> int:
    """该 (provider, model) 的全局每分钟上限；0 = 未配置/禁用 = 不限。

    命中规则：配置 "provider/model" 须双匹配；裸模型名只按 model 匹配。
    随 policy mtime 热重载（load_model_policy 缓存），改配置不用重启。
    """
    try:
        pol = load_model_policy()
    except Exception:
        return 0
    for m in pol.model_limits:
        if not m.enabled or m.rpm <= 0 or not m.model:
            continue
        if "/" in m.model:
            p, _, mm = m.model.partition("/")
            if mm == model and p == provider_name:
                return m.rpm
        elif m.model == model:
            return m.rpm
    return 0


# ---------------------------------------------------------------------------
# 1.1 审查阈值（small_model.is_confidential 消费）
# ---------------------------------------------------------------------------

def review_threshold() -> float:
    try:
        return float(load_model_policy().review_model.threshold)
    except Exception:
        return 0.7


# --- P2：scope 配置访问器（main.py 只经这几个函数读，不直接摸 ReviewModelCfg）---

def review_scopes() -> List[str]:
    """参与判定的 scope 列表。读不到就是「今天的行为」。"""
    try:
        return list(load_model_policy().review_model.scopes or ["last_user"])
    except Exception:
        return ["last_user"]


def review_chunk_chars() -> int:
    try:
        return max(1, int(load_model_policy().review_model.chunk_chars or 1200))
    except Exception:
        return 1200


def review_budget_chars() -> int:
    try:
        return max(0, int(load_model_policy().review_model.budget_chars or 0))
    except Exception:
        return 5000


# ---------------------------------------------------------------------------
# fallback trace（本次请求的切换链，main.py 汇入 gateway meta）
# ---------------------------------------------------------------------------

_fallback_trace: contextvars.ContextVar[List[Dict[str, Any]]] = contextvars.ContextVar(
    "gw_model_fallback", default=[]
)


def push_fallback(entry: Dict[str, Any]) -> None:
    lst = list(_fallback_trace.get())
    lst.append(entry)
    _fallback_trace.set(lst)


def pop_fallback_trace() -> List[Dict[str, Any]]:
    lst = _fallback_trace.get()
    if lst:
        _fallback_trace.set([])
    return lst


def reset_fallback_trace() -> None:
    _fallback_trace.set([])
