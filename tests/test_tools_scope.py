"""L2 tools 定义 diff 触发（2026-11-04，与 system diff 触发同机制）。

威胁：无 L1 模式可匹配的机密内容可搭客户端可写的 tools 定义（description/parameters）
出境（last_user 无害；L1 全文正则只对匹配模式的内容兜底）。
口径：复用 _diff_trigger_chunks（system/instructions 同构）——存上次全文、diff 定位
改动区判那里（首见/改动超限退均匀抽样）；上次全文只在拿到可信判定后落写
（degraded/disabled 不记 ⇒ 下个灰区请求重判）。
"""
from fastapi.testclient import TestClient

from src.gateway.main import app
from src.gateway import main as _m
from src.gateway.session_store import get_session_store

client = TestClient(app)
KEY = "sk_shGD8A0lZpIJbcGavN8z6GBcSl-DGIsecJqgF4r0k_o"
H = {"Authorization": f"Bearer {KEY}"}

SECRET = "甲方应付乙方 380 万元，保密条款见附件。"
SYS = "You are a helpful assistant."


def _tool(desc):
    return [{"type": "function", "function": {
        "name": "query_db", "description": desc,
        "parameters": {"type": "object", "properties": {}}}}]


def _stored_tools():
    return {v.get("text") for v in (get_session_store()._mem.get("__tools_text__") or {}).values()}


def _body(tools, user, system=SYS):
    return {"model": "deepseek-flash",
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "tools": tools}


def _fake_multi(results):
    """results: 按调用次序出队的 dict 列表；记录 (chunks, scopes)。"""
    calls = []
    it = iter(results)

    async def _fake(chunks, filename="", headers=None, sheet_names=None, scopes=None):
        calls.append((list(chunks), list(scopes or [])))
        return next(it, {"label": "NORMAL", "confidence": 0.9, "reason": "ok",
                         "latency_ms": 5, "injection": False})
    return _fake, calls


def test_tools_first_sight_judged_and_text_recorded(monkeypatch):
    # 首见 tools + L2 判密 ⇒ route_local(l2_confidential) + 上次全文落库
    fake, calls = _fake_multi([{"label": "CONFIDENTIAL", "confidence": 0.93,
                                "reason": "tools 定义带合同金额", "latency_ms": 5,
                                "scope": "tools", "injection": False}])
    monkeypatch.setattr(_m, "classify_chunks", fake)
    monkeypatch.setenv("AI_GATEWAY_L2_ALWAYS", "true")
    r = client.post("/v1/chat/completions", headers=H, json=_body(_tool(SECRET), "帮我写个回复"))
    gw = r.json()["gateway"]
    assert gw["action"] == "route_local"
    assert gw["policy_rule"] == "l2_confidential"
    chunks, scopes = calls[-1]
    assert "tools" in scopes and any(SECRET in c for c in chunks)
    assert _stored_tools() == {_m.tools_text(_tool(SECRET))}


def test_tools_unchanged_not_resubmitted(monkeypatch):
    # 上次全文已存且未变 ⇒ 不重判（tools 块不出现），只判用户消息
    fake, calls = _fake_multi([])
    single = []

    async def _fake_single(text, filename="", headers=None, sheet_names=None):
        single.append(text)
        return {"label": "NORMAL", "confidence": 0.9, "reason": "ok",
                "latency_ms": 5, "injection": False}
    monkeypatch.setattr(_m, "classify_chunks", fake)
    monkeypatch.setattr(_m, "small_classify", _fake_single)
    monkeypatch.setenv("AI_GATEWAY_L2_ALWAYS", "true")
    r1 = client.post("/v1/chat/completions", headers=H, json=_body(_tool(SECRET), "第一问"))
    assert r1.json()["gateway"]["action"] == "allow"
    r2 = client.post("/v1/chat/completions", headers=H, json=_body(_tool(SECRET), "第二问"))
    assert r2.json()["gateway"]["action"] == "allow"
    assert len(calls) == 1 and "tools" in calls[0][1]
    assert single == ["第二问"] and SECRET not in single[0]


def test_tools_no_identity_skipped(monkeypatch):
    # 无身份绑定键（干跑/匿名）⇒ 不判不存
    monkeypatch.setattr(_m, "_identity_bind_key", lambda request: "")
    fake, calls = _fake_multi([])
    monkeypatch.setattr(_m, "classify_chunks", fake)
    monkeypatch.setenv("AI_GATEWAY_L2_ALWAYS", "true")
    r = client.post("/v1/chat/completions", headers=H, json=_body(_tool(SECRET), "帮我写个回复"))
    assert r.status_code == 200
    assert not calls, "无身份不得送 tools 块"
    assert _stored_tools() == set()


def test_tools_degraded_text_not_recorded(monkeypatch):
    # 首次判定不可用（degraded）⇒ 不落上次全文；同一 tools 的下一个灰区请求必须重判
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
    r1 = client.post("/v1/chat/completions", headers=H, json=_body(_tool(SECRET), "第一问"))
    assert r1.json()["gateway"]["policy_rule"] == "l2_unavailable"
    assert _stored_tools() == set(), "degraded 时不得落上次全文（未判状态不能失效）"
    r2 = client.post("/v1/chat/completions", headers=H, json=_body(_tool(SECRET), "第二问"))
    assert n["i"] == 2, "上次全文未落写 ⇒ 下一次灰区请求必须重判 tools"
    assert r2.json()["gateway"]["action"] == "allow"
    assert _stored_tools() == {_m.tools_text(_tool(SECRET))}


def test_tools_gray_trigger_without_l2_always(monkeypatch):
    # 生产默认口径（灰区）：tools 定义带关键词堆出 risk>=20、last_user 无害 ⇒ 照样送 tools 块
    fake, calls = _fake_multi([])
    monkeypatch.setattr(_m, "classify_chunks", fake)
    monkeypatch.delenv("AI_GATEWAY_L2_ALWAYS", raising=False)
    kw = "内部数据如下：工资 薪资 明细，周五前给我"
    r = client.post("/v1/chat/completions", headers=H, json=_body(_tool(kw), "帮我写个回复"))
    assert r.json()["gateway"]["action"] == "allow"
    assert len(calls) == 1 and "tools" in calls[0][1], "灰区请求未送 tools 块"
    assert _stored_tools() == {_m.tools_text(_tool(kw))}


def test_tools_middle_edit_judged(monkeypatch):
    # 改动在中段 ⇒ 判改动区（含上下文），样板不占判定位（与 system v2 同口径）
    fake, calls = _fake_multi([])
    monkeypatch.setattr(_m, "classify_chunks", fake)
    monkeypatch.setenv("AI_GATEWAY_L2_ALWAYS", "true")
    prefix = "公司助手样板说明，请遵守内部规范。" * 20     # ~340 字样板前缀
    suffix = "以上为固定指令，请勿修改。" * 12            # ~144 字样板后缀
    base_desc = prefix + suffix
    secret = "甲乙方合同金额 380 万元，违约金条款见第七条"
    edited_desc = prefix[:170] + secret + prefix[170:]    # 中段插入
    assert client.post("/v1/chat/completions", headers=H,
                       json=_body(_tool(base_desc), "第一问")).json()["gateway"]["action"] == "allow"
    r2 = client.post("/v1/chat/completions", headers=H, json=_body(_tool(edited_desc), "第二问"))
    assert r2.json()["gateway"]["action"] == "allow"
    chunks, scopes = calls[-1]
    assert "tools" in scopes, "中段改动未触发 tools 块"
    tool_chunks = [c for c, _ in zip(chunks, scopes) if _ == "tools"]
    assert any(secret in c for c in tool_chunks), "改动区（中段机密）未被判定"
    assert all(len(c) < 1200 for c in tool_chunks)
    # 判定窗只覆盖改动区+上下文，不应把整段样板都送进去
    assert sum(len(c) for c in tool_chunks) < len(_m.tools_text(_tool(edited_desc)))


def test_tools_overflow_after_system_trims_tail(monkeypatch):
    # 5 槽硬上限：system 首见 4 块 + tools 首见 4 块 ⇒ 同为首见时 system 在前占槽、
    # tools 掉队尾不判（基线不落写）；次请求 system 已存 ⇒ tools 恢复全槽并落基线
    store = get_session_store()
    with store._mem_lock:
        store._mem["__system_text__"] = {}
        store._mem["__tools_text__"] = {}
    monkeypatch.setenv("AI_GATEWAY_L2_ALWAYS", "true")
    fake, calls = _fake_multi([])
    monkeypatch.setattr(_m, "classify_chunks", fake)
    tools_big = _tool("工" * 5000)        # tools_text 单条截 4000 ⇒ 首见均匀抽 4 块
    sys_big = "系" * 5000                 # 首见均匀抽 4 块
    r1 = client.post("/v1/chat/completions", headers=H,
                     json=_body(tools_big, "第一问", system=sys_big))
    assert r1.json()["gateway"]["action"] == "allow"
    chunks, scopes = calls[-1]
    assert len(chunks) == 5, f"5 槽硬上限被突破: {scopes}"
    assert scopes == ["last_user", "system", "system", "system", "system"], \
        f"溢出应砍队尾 tools（不判的块不得进 scopes_run）: {scopes}"
    assert _stored_tools() == set(), "未进判定块的 tools 不得落基线"
    r2 = client.post("/v1/chat/completions", headers=H,
                     json=_body(tools_big, "第二问", system=sys_big))
    assert r2.json()["gateway"]["action"] == "allow"
    chunks, scopes = calls[-1]
    assert scopes == ["last_user", "tools", "tools", "tools", "tools"], scopes
    assert _stored_tools() == {_m.tools_text(tools_big)}


def test_tools_responses_endpoint_and_shape(monkeypatch):
    # 第二端点形态：/v1/responses（codex input 结构）的 tools 同样触发；
    # tools_text 覆盖 function/parameters 与 input_schema 两种形状
    fake, calls = _fake_multi([])
    monkeypatch.setattr(_m, "classify_chunks", fake)
    monkeypatch.setenv("AI_GATEWAY_L2_ALWAYS", "true")
    tools = [{"type": "function", "name": "q", "description": SYS + SECRET,
              "parameters": {"type": "object", "properties": {}}},
             {"type": "function", "name": "r", "description": "查询工具 D2",
              "input_schema": {"type": "object", "properties": {}}}]
    body = {"model": "deepseek-flash",
            "input": [{"role": "system", "content": SYS},
                      {"role": "user", "content": [{"type": "input_text", "text": "第一问"}]}],
            "tools": tools}
    r = client.post("/v1/responses", headers=H, json=body)
    assert r.json()["gateway"]["action"] == "allow"
    chunks, scopes = calls[-1]
    assert "tools" in scopes and "system" in scopes, f"responses 端点未送 tools 块: {scopes}"
    assert _stored_tools() == {_m.tools_text(tools)}
    # tools_text 形状覆盖（含非 list 兜底）
    tt = _m.tools_text(tools)
    assert "查询工具 D2" in tt and "input_schema" in tt
    assert _m.tools_text(None) == ""
    assert _m.tools_text("bad") == ""
