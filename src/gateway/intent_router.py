"""按任务复杂度给别名组选头（复杂度路由）。

口径（改动前先读）：

- 复杂度路由**定死常开**（2026-10-26 起不再提供开关）：只要别名组启用成员
  档位（alias_member.tier）有 ≥2 种就判定；单档位组一次调用都不发（选头无意义）。
- 判定模型**复用 L2 小模型**（small_model 的 SMALL_MODEL_* 配置，内网 192.168.x）。
  不新增 provider 配置：内容不出境 —— 与 L2 判密同一条内网通道；外网模型永远不参与。
- 一次用户发言只判一次：判定输入是「最后一条用户消息」，同一轮里几十个 tool 回合
  反复携带同一段文字 ⇒ 命中 10min 缓存，只判一次。用户换了说法（下一轮）才重判。
- 会话级档位粘性（apply_sticky）：跨 user turn 只升不降 —— 换模型 = 废掉上一模型的
  vendor prompt cache（下轮全量重付）+ 带长上下文的简单追问掉到轻模型的质量断崖。
  窗口 AI_GATEWAY_INTENT_STICKY_MIN 分钟（0=关），相对「上次使用」计时。
- 任何失败/超时/熔断/非法输出 ⇒ 返回 None ⇒ 候选链按原顺序（判不出来不改判决）；
  但若会话粘性档还在窗口内，用会话档顶上（判不出来不该掉档）。
  判定模型自己有自己的熔断键，绝不与 L2 判密共用（判密度挂了不影响路由，反之亦然）。
- 进程内缓存：多 worker 部署会各判各的（各自多花一次 300ms），单 uvicorn 生产无此问题；
  真要扩多 worker 再换 Redis。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from contextvars import ContextVar
from typing import List, Optional

from .alias_router import find_alias

# 请求级判定结果（main._review_text 写、alias_router.alias_candidates 读）。
# 与 alias_router._alias_ctx 分开存：后者在 routing 里 ctx_start 整份重建，会把它冲掉。
_level_ctx: "ContextVar[Optional[dict]]" = ContextVar("intent_level", default=None)

_BREAKER_KEY = "intent_complexity"
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 600
_CACHE_MAX = 500

LEVEL_NAMES = {1: "简单", 2: "中等", 3: "复杂"}

_SYSTEM = """你是任务复杂度判定器。读用户最新一条消息，判断**完成它**需要多强的模型，只输出一个数字：
1 = 简单：闲聊、问候、名词/事实问答、概念解释（"为什么 X"）、翻译一小段，不需要读代码或改代码
2 = 中等：写或改代码、加或修测试、解释一段具体代码、修一个明确的 bug、单文件小改动、写个小脚本
3 = 复杂：跨多个文件的改动、架构或方案设计、根因不明的故障排查、需要大量上下文才能判断的任务

判断标准是完成它的工作量（要读多少、改多少、推多深），不是你觉得难不难。拿不准时选 2。
只输出数字 1 或 2 或 3，不要输出任何其他内容。

例子：
- 「你好」「解释一下什么是 prompt caching」「为什么 TCP 握手要三次」→ 1
- 「给 cache.go 的 LRU 补单元测试」「修一下这个空指针」「写个脚本合并 CSV」→ 2
- 「高峰期偶发 502，分析根因并给跨模块重构方案」「把这三个文件的鉴权逻辑统一重写」→ 3"""


def _timeout() -> float:
    try:
        return max(0.2, float(os.getenv("AI_GATEWAY_INTENT_TIMEOUT", "3")))
    except (TypeError, ValueError):
        return 3.0


def _tier(cand: dict) -> int:
    try:
        t = int(cand.get("tier") or 2)
    except (TypeError, ValueError):
        t = 2
    return t if t in (1, 2, 3) else 2


def ctx_level() -> Optional[int]:
    return (_level_ctx.get() or {}).get("level")


def ctx_set(info: Optional[dict]) -> None:
    _level_ctx.set(info or None)


def ctx_info() -> Optional[dict]:
    return _level_ctx.get()


def _sticky_window() -> int:
    """档位粘性窗口（秒）：AI_GATEWAY_INTENT_STICKY_MIN 分钟，0=关（每次请求读，即时生效）。"""
    try:
        return max(0, int(float(os.getenv("AI_GATEWAY_INTENT_STICKY_MIN", "30")))) * 60
    except (TypeError, ValueError):
        return 30 * 60


async def apply_sticky(session_id, info):
    """会话级档位粘性（借鉴 magpie 的 Stays）：跨 user turn 只升不降。

    换模型 = 废掉上一模型的 vendor prompt cache（下轮全量重付）+ 质量断崖
    （带长上下文的"修 typo"掉到轻模型）。所以会话窗口内：
    有效档位 = max(本次判定, 会话历史最高档)；判定失败时直接用会话档
    （判不出来不该掉档）。窗口过后（或 0=关）回到逐条自由判定。

    info = _decide 的返回（{"level","ms","cached"} 或 None）；返回写 ctx 的最终 dict。
    测试可 monkeypatch get_session_store 注入假 store。

    async（2026-10-27）：session store 同步 Redis 挪出事件循环
    （main._review_text 调用点 await）。
    """
    from starlette.concurrency import run_in_threadpool
    from .session_store import get_session_store
    window = _sticky_window()
    prev = await run_in_threadpool(get_session_store().get_sticky_tier, session_id, window) if window else None
    level = (info or {}).get("level")
    eff = level
    sticky = False
    if prev and prev[0] >= 2:
        eff = max(level, prev[0]) if level else prev[0]
        sticky = info is None or prev[0] >= level
    if eff in (2, 3):
        # 档 1 不记（等于没约束）；时间戳随每次使用刷新 = 活跃会话档位不释放
        await run_in_threadpool(get_session_store().mark_sticky_tier, session_id, eff)
    if info is None:
        return {"level": eff, "ms": 0, "cached": True, "sticky": True} if (eff and sticky) else None
    out = dict(info)
    if sticky:
        out["level"] = eff
        out["sticky"] = True
    return out


def _source(text: str) -> str:
    """判定输入：太长就留头 1500 + 尾 1000（用户真正的诉求多半在头，结论/报错常在尾）。"""
    s = (text or "").strip()
    if not s:
        return ""
    if len(s) <= 2500:
        return s
    return s[:1500] + "\n…\n" + s[-1000:]


def _parse(content: str) -> Optional[int]:
    """取回复里第一个 1/2/3；没有（模型跑题/只吐思考）即 None。"""
    m = re.search(r"[123]", content or "")
    return int(m.group(0)) if m else None


def select(cands: List[dict], level: Optional[int]) -> List[dict]:
    """按 level 把「够用且最省」的候选提到链首，其余保持优先级顺序做故障切换。

    level=None / 单档位组 ⇒ 原样返回（等价旧行为）。
    """
    if not level or len(cands) < 2:
        return cands
    if len({_tier(c) for c in cands}) < 2:
        return cands
    enough = [i for i, c in enumerate(cands) if _tier(c) >= level]
    if enough:
        # 够强里挑最省的；同档按原优先级（i 升序）
        head = min(enough, key=lambda i: (_tier(cands[i]), i))
    else:
        # 谁都不够强（配的是「轻/中」两档却判成复杂）⇒ 退最强的那一档
        head = min(range(len(cands)), key=lambda i: (-_tier(cands[i]), i))
    return [cands[head]] + [c for i, c in enumerate(cands) if i != head]


def plan(model: str, text: str):
    """请求级入口：该判就返回一个 Task（可与 L2 并行），否则返回 None。"""
    try:
        group = find_alias(model)
        if not group:
            return None
        cands = group.get("candidates") or []
        if len({_tier(c) for c in cands}) < 2:
            return None
        src = _source(text)
        if not src:
            return None
        return asyncio.ensure_future(_decide(src))
    except Exception:
        return None


async def _decide(src: str) -> Optional[dict]:
    """判一次复杂度。返回 {"level", "ms", "cached"} 或 None（None = 不做决定）。"""
    key = hashlib.sha256(src.encode("utf-8", "ignore")).hexdigest()
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        return {**hit[1], "cached": True}

    from .circuit_breaker import get_breaker
    breaker = get_breaker()
    if not breaker.allow(_BREAKER_KEY):
        return None

    from . import small_model as sm
    if not sm.l2_enabled():
        return None
    t0 = time.time()
    try:
        resp = await sm._get_client().post(
            sm.l2_url(),
            json={
                "model": sm.l2_name(),
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    # 客户端会把 user_info/cwd/当前时间塞进正文，剥掉只留真问题
                    {"role": "user", "content": _slim(src)},
                ],
                "temperature": 0,
                "max_tokens": 24,
            },
            timeout=_timeout(),
        )
        resp.raise_for_status()
        content = ((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except Exception:
        breaker.record_failure(_BREAKER_KEY)
        return None

    level = _parse(content)
    if level is None:
        # 模型答了但不是数字：算一次失败（熔断阈值内不会再拖慢下一轮）
        breaker.record_failure(_BREAKER_KEY)
        return None
    breaker.record_success(_BREAKER_KEY)

    out = {"level": level, "ms": int((time.time() - t0) * 1000), "cached": False}
    _CACHE[key] = (now, {k: v for k, v in out.items() if k != "cached"})
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))
    return out


def _slim(s: str) -> str:
    """剥 IDE 客户端塞进 user 正文的样板（system-reminder / environment_context）。"""
    try:
        from .main import _slim_client_envelope
        return _slim_client_envelope(s) or s
    except Exception:
        return s
