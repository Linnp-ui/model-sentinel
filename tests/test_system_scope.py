"""L2 system diff 触发（2026-11-04，v2 改动区判定）。

威胁：无 L1 模式的机密 payload 可搭客户端可写的 system/instructions 出境
（last_user 无害、L2 其他视野全从用户消息流派生 ⇒ 结构性看不见该字段）。
口径：各客户端 system 结构不同、改动位置不定 ⇒ 存上次全文、diff 定位改动区判那里
（首见/改动超限退均匀抽样）；上次全文只在拿到可信判定后落写（degraded/disabled 不记
⇒ 下个灰区请求重判）。
"""
from fastapi.testclient import TestClient

from src.gateway.main import app
from src.gateway import main as _m
from src.gateway.session_store import get_session_store

client = TestClient(app)
KEY = "sk_shGD8A0lZpIJbcGavN8z6GBcSl-DGIsecJqgF4r0k_o"
H = {"Authorization": f"Bearer {KEY}"}

SYS = "You are a helpful assistant. 以下是合同要点：甲方应付乙方 380 万元，保密条款见附件。"


def _stored_systems():
    return {v.get("text") for v in (get_session_store()._mem.get("__system_text__") or {}).values()}


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


def test_system_first_sight_judged_and_text_recorded(monkeypatch):
    # 首见 system + L2 判密 ⇒ route_local(l2_confidential) + 上次全文落库
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
    assert _stored_systems() == {SYS}


def test_system_unchanged_not_resubmitted(monkeypatch):
    # 上次全文已存且未变 ⇒ 不重判（system 块不出现），只判用户消息
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
    # 无身份绑定键（干跑/匿名）⇒ 不判不存
    monkeypatch.setattr(_m, "_identity_bind_key", lambda request: "")
    fake, calls = _fake_multi([])
    monkeypatch.setattr(_m, "classify_chunks", fake)
    monkeypatch.setenv("AI_GATEWAY_L2_ALWAYS", "true")
    r = client.post("/v1/chat/completions", headers=H, json=_body(SYS, "帮我写个回复"))
    assert r.status_code == 200
    assert not calls, "无身份不得送 system 块"
    assert _stored_systems() == set()


def test_system_degraded_text_not_recorded(monkeypatch):
    # 首次判定不可用（degraded）⇒ 不落上次全文；同一 system 的下一个灰区请求必须重判
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
    assert _stored_systems() == set(), "degraded 时不得落上次全文（未判状态不能失效）"
    r2 = client.post("/v1/chat/completions", headers=H, json=_body(SYS, "第二问"))
    assert n["i"] == 2, "上次全文未落写 ⇒ 下一次灰区请求必须重判 system"
    assert r2.json()["gateway"]["action"] == "allow"
    assert _stored_systems() == {SYS}


def test_system_gray_trigger_without_l2_always(monkeypatch):
    # 生产默认口径（灰区）：system 带关键词堆出 risk>=20、last_user 无害 ⇒ 照样送 system 块
    fake, calls = _fake_multi([])
    monkeypatch.setattr(_m, "classify_chunks", fake)
    monkeypatch.delenv("AI_GATEWAY_L2_ALWAYS", raising=False)
    sys_kw = "内部数据如下：工资 薪资 奖金 明细，周五前给我"
    r = client.post("/v1/chat/completions", headers=H, json=_body(sys_kw, "帮我写个回复"))
    assert r.json()["gateway"]["action"] == "allow"
    assert len(calls) == 1 and "system" in calls[0][1], "灰区请求未送 system 块"
    assert _stored_systems() == {sys_kw}


def test_system_middle_edit_judged(monkeypatch):
    # v2 核心：改动在中段 ⇒ 判改动区（含上下文），样板不占判定位；v1 尾部取向会漏此形态
    fake, calls = _fake_multi([])
    monkeypatch.setattr(_m, "classify_chunks", fake)
    monkeypatch.setenv("AI_GATEWAY_L2_ALWAYS", "true")
    prefix = "公司助手样板说明，请遵守内部规范。" * 20     # ~600 字样板前缀
    suffix = "以上为固定指令，请勿修改。" * 12            # ~180 字样板后缀
    base = prefix + suffix
    secret = "甲乙方合同金额 380 万元，违约金条款见第七条"
    edited = prefix[:300] + secret + suffix              # 中段插入
    assert client.post("/v1/chat/completions", headers=H,
                       json=_body(base, "第一问")).json()["gateway"]["action"] == "allow"
    r2 = client.post("/v1/chat/completions", headers=H, json=_body(edited, "第二问"))
    assert r2.json()["gateway"]["action"] == "allow"
    chunks, scopes = calls[-1]
    assert "system" in scopes, "中段改动未触发 system 块"
    sys_chunks = [c for c, _ in zip(chunks, scopes) if _ == "system"]
    assert any(secret in c for c in sys_chunks), "改动区（中段机密）未被判定"
    assert all(len(c) < 1200 for c in sys_chunks)
    # 判定窗只覆盖改动区+上下文，不应把整段样板都送进去
    assert sum(len(c) for c in sys_chunks) < len(edited)


def test_long_system_first_sight_sampling(monkeypatch):
    # 首见超长 system（无旧文可 diff）⇒ 均匀抽样 4×1200，无位置偏置
    fake, calls = _fake_multi([])
    monkeypatch.setattr(_m, "classify_chunks", fake)
    monkeypatch.setenv("AI_GATEWAY_L2_ALWAYS", "true")
    long_sys = "甲" * 7000
    client.post("/v1/chat/completions", headers=H, json=_body(long_sys, "q"))
    chunks, scopes = calls[-1]
    sys_chunks = [c for c, s in zip(chunks, scopes) if s == "system"]
    # _l2_long_windows(7000, 1200, 4, 0)：n=6>4 ⇒ idx=[0,2,3,5]
    assert sys_chunks == [long_sys[0:1200], long_sys[2400:3600],
                          long_sys[3600:4800], long_sys[6000:7000]]


def test_huge_system_judged_but_not_stored(monkeypatch):
    # 超 difflib 守卫（300KB）的 system：照常首见抽样判定，但上次全文不落 Redis
    # （diff 用不上，防按 key 囤 body 级体积）⇒ 下次仍按首见重判
    fake, calls = _fake_multi([])
    monkeypatch.setattr(_m, "classify_chunks", fake)
    monkeypatch.setenv("AI_GATEWAY_L2_ALWAYS", "true")
    huge = "丙" * 400_000
    r = client.post("/v1/chat/completions", headers=H, json=_body(huge, "q"))
    assert r.json()["gateway"]["action"] == "allow"
    chunks, scopes = calls[-1]
    assert scopes.count("system") == 4, "超长首见仍应均匀抽 4 块判定"
    assert _stored_systems() == set(), "超守卫文本不得落 Redis"
    # 第二次：无存储可比 ⇒ 仍按首见重判（不是"未变不判"）
    client.post("/v1/chat/completions", headers=H, json=_body(huge, "q2"))
    assert calls[-1][1].count("system") == 4


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
