"""
Policy Engine — YAML-driven priority chain with Pydantic validation.
src/gateway/policy.py

支持 when: always / file.ext / file.headers contains_any / text contains_any
/ text matches_regex / findings.pii_count

设计要点 (P2 改进):
  - load_policy() 用 Pydantic 强校验, 启动时 / 热重载时即报错 (fail-fast),
    避免 "yaml 字段名拼错 → 静默失效" 这种常见 bug。
  - Policy 对象支持 r["action"] / r["name"] 字典式访问, 保持与旧 dict 路径
    兼容, 现有 routing.py 与 test_gateway.py 不需要改。
  - decide() 返回 Policy 实例; 兜底「default_allow」也是 Policy。
  - 用户在 policy.yaml 写的字段名 / 优先级 / 运算符 typo 立刻抛 ValidationError。
"""
from __future__ import annotations
import os
import re
import yaml
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# 2026-10-19 红队：text 的 contains_any/matches_regex 只跑原文，零宽化「财\u200b务报表」
# 「aws_\u200bsecret」/「sk-12…\u200b…f」全 miss。归一化（NFKC+零宽剥离）与 PII 层同一真源，
# 避免两套正则漂移。inspection.py 仅依赖标准库，此处单向导入不成环。
from .inspection import _normalize_for_pii as _normalize_text

DEFAULT_POLICY = Path(__file__).parent.parent.parent / "policy.yaml"

# ---------------------------------------------------------------------------
# Pydantic schema (P2: 替代裸 dict)
# ---------------------------------------------------------------------------

Action = Literal["allow", "route_local", "block"]


class _DictLike(BaseModel):
    """Base that supports r['action'] dict-style access for back-compat."""

    model_config = ConfigDict(extra="allow")

    def __getitem__(self, key: str) -> Any:  # type: ignore[override]
        if key in self.model_fields_set or key in self.model_fields:
            return getattr(self, key)
        return getattr(self, key, None)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except (KeyError, AttributeError):
            return default


class PolicyTarget(_DictLike):
    provider: str = "openai"
    model: str = "gpt-4o-mini"


# cond 写成 dict: { "text contains_any": [...], "findings.pii_count >=": 60 }
# 自由形式 (key 内嵌运算符), pydantic 校验做不到字面 schema 校验, 只校验 dict[non-empty str, Any]。
class Policy(_DictLike):
    """Single policy rule. Pydantic 强校验, 出错立刻 raise."""

    name: str
    priority: int = 100
    action: Action
    description: str = ""
    when: Union[str, Dict[str, Any]] = "always"
    target: Optional[PolicyTarget] = None
    # 优先级锁定：被锁定的规则不参与策略调节建议的「优先级重排」（规则 4），
    # 其 priority 不会被 suggestion/apply 覆盖。别名字段同步，任一为 true 即锁定。
    priority_locked: bool = False
    locked: bool = False
    locked_priority: bool = False
    pinned: bool = False
    fixed_priority: bool = False

    @model_validator(mode="before")
    @classmethod
    def _sync_priority_locked(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        keys = ("priority_locked", "locked", "locked_priority", "pinned", "fixed_priority")
        val = any(bool(data.get(k)) for k in keys)
        for k in keys:
            data[k] = val
        return data

    @field_validator("when")
    @classmethod
    def _when_not_empty(cls, v: Any) -> Any:
        if v is None or v == "":
            return "always"
        if isinstance(v, Mapping):
            # 嵌套 any/all 壳静默永不命中：_match_when 只剥一层壳，嵌套壳掉进
            # _eval_cond，key "any"/"all" 匹配不到任何算子 → 恒 False，而自由形式
            # dict 的 when 校验不挡它 ⇒「保存成功、干跑不命中、线上零报错」。
            # 单层组（{"any":[单条件...]}）合法；壳里再出现 any/all 即拒（fail-fast）。
            for k in ("any", "all"):
                if k not in v:
                    continue
                items = v[k]
                if not isinstance(items, list):
                    raise ValueError(f"'{k}' 的值必须是条件对象列表，得到: {items!r}")
                for item in items:
                    if not isinstance(item, Mapping):
                        raise ValueError(f"'{k}' 的条件项必须是单条件对象，得到: {item!r}")
                    if set(item) & {"any", "all"}:
                        raise ValueError(
                            f"when 里的 any/all 不支持嵌套（引擎求值不了，规则永不命中）；只用一层 any/all 组")
        return v

    @field_validator("priority")
    @classmethod
    def _priority_range(cls, v: int) -> int:
        if not 0 <= v <= 1000:
            raise ValueError(f"priority must be 0..1000, got {v}")
        return v


class PolicyDoc(BaseModel):
    model_config = ConfigDict(extra="ignore")
    policies: List[Policy] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 加载 / 兜底
# ---------------------------------------------------------------------------

def _default_allow() -> Policy:
    # 规则名与 policy.yaml 的「default_allow」一致：审计里显式规则与隐式兜底同名，聚合不分叉
    return Policy(name="default_allow", priority=100, action="allow", description="implicit fallback")


_POLICY_CACHE: Dict[str, Any] = {"key": None, "policies": None}


def invalidate_policy_cache() -> None:
    """P1: policy 写路径（policy_admin._write_doc）落盘后调用，强制下次重载。"""
    _POLICY_CACHE.update(key=None, policies=None)


def load_policy(path: str | Path | None = None) -> List[Policy]:
    """
    Load + validate policy.yaml. Returns sorted list of Policy.

    - path 缺省时读 AI_GATEWAY_POLICY_PATH（未设则仓库根 policy.yaml）
      —— 管理后台规则 CRUD 与请求链路必须读写同一份文件
    - 文件不存在 → [default_allow()] (保持向后兼容: 早期部署靠这避免 crash)
    - 存在但 schema 错 → raise ValidationError (P2 改 fail-fast)
    - 任何 rules 解析错误 → 抛错, 由调用方决定重试 / 走 fallback
    """
    p = Path(path or os.getenv("AI_GATEWAY_POLICY_PATH", "") or DEFAULT_POLICY)
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        # 缺文件恒回 default_allow（原语义；与 routing 保旧配置不同，策略缺失不沿用旧规则）。
        # 文件重现后 mtime 变化，缓存 key 对不上自动重载，无需在此清缓存。
        return [_default_allow()]
    hit = _POLICY_CACHE["policies"]
    if hit is not None and _POLICY_CACHE["key"] == key:
        return hit
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if isinstance(data, dict) and "policies" in data:
        doc = PolicyDoc.model_validate(data)
    elif isinstance(data, list):
        doc = PolicyDoc.model_validate({"policies": data})
    else:
        raise ValidationError.from_exception_data(
            "policy.yaml", [
                {"type": "missing", "loc": ("policies",), "input": data, "msg": "missing 'policies' key"},
            ]
        )
    result = sorted(doc.policies, key=lambda x: x.priority)
    _POLICY_CACHE.update(key=key, policies=result)
    return result


# ---------------------------------------------------------------------------
# when 表达式求值 (保留旧逻辑, Pydantic 不约束 when 自由形式)
# ---------------------------------------------------------------------------

def _contains_any(haystack: Any, needles: List[str]) -> bool:
    if isinstance(haystack, list):
        haystack = " ".join(str(x) for x in haystack)
    hay = str(haystack).lower()
    return any(str(n).lower() in hay for n in needles)


def _get_field(ctx: Mapping[str, Any], field: str) -> Any:
    cur: Any = ctx
    for p in field.split("."):
        if isinstance(cur, Mapping):
            cur = cur.get(p)
        else:
            return None
    return cur


def _eval_cond(cond: Mapping[str, Any], ctx: Mapping[str, Any]) -> bool:
    """支持 == / != / in / not_in / contains_any / matches_regex / 比较运算符, 字段名嵌入 key."""
    for k, v in cond.items():
        k = k.strip()
        if "!=" in k:
            return _get_field(ctx, k.split("!=")[0].strip()) != v
        if " not_in" in k:
            field = k.split(" not_in")[0].strip()
            val = _get_field(ctx, field)
            items = v if isinstance(v, list) else [x.strip() for x in str(v).split(",") if x.strip()]
            return not any(str(val).strip().lower() == str(x).strip().lower() for x in items)
        if "==" in k:
            return _get_field(ctx, k.split("==")[0].strip()) == v
        if " in" in k:
            field = k.split(" in")[0].strip()
            val = _get_field(ctx, field)
            if isinstance(v, list):
                if field == "file.ext":
                    return str(val).lower() in [str(x).lower() for x in v]
                return any(str(val).lower() == str(x).lower() for x in v) or _contains_any(val, [str(x) for x in v])
            return str(val) in str(v)
        if "contains_any" in k:
            field = k.split("contains_any")[0].strip()
            hay = _get_field(ctx, field)
            if _contains_any(hay, v):
                return True
            if field == "text":
                # 零宽拆分「财\u200b务报表」「aws_\u200bsecret」原文 miss ⇒ 归一化副本再跑
                return _contains_any(_normalize_text(str(hay or "")), v)
            return False
        if "matches_regex" in k:
            field = k.split("matches_regex")[0].strip()
            raw = str(_get_field(ctx, field) or "")
            if re.search(v, raw):
                return True
            if field == "text":
                # 拦截密钥的裸正则跑原文可被零宽化绕过（sk-32hex/aws_secret 无 PII 兜底前）⇒ 归一化副本再跑
                return bool(re.search(v, _normalize_text(raw)))
            return False
        if ">=" in k:
            try: return float(_get_field(ctx, k.split(">=")[0].strip())) >= float(v)
            except (TypeError, ValueError): return False
        if "<=" in k:
            try: return float(_get_field(ctx, k.split("<=")[0].strip())) <= float(v)
            except (TypeError, ValueError): return False
        if ">" in k:
            try: return float(_get_field(ctx, k.split(">")[0].strip())) > float(v)
            except (TypeError, ValueError): return False
        if "<" in k:
            try: return float(_get_field(ctx, k.split("<")[0].strip())) < float(v)
            except (TypeError, ValueError): return False
    return False


def _match_when(when: Any, ctx: Mapping[str, Any]) -> bool:
    if when == "always":
        return True
    if isinstance(when, Mapping):
        if "any" in when:
            return any(_eval_cond(c, ctx) for c in when["any"])
        if "all" in when:
            return all(_eval_cond(c, ctx) for c in when["all"])
        return _eval_cond(when, ctx)
    return False


def decide(policy_list: List[Union[Policy, Mapping[str, Any]]], ctx: Mapping[str, Any]) -> Policy:
    """
    Walk policy_list in priority order, return first matching Policy.
    Fallback: 「default_allow」(priority 100, action allow).

    兼容: 也接受 Mapping (dict) 条目, 让测试用 plist = [{"when":...}, ...] 写直白
    仍能 work. 真实部署路径都走 load_policy() 返 Policy.
    """
    for p in policy_list:
        when = p.get("when", "always") if hasattr(p, "get") else getattr(p, "when", "always")
        if _match_when(when, ctx):
            if isinstance(p, Policy):
                return p
            return Policy.model_validate(dict(p))
    return _default_allow()
