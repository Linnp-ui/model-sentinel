"""
Small Model Layer — 第二层语义判定
src/gateway/small_model.py:1
对 L1 未命中的模糊内容做二次分类。双后端（AI_GATEWAY_L2_BACKEND）：
- qwen（默认）：本地 Qwen3-4B-Instruct-2507 (10.0.0.10:8002, OpenAI chat 协议)
- laya：Laya multilingual 非自回归决策模型 (127.0.0.1:8003 /classify, noul 英文题规格)
影子双跑（AI_GATEWAY_L2_SHADOW=true）：主后端权威，laya 同流量并行打标 shadow_*。
"""
from __future__ import annotations
import asyncio
import os
import re
import json
import time
import httpx
from typing import Dict, Any

CONFIDENTIAL_THRESHOLD = 0.7
# 紧凑解码：输出只是小 JSON，48 tokens 绰绰有余（实测 32）；guided_json 定型防坏 JSON
L2_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": ["CONFIDENTIAL", "NORMAL"]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["label", "confidence", "reason"],
}

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, ""))
    except (TypeError, ValueError):
        return default


def l2_enabled() -> bool:
    return (os.getenv("SMALL_MODEL_ENABLED", "true") or "").strip().lower() == "true"


def l2_url() -> str:
    return os.getenv("SMALL_MODEL_URL", "http://10.0.0.10:8002/v1/chat/completions")


def l2_name() -> str:
    return os.getenv("SMALL_MODEL_NAME", "Qwen3-4B-Instruct-2507")


def l2_timeout() -> float:
    return _env_float("SMALL_MODEL_TIMEOUT", 5.0)


def l2_max_tokens() -> int:
    try:
        return int(os.getenv("SMALL_MODEL_MAX_TOKENS", "48"))
    except (TypeError, ValueError):
        return 48


def l2_backend() -> str:
    """qwen = OpenAI chat 协议（vLLM 8002）；laya = /classify JSON（决策模型 8003）。

    laya 切换前提：影子期数据证明质量达标（AI_GATEWAY_L2_SHADOW）。
    """
    return (os.getenv("AI_GATEWAY_L2_BACKEND", "qwen") or "qwen").strip().lower()


def _shadow_url() -> str:
    """影子期 laya 端点；未配置 = 影子关闭（避免误连）。"""
    return (os.getenv("AI_GATEWAY_L2_SHADOW_URL") or "").strip()


def _shadow_enabled() -> bool:
    """影子期：主后端判定继续权威，laya 同流量并行打标（shadow_* 进审计 l2 字段）。"""
    return ((os.getenv("AI_GATEWAY_L2_SHADOW") or "").strip().lower() == "true"
            and bool(_shadow_url()) and l2_backend() != "laya")

# P0-2：L2 "判定不可用" 的归因码。判定不可用 != 判定为 NORMAL —— 调用方必须分开处理，
# 默认降级到本地模型（route_local），不得当作放行出境。
DEGRADED_CIRCUIT = "circuit_open"
DEGRADED_NO_JSON = "no_json"
DEGRADED_BAD_LABEL = "bad_label"
DEGRADED_EXC = "exception"
_L2_BREAKER_KEY = "small_model"   # 复用 CircuitBreaker，面板 circuit 区可见

_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 600
# 连接池按事件循环隔离：httpx 连接绑定创建它的 loop，
# 复用已关闭 loop 上的连接会抛 "Event loop is closed"（TestClient 每个请求一个新 loop）
_CLIENTS: dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}


def _degraded(reason: str, latency_ms: int, **extra) -> Dict[str, Any]:
    """L2 没产出可信判定。confidence 记 0.0：它与「模型说 0.5 置信度的 NORMAL」必须可区分。

    这是唯一的降级出口 —— 指标也在这里记，避免各 return 点漏记。
    """
    try:
        from .metrics import get_metrics
        get_metrics().inc_l2_degraded()
    except Exception:
        pass  # 遥测尽力而为，绝不因为它影响判定
    return {"label": "NORMAL", "confidence": 0.0,
            "reason": f"l2 degraded: {reason}",
            "latency_ms": latency_ms, "fallback": True,
            "degraded": True, "degraded_reason": reason, **extra}


def is_degraded(result: Dict[str, Any]) -> bool:
    """L2 判定是否不可信（没产出结论，而非「结论是正常」）。

    fail-closed 消费点靠它决定是否 route_local，见 main._l2_decision。
    """
    return bool((result or {}).get("degraded"))


def _get_client() -> httpx.AsyncClient:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None  # type: ignore[assignment]

    for l, c in list(_CLIENTS.items()):
        if l.is_closed():
            _CLIENTS.pop(l, None)

    client = _CLIENTS.get(loop) if loop is not None else None
    if client is None or client.is_closed:
        # 小模型在内网，必须绕过系统代理，否则请求会被公司代理转发/拖慢
        client = httpx.AsyncClient(timeout=l2_timeout(), trust_env=False,
                                   limits=httpx.Limits(max_keepalive_connections=10))
        if loop is not None:
            _CLIENTS[loop] = client
    return client

def _cache_key(text: str, filename: str, headers: list, sheet_names: list) -> str:
    import hashlib
    h = hashlib.md5(f"{text[:800]}|{filename}|{','.join(headers or [])}|{','.join(sheet_names or [])}".encode()).hexdigest()
    return h

SYSTEM_PROMPT = os.getenv("AI_GATEWAY_L2_SYSTEM_PROMPT") or """你是企业机密分类器。判断用户内容是否属于机密，必须输出严格JSON。

机密(CONFIDENTIAL)包括（任一满足即判密）：
- 设计图纸、施工图、结构图、CAD/BIM/图号/技术方案/工艺参数
- 财务数据：工资/薪资/奖金/成本/营收/利润/预算/决算/资产负债/报价/单价/总价/税率
- 个人敏感：身份证/手机号/银行卡/住址/人事档案/银行流水
- 商业秘密：合同/投标/报价单/内部资料/机密/保密/不得外传/内部会议纪要
示例：含"姓名+工资/金额"表格即机密；含"图号/施工图"即机密。

非机密(NORMAL)：通用知识、公开文档、日常对话、无敏感字段的普通表格。

只输出JSON: {"label":"CONFIDENTIAL"|"NORMAL","confidence":0.0-1.0,"reason":"一句话原因（十个字内）"}
不要输出其他内容。"""

USER_PROMPT_TEMPLATE = os.getenv("AI_GATEWAY_L2_USER_PROMPT_TEMPLATE") or """\
文件名: {filename}
Sheet: {sheet_names}
表头: {headers}

内容预览:
{preview}"""

_PROMPT_OVERRIDES: dict = {}


def get_system_prompt() -> str:
    """L2 system prompt：启动时持久化覆盖 > 运行时内存覆盖 > env > 内置默认。"""
    return _PROMPT_OVERRIDES.get("AI_GATEWAY_L2_SYSTEM_PROMPT") or os.getenv("AI_GATEWAY_L2_SYSTEM_PROMPT") or SYSTEM_PROMPT


def get_user_prompt_template() -> str:
    """L2 user prompt 模板：占位符 {filename} {sheet_names} {headers} {preview}。"""
    return _PROMPT_OVERRIDES.get("AI_GATEWAY_L2_USER_PROMPT_TEMPLATE") or os.getenv("AI_GATEWAY_L2_USER_PROMPT_TEMPLATE") or USER_PROMPT_TEMPLATE


def set_prompt_overrides(system: str | None = None, template: str | None = None) -> dict:
    """内存级即时覆盖（admin PUT 调用）。空串表示还原默认。"""
    if system is not None:
        if system == "":
            _PROMPT_OVERRIDES.pop("AI_GATEWAY_L2_SYSTEM_PROMPT", None)
            os.environ.pop("AI_GATEWAY_L2_SYSTEM_PROMPT", None)
        else:
            _PROMPT_OVERRIDES["AI_GATEWAY_L2_SYSTEM_PROMPT"] = system
            os.environ["AI_GATEWAY_L2_SYSTEM_PROMPT"] = system
    if template is not None:
        if template == "":
            _PROMPT_OVERRIDES.pop("AI_GATEWAY_L2_USER_PROMPT_TEMPLATE", None)
            os.environ.pop("AI_GATEWAY_L2_USER_PROMPT_TEMPLATE", None)
        else:
            _PROMPT_OVERRIDES["AI_GATEWAY_L2_USER_PROMPT_TEMPLATE"] = template
            os.environ["AI_GATEWAY_L2_USER_PROMPT_TEMPLATE"] = template
    return {k: v for k, v in _PROMPT_OVERRIDES.items()}


def _build_user_prompt(text: str, filename: str = "", headers: list = None, sheet_names: list = None) -> str:
    headers = headers or []
    sheet_names = sheet_names or []
    try:
        return get_user_prompt_template().format(
            filename=filename or "",
            sheet_names=", ".join(sheet_names),
            headers=", ".join(headers),
            preview=(text[:1200] if text else ""),
        )
    except (KeyError, IndexError):
        # 模板占位符被改坏：回退内置模板
        return USER_PROMPT_TEMPLATE.format(
            filename=filename or "", sheet_names=", ".join(sheet_names),
            headers=", ".join(headers), preview=(text[:1200] if text else ""),
        )

async def classify(text: str, filename: str = "", headers: list = None, sheet_names: list = None) -> Dict[str, Any]:
    if not l2_enabled():
        return {"label": "NORMAL", "confidence": 0.5, "reason": "small_model disabled", "latency_ms": 0,
                "fallback": True, "disabled": True, "degraded": False}
    if not text and not filename:
        return {"label": "NORMAL", "confidence": 0.5, "reason": "empty input", "latency_ms": 0,
                "fallback": True, "degraded": False}
    from .circuit_breaker import get_breaker
    _cb = get_breaker()
    key = _cache_key(text, filename, headers, sheet_names)
    now = time.time()
    if key in _CACHE:
        ts, val = _CACHE[key]
        if now - ts < _CACHE_TTL:
            return {**val, "cached": True, "latency_ms": 0}
        else:
            del _CACHE[key]
    if not _cb.allow(_L2_BREAKER_KEY):
        # 连续失败已熔断：不再等超时，直接判定不可用（结果由调用方 fail-local）
        return _degraded(DEGRADED_CIRCUIT, 0)
    start = time.time()
    try:
        if l2_backend() == "laya":
            res = await _call_laya(text, filename, headers, sheet_names)
        else:
            res = await _call_qwen(text, filename, headers, sheet_names)
    except Exception as e:
        _cb.record_failure(_L2_BREAKER_KEY)
        res = _degraded(DEGRADED_EXC, int((time.time() - start) * 1000), error=str(e))
    if _shadow_enabled():
        # 影子失败只丢影子数据，绝不影响主判定（上方已定型）
        await _shadow_run(res, text, filename, headers, sheet_names)
    if not res.get("degraded"):
        _cb.record_success(_L2_BREAKER_KEY)
        _CACHE[key] = (now, res)
        if len(_CACHE) > 500:
            _CACHE.pop(next(iter(_CACHE)))
    return res


async def _call_qwen(text: str, filename: str, headers: list, sheet_names: list) -> Dict[str, Any]:
    """qwen 后端（vLLM OpenAI chat 协议）。异常直接抛，熔断记账由 classify() 统一做。"""
    client = _get_client()
    start = time.time()
    resp = await client.post(l2_url(), json={
        "model": l2_name(),
        "messages": [
            {"role": "system", "content": get_system_prompt()},
            {"role": "user", "content": _build_user_prompt(text, filename, headers, sheet_names)},
        ],
        "temperature": 0.1,
        "max_tokens": l2_max_tokens(),
        "extra_body": {"guided_json": L2_RESPONSE_SCHEMA},
    })
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"] or ""
    latency = int((time.time() - start) * 1000)
    m = re.search(r"\{[^}]+\}", content, re.S)
    if not m:
        return _degraded(DEGRADED_NO_JSON, latency, raw=content[:200])
    obj = json.loads(m.group(0))
    label = obj.get("label", "NORMAL")
    if label not in ("CONFIDENTIAL", "NORMAL"):
        # 模型输出不可信就不该被采信（不静默改成 NORMAL）
        return _degraded(DEGRADED_BAD_LABEL, latency, raw=content[:200])
    return {"label": label, "confidence": float(obj.get("confidence", 0.5)),
            "reason": obj.get("reason", ""), "latency_ms": latency, "raw": content}


async def _call_laya(text: str, filename: str, headers: list, sheet_names: list,
                     timeout: float | None = None) -> Dict[str, Any]:
    """laya 后端（/classify JSON，非自回归决策模型）。主备一律走
    AI_GATEWAY_L2_SHADOW_URL（SMALL_MODEL_URL 是 OpenAI chat 口，打 classify 包过去
    必 400；2026-09-23 修，否则 BACKEND=laya 当天全灰进 l2_unavailable）。
    timeout=None 用 l2_timeout()，非 None = 影子短超时。URL 为空直接 degraded。"""
    client = _get_client()
    url = _shadow_url()
    if not url:
        return _degraded(DEGRADED_EXC, 0, error="AI_GATEWAY_L2_SHADOW_URL 为空")
    start = time.time()
    try:
        resp = await client.post(url, json={
            "text": (text or "")[:1200],
            "filename": filename or "",
            "headers": list(headers or []),
            "sheet_names": list(sheet_names or []),
        }, timeout=timeout if timeout is not None else l2_timeout())
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return _degraded(DEGRADED_EXC, int((time.time() - start) * 1000), error=str(e))
    latency = int((time.time() - start) * 1000)
    label = data.get("label")
    if label not in ("CONFIDENTIAL", "NORMAL"):
        return _degraded(DEGRADED_BAD_LABEL, latency, raw=str(data)[:200])
    return {"label": label, "confidence": float(data.get("confidence", 0.0)),
            "reason": data.get("reason", ""), "latency_ms": latency,
            "probabilities": data.get("probabilities"),
            "raw": f"laya {label} p={data.get('confidence')}"}


async def _shadow_run(res: Dict[str, Any], text: str, filename: str, headers: list, sheet_names: list) -> None:
    """影子期：同一段文本问 laya，结果写 shadow_* 键（审计 l2 字段整体 JSON 落库，自动留痕）。

    2s 短超时：laya 服务挂了只让影子数据缺失，不拖主链路。
    """
    try:
        sh = await _call_laya(text, filename, headers, sheet_names, timeout=2.0)
        if sh.get("degraded"):
            res["shadow_degraded"] = sh.get("error") or sh.get("degraded_reason") or "error"
        else:
            res["shadow_label"] = sh.get("label")
            res["shadow_conf"] = sh.get("confidence")
            res["shadow_probabilities"] = sh.get("probabilities")
            res["shadow_reason"] = sh.get("reason")
    except Exception as e:
        res["shadow_degraded"] = str(e)[:120]

def is_confidential(result: Dict[str, Any]) -> bool:
    """1.1: 阈值运行时可调 —— model_policy.yaml review_model.threshold（mtime 热重载）。"""
    try:
        from .model_policy import review_threshold
        th = review_threshold()
    except Exception:
        th = CONFIDENTIAL_THRESHOLD
    return result.get("label") == "CONFIDENTIAL" and float(result.get("confidence", 0)) >= th

_SHADOW_KEYS = ("shadow_label", "shadow_conf", "shadow_probabilities",
                 "shadow_reason", "shadow_degraded")


def _carry_shadow(out: dict, src: dict) -> dict:
    """多块聚合把影子键从决策块带出来（不带=审计 l2 无影子，覆盖率被低估）。"""
    for k in _SHADOW_KEYS:
        if k in src:
            out[k] = src[k]
    return out


async def classify_chunks(chunks: list, filename: str = "", headers: list = None, sheet_names: list = None,
                          scopes: list = None) -> Dict[str, Any]:
    """P2：`scopes` 是**纯增量**参数（块与 scope 名按位置一一对应）。

    `scopes=None`（或空）时返回**逐字节等于旧实现** —— 默认档 `scopes=["last_user"]`
    走单块路径，根本到不了这里；这里只服务多块（含影子期 probe）。
    新键只在给了 scopes 时出现，既有调用方与断言不受影响。
    """
    if not chunks:
        return await classify("", filename, headers, sheet_names)
    import asyncio
    tasks = [classify(ch, filename, headers, sheet_names) for ch in chunks[:5]]
    results = await asyncio.gather(*tasks)
    total_lat = max([r.get("latency_ms", 0) for r in results]) if results else 0
    # 记账收口：`cached` 必须传染到聚合结果。分块走 `classify()` 命中进程内缓存时
    # 会带 `cached=True, latency_ms=0`，而本聚合此前丢掉了该键 ⇒ 多块全缓存命中的
    # 请求在 `gateway_l2_cached_total` 与 `gateway_l2_latency_ms` 里双双隐身
    # （生产实测：同文本双块重发 `Δcached=+0 / Δl2_count=+0` ⇒ 整发不可见）。
    # 取 all() 而非 any()：只要有一块真打了上游，就有真实延迟进直方图，
    # 此时不能同时算作「缓存命中」，否则两个读数会互相污染。
    _all_cached = bool(results) and all(bool(r.get("cached")) for r in results)
    for idx, r in enumerate(results):
        if is_confidential(r):
            out = {"label": "CONFIDENTIAL", "confidence": r["confidence"], "reason": r["reason"], "latency_ms": total_lat, "chunks": len(results), "hit_index": idx, "raw": r.get("raw")}
            _carry_shadow(out, r)  # 影子跟命中块走（主影对同一段文本才可比）
            if scopes:
                # 命中块 = scope 名（P2 留痕：让「被哪一类内容拦下」可查）
                out["scope"] = scopes[idx] if idx < len(scopes) else None
                out["cached"] = _all_cached
            return out
    # 判密分支已在上面命中；走到这里说明没有分块判密 —— 若任一分块「没产出结论」，
    # 整体就不能算「已确认正常」（否则一块超时会被其余 NORMAL 分块淹没）。
    _bad = [r for r in results if is_degraded(r)]
    if _bad:
        out = _degraded(_bad[0].get("degraded_reason") or DEGRADED_EXC, total_lat,
                        chunks=len(results), degraded_chunks=len(_bad))
        _carry_shadow(out, next((r for r in results if r.get("shadow_label")), _bad[0]))
        if scopes:
            # 哪几个 scope 没产出结论 —— 「降级了但不知道是谁超时」是没法定位的
            out["degraded_scopes"] = [scopes[i] for i, r in enumerate(results)
                                      if is_degraded(r) and i < len(scopes)]
            out["cached"] = _all_cached
        return out
    best = max(results, key=lambda x: float(x.get("confidence", 0))) if results else {"label":"NORMAL","confidence":0.5}
    out = {"label": "NORMAL", "confidence": best.get("confidence", 0.5), "reason": best.get("reason",""), "latency_ms": total_lat, "chunks": len(results)}
    _carry_shadow(out, best)
    if scopes:
        out["scopes_run"] = list(scopes[:len(results)])
        out["cached"] = _all_cached
    return out
