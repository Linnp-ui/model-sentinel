"""复杂度路由（intent_router）单测：选头规则、判不出来不改顺序、缓存只判一次。

跑：.venv/bin/python -m pytest tests/test_intent_router.py -q
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from src.gateway import alias_router as ar
from src.gateway import intent_router as ir
from src.gateway import small_model as sm
from src.gateway.main import app


def _cands(*tiers):
    return [{"provider": f"pytest-ir-p{i}", "model": f"m{i}", "idx": i, "tier": t}
            for i, t in enumerate(tiers)]


def _models(cands):
    return [c["model"] for c in cands]


# ---------- 选头规则 ----------

def test_select_off_keeps_order():
    c = _cands(1, 3)
    assert ir.select(c, None) == c


def test_select_single_tier_keeps_order():
    """只有一个档位 = 没有可挑的，顺序必须与旧版逐字相同。"""
    c = _cands(2, 2)
    assert ir.select(c, 3) == c


def test_select_cheapest_enough_wins():
    c = _cands(1, 3)
    assert _models(ir.select(c, 1)) == ["m0", "m1"]   # 简单 → 轻量
    assert _models(ir.select(c, 2)) == ["m1", "m0"]   # 没有中档 → 上到够用的重档
    assert _models(ir.select(c, 3)) == ["m1", "m0"]   # 复杂 → 重档


def test_select_falls_back_to_strongest():
    """只配了轻/中却判成复杂：退最强的，不能退最弱的。"""
    c = _cands(1, 2)
    assert _models(ir.select(c, 3)) == ["m1", "m0"]


def test_select_keeps_priority_within_tier():
    c = _cands(3, 3, 1)
    assert _models(ir.select(c, 3)) == ["m0", "m1", "m2"]


def test_parse_level():
    assert ir._parse("2") == 2
    assert ir._parse("答案：3") == 3
    assert ir._parse("no idea") is None
    assert ir._parse("") is None


# ---------- 与候选链合流 ----------

def test_alias_candidates_reorders_by_level():
    group = {"name": "pytest-ir-g", "intent": True, "candidates": [
        {"provider": "pytest-ir-a", "model": "light", "tier": 1},
        {"provider": "pytest-ir-b", "model": "heavy", "tier": 3},
    ]}
    try:
        ir.ctx_set({"level": 3})
        assert _models(ar.alias_candidates(group)) == ["heavy", "light"]
        ir.ctx_set({"level": 1})
        assert _models(ar.alias_candidates(group)) == ["light", "heavy"]
    finally:
        ir.ctx_set(None)
    assert _models(ar.alias_candidates(group)) == ["light", "heavy"], "判定关掉后回旧顺序"


def test_alias_candidates_skips_tried_candidate():
    """route_chat 的故障切换靠 ctx.failover 推进：选头换位也不会回头重试。"""
    group = {"name": "pytest-ir-h", "intent": True, "candidates": [
        {"provider": "pytest-ir-c", "model": "light", "tier": 1},
        {"provider": "pytest-ir-d", "model": "heavy", "tier": 3},
    ]}
    try:
        ir.ctx_set({"level": 3})
        ar.ctx_start("pytest-ir-h")
        ar.ctx_record_failover("pytest-ir-d/heavy")
        assert _models(ar.alias_candidates(group)) == ["light"]
    finally:
        ar.ctx_start("reset")
        ir.ctx_set(None)


# ---------- 判定调用 ----------

def test_plan_ignores_intent_flag(monkeypatch):
    """复杂度路由定死常开（2026-10-26）：库里残留的 intent=False 不再跳过判定。"""
    monkeypatch.setattr(ir, "find_alias", lambda m: {
        "name": "g", "intent": False,
        "candidates": [{"provider": "a", "model": "x", "tier": 1},
                       {"provider": "b", "model": "y", "tier": 3}]})

    async def _run():
        t = ir.plan("g", "hello")
        assert t is not None, "intent 标志必须被忽略（常开）"
        t.cancel()

    asyncio.run(_run())


def test_plan_skips_single_tier(monkeypatch):
    monkeypatch.setattr(ir, "find_alias", lambda m: {
        "name": "g", "intent": True,
        "candidates": [{"provider": "a", "model": "x", "tier": 2},
                       {"provider": "b", "model": "y", "tier": 2}]})
    assert ir.plan("g", "hello") is None


class _FakeResp:
    status_code = 200

    def __init__(self, content):
        self._content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeClient:
    def __init__(self, content):
        self.calls = 0
        self._content = content

    async def post(self, *a, **k):
        self.calls += 1
        return _FakeResp(self._content)


def _fake_sm(monkeypatch, client):
    monkeypatch.setattr(sm, "l2_enabled", lambda: True)
    monkeypatch.setattr(sm, "_get_client", lambda: client)


def test_decide_caches_per_message(monkeypatch):
    """同一轮几十个 tool 回合带同一段用户消息 ⇒ 只判一次。"""
    fake = _FakeClient("2")
    _fake_sm(monkeypatch, fake)
    ir._CACHE.clear()
    a = asyncio.run(ir._decide("pytest-ir 同一段用户消息"))
    b = asyncio.run(ir._decide("pytest-ir 同一段用户消息"))
    assert a["level"] == 2 and not a.get("cached")
    assert b["cached"] is True
    assert fake.calls == 1


def test_decide_failure_is_silent(monkeypatch):
    class _Boom:
        async def post(self, *a, **k):
            raise RuntimeError("classifier down")

    _fake_sm(monkeypatch, _Boom())
    ir._CACHE.clear()
    assert asyncio.run(ir._decide("pytest-ir 判定模型挂了")) is None


# ---------- 管理面校验 ----------

client = TestClient(app)


def test_alias_save_same_tier_no_longer_422():
    """定死常开后「同档位 422」校验已删：单档位组照存（选头自动退化为原顺序）。"""
    body = {"intent": True, "members": [
        {"provider": "deepseek", "model": "deepseek-flash", "priority": 10, "tier": 2},
        {"provider": "deepseek", "model": "deepseek-v4-pro", "priority": 20, "tier": 2},
    ]}
    r = client.put("/admin/api/aliases/pytest-ir-intent", json=body)
    assert r.status_code == 200, r.text
    client.delete("/admin/api/aliases/pytest-ir-intent")


def test_alias_intent_saved_and_exposed():
    store_groups = client.get("/admin/api/aliases").json()["groups"]
    names = [g["name"] for g in store_groups]
    if "pytest-ir-intent2" in names:
        client.delete("/admin/api/aliases/pytest-ir-intent2")
    try:
        body = {"intent": True, "members": [
            {"provider": "deepseek", "model": "deepseek-flash", "priority": 10, "tier": 1},
            {"provider": "deepseek", "model": "deepseek-v4-pro", "priority": 20, "tier": 3},
        ]}
        assert client.put("/admin/api/aliases/pytest-ir-intent2", json=body).status_code == 200
        g = [x for x in client.get("/admin/api/aliases").json()["groups"]
             if x["name"] == "pytest-ir-intent2"][0]
        assert g["intent"] in (1, True)
        assert [m["tier"] for m in g["members"]] == [1, 3]
    finally:
        client.delete("/admin/api/aliases/pytest-ir-intent2")


# ---------- 会话级档位粘性（apply_sticky） ----------

class _FakeStickyStore:
    def __init__(self, prev=None):
        self.prev = prev  # (tier, ts) 或 None
        self.marked = []

    def get_sticky_tier(self, sid, window_s):
        if sid and self.prev and window_s > 0:   # 真 store 对 None sid 返回 None
            return self.prev
        return None

    def mark_sticky_tier(self, sid, tier):
        self.marked.append((sid, tier))


def _patch_store(monkeypatch, store):
    # apply_sticky 内是函数级 import ⇒ 要打在源模块上才生效
    from src.gateway import session_store as sst
    monkeypatch.setattr(sst, "get_session_store", lambda: store)


def test_sticky_raises_downgraded_turn(monkeypatch):
    """会话里用过档 3 后，下一轮判成 1 ⇒ 有效档位仍是 3（只升不降）。"""
    monkeypatch.setenv("AI_GATEWAY_INTENT_STICKY_MIN", "30")
    st = _FakeStickyStore(prev=(3, 123))
    _patch_store(monkeypatch, st)
    out = asyncio.run(ir.apply_sticky("pytest-ir-sess-0001", {"level": 1, "ms": 40, "cached": False}))
    assert out["level"] == 3 and out["sticky"] is True
    # 时间戳随使用刷新（活跃会话档位不释放）
    assert st.marked == [("pytest-ir-sess-0001", 3)]


def test_sticky_applies_on_classifier_failure(monkeypatch):
    """判定失败（info=None）⇒ 用会话档顶上，而不是掉回顺序路由。"""
    monkeypatch.setenv("AI_GATEWAY_INTENT_STICKY_MIN", "30")
    st = _FakeStickyStore(prev=(2, 123))
    _patch_store(monkeypatch, st)
    out = asyncio.run(ir.apply_sticky("pytest-ir-sess-0002", None))
    assert out["level"] == 2 and out["sticky"] is True and out["cached"] is True
    assert st.marked == [("pytest-ir-sess-0002", 2)]


def test_sticky_keeps_own_higher_level(monkeypatch):
    """本次判 3、会话档 2 ⇒ 用 3，且不带 sticky 标（粘性没起作用）。"""
    monkeypatch.setenv("AI_GATEWAY_INTENT_STICKY_MIN", "30")
    st = _FakeStickyStore(prev=(2, 123))
    _patch_store(monkeypatch, st)
    out = asyncio.run(ir.apply_sticky("pytest-ir-sess-0003", {"level": 3, "ms": 50, "cached": False}))
    assert out["level"] == 3 and out.get("sticky") is not True
    assert st.marked == [("pytest-ir-sess-0003", 3)]


def test_sticky_off_is_noop(monkeypatch):
    """窗口 0=关 ⇒ 与旧行为逐字相同：不读不写，level 原样。"""
    monkeypatch.setenv("AI_GATEWAY_INTENT_STICKY_MIN", "0")
    st = _FakeStickyStore(prev=(3, 123))
    _patch_store(monkeypatch, st)
    out = asyncio.run(ir.apply_sticky("pytest-ir-sess-0004", {"level": 1, "ms": 40, "cached": False}))
    assert out == {"level": 1, "ms": 40, "cached": False}
    assert st.marked == []


def test_sticky_no_session_no_sticky(monkeypatch):
    """无会话 id（没头也没指纹）⇒ 不粘，行为与旧版相同。"""
    monkeypatch.setenv("AI_GATEWAY_INTENT_STICKY_MIN", "30")
    st = _FakeStickyStore(prev=(3, 123))
    _patch_store(monkeypatch, st)
    out = asyncio.run(ir.apply_sticky(None, {"level": 1, "ms": 40, "cached": False}))
    assert out["level"] == 1 and out.get("sticky") is not True
    assert st.marked == []


def test_sticky_store_window_memory_backend(monkeypatch):
    """真 store（测试里 memory 后端）：mark 只升不降；get 按调用方给的窗口判过期。"""
    from src.gateway import session_store as sst
    st = sst.get_session_store()
    sid = "pytest-ir-sticky-0001"
    st.mark_sticky_tier(sid, 2)
    assert st.get_sticky_tier(sid, 60)[0] == 2
    st.mark_sticky_tier(sid, 1)    # 降级无效
    assert st.get_sticky_tier(sid, 60)[0] == 2
    st.mark_sticky_tier(sid, 3)    # 升级
    assert st.get_sticky_tier(sid, 60)[0] == 3

    class _T:
        now = 1_000_000.0
        def time(self):
            return self.now
    t = _T()
    monkeypatch.setattr(sst, "time", t)
    st.mark_sticky_tier("pytest-ir-sticky-0002", 3)
    t.now += 30
    assert st.get_sticky_tier("pytest-ir-sticky-0002", 60) == (3, 1_000_000)
    t.now += 31   # 距上次使用 61s > 窗口 60s ⇒ 过期释放
    assert st.get_sticky_tier("pytest-ir-sticky-0002", 60) is None
