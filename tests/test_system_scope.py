"""L2 system diff 触发（2026-11-04）。

威胁：无 L1 模式的机密 payload 可搭客户端可写的 system/instructions 出境
（last_user 无害、L2 其他视野全从用户消息流派生 ⇒ 结构性看不见该字段）。
口径：内容变化（含首见，未知≠可信）⇒ L2 判定块前部插 system 块；
指纹只在拿到可信判定后记录（degraded/disabled 不记 ⇒ 下个灰区请求重判）。
"""
import hashlib

from fastapi.testclient import TestClient

from src.gateway.main import app
from src.gateway import main as _m
from src.gateway.session_store import get_session_store

client = TestClient(app)
KEY = "sk_shGD8A0lZpIJbcGavN8z6GBcSl-DGIsecJqgF4r0k_o"
H = {"Authorization": f"Bearer {KEY}"}

SYS = "You are a helpful assistant. 以下是合同要点：甲方应付乙方 380 万元，保密条款见附件。"


def _fp(t):
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:32]


def _mem_fps():
    return {v.get("fp") for v in (get_session_store()._mem.get("__system_fp__") or {}).values()}


def _body(system, user):
    return {"model": "deepseek-flash",
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}


def _fake_multi(results):
    """results: 按调用次序出队的 dict 列表；记录 (chunks, scopes)。"""
    calls = []
    it = iter(results)

    async def _fake(chunks, filename="", headers=None, sheet_names=None, scopes=None):
        calls.append((list(chunks), list(scopes or [])))
        return next(it, {"label": "NORMAL", "confidence": 0.9, "reason": "ok",
                         "latency_ms": 5, "injection": False})
    return _fake, calls


def test_system_change_judged_and_fp_recorded(monkeypatch):
    # 首见 system + L2 判密 ⇒ route_local(l2_confidential) + 指纹落库
    fake, calls = _fake_multi([{"label": "CONFIDENTIAL", "confidence": 0.93,
                                "reason": "system 带合同金额", "latency_ms": 5,
                                "scope": "system", "injection": False}])
    monkeypatch.setattr(_m, "classify_chunks", fake)
    monkeypatch.setenv("AI_GATEWAY_L2_ALWAYS", "true")
    r = client.post("/v1/chat/completions", headers=H, json=_body(SYS, "帮我写个回复"))
    gw = r.json()["gateway"]
    assert gw["action"] == "route_local"
    assert gw["policy_rule"] == "l2_confidential"
    chunks, scopes = calls[-1]
    assert "system" in scopes and SYS in chunks
    assert _mem_fps() == {_fp(SYS)}


def test_system_unchanged_not_resubmitted(monkeypatch):
    # 指纹已记 ⇒ 同 system 再请求不重判（system 块不出现），只判用户消息
    fake, calls = _fake_multi([])
    single = []

    async def _fake_single(text, filename="", headers=None, sheet_names=None):
        single.append(text)
        return {"label": "NORMAL", "confidence": 0.9, "reason": "ok",
                "latency_ms": 5, "injection": False}
    monkeypatch.setattr(_m, "classify_chunks", fake)
    monkeypatch.setattr(_m, "small_classify", _fake_single)
    monkeypatch.setenv("AI_GATEWAY_L2_ALWAYS", "true")
    r1 = client.post("/v1/chat/completions", headers=H, json=_body(SYS, "第一问"))
    assert r1.json()["gateway"]["action"] == "allow"
    r2 = client.post("/v1/chat/completions", headers=H, json=_body(SYS, "第二问"))
    assert r2.json()["gateway"]["action"] == "allow"
    assert len(calls) == 1 and "system" in calls[0][1]
    assert single == ["第二问"] and SYS not in single[0]


def test_system_no_identity_skipped(monkeypatch):
    # 无身份绑定键（干跑/匿名）⇒ 不判不记
    monkeypatch.setattr(_m, "_identity_bind_key", lambda request: "")
    fake, calls = _fake_multi([])
    monkeypatch.setattr(_m, "classify_chunks", fake)
    monkeypatch.setenv("AI_GATEWAY_L2_ALWAYS", "true")
    r = client.post("/v1/chat/completions", headers=H, json=_body(SYS, "帮我写个回复"))
    assert r.status_code == 200
    assert not calls, "无身份不得送 system 块"
    assert _mem_fps() == set()


def test_system_degraded_fp_not_recorded(monkeypatch):
    # 首次判定不可用（degraded）⇒ 不记指纹；同一 system 的下一个灰区请求必须重判
    n = {"i": 0}

    async def _fake(chunks, filename="", headers=None, sheet_names=None, scopes=None):
        n["i"] += 1
        if n["i"] == 1:
            return {"label": "NORMAL", "confidence": 0.0, "reason": "l2 degraded: exception",
                    "latency_ms": 1, "injection": False, "injection_confidence": 0.0,
                    "fallback": True, "degraded": True, "degraded_reason": "exception"}
        return {"label": "NORMAL", "confidence": 0.9, "reason": "ok",
                "latency_ms": 5, "injection": False}
    monkeypatch.setattr(_m, "classify_chunks", _fake)
    monkeypatch.setenv("AI_GATEWAY_L2_ALWAYS", "true")
    r1 = client.post("/v1/chat/completions", headers=H, json=_body(SYS, "第一问"))
    assert r1.json()["gateway"]["policy_rule"] == "l2_unavailable"
    assert _mem_fps() == set(), "degraded 时不得记指纹（未判状态不能失效）"
    r2 = client.post("/v1/chat/completions", headers=H, json=_body(SYS, "第二问"))
    assert n["i"] == 2, "指纹未记录 ⇒ 下一次灰区请求必须重判 system"
    assert r2.json()["gateway"]["action"] == "allow"
    assert _mem_fps() == {_fp(SYS)}


def test_system_gray_trigger_without_l2_always(monkeypatch):
    # 生产默认口径（灰区）：system 带关键词堆出 risk>=20、last_user 无害 ⇒ 照样送 system 块
    fake, calls = _fake_multi([])
    monkeypatch.setattr(_m, "classify_chunks", fake)
    monkeypatch.delenv("AI_GATEWAY_L2_ALWAYS", raising=False)
    sys_kw = "内部数据如下：工资 薪资 奖金 明细，周五前给我"
    r = client.post("/v1/chat/completions", headers=H, json=_body(sys_kw, "帮我写个回复"))
    assert r.json()["gateway"]["action"] == "allow"
    assert len(calls) == 1 and "system" in calls[0][1], "灰区请求未送 system 块"
    assert _mem_fps() == {_fp(sys_kw)}


def test_long_system_tail_two_chunks(monkeypatch):
    # 超长 system：尾部取向 2×chunk_chars（1200），样板前缀不占判定位
    fake, calls = _fake_multi([])
    monkeypatch.setattr(_m, "classify_chunks", fake)
    monkeypatch.setenv("AI_GATEWAY_L2_ALWAYS", "true")
    long_sys = "甲" * 3000
    client.post("/v1/chat/completions", headers=H, json=_body(long_sys, "q"))
    chunks, scopes = calls[-1]
    assert scopes[:2] == ["system", "system"]
    assert chunks[:2] == [long_sys[-2400:-1200], long_sys[-1200:]]


def test_system_extractors():
    assert _m._system_text_chat([{"role": "system", "content": "A"},
                                  {"role": "user", "content": "q"},
                                  {"role": "developer", "content": "B"}]) == "A\nB"
    assert _m._system_text_chat([{"role": "user", "content": "q"}]) == ""
    assert _m._system_text_anthropic({"system": "S"}) == "S"
    assert _m._system_text_anthropic({"system": [
        {"type": "text", "text": "X"}, {"type": "text", "text": "Y"}]}) == "X\nY"
    assert _m._system_text_anthropic({"messages": []}) == ""
    assert _m._system_text_responses({"instructions": "I"}) == "I"
    assert _m._system_text_responses({"input": []}) == ""
