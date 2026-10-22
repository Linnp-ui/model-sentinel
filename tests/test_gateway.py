"""
pytest 快速验证 - 无需 postgres/redis
tests/test_gateway.py
"""
from fastapi.testclient import TestClient
from src.gateway.main import app

client = TestClient(app)
KEY = "sk_shGD8A0lZpIJbcGavN8z6GBcSl-DGIsecJqgF4r0k_o"
H = {"Authorization": f"Bearer {KEY}"}

def test_chat_allow():
    r = client.post("/v1/chat/completions", headers=H, json={"model":"gpt-4o-mini","messages":[{"role":"user","content":"总结CAP定理"}]})
    assert r.status_code == 200
    assert r.json()["gateway"]["action"] == "allow"

def test_chat_pii_route_local():
    r = client.post("/v1/chat/completions", headers=H, json={"model":"gpt-4o-mini","messages":[{"role":"user","content":"员工张三 身份证 110101199001011237"}]})
    assert r.status_code == 200
    assert r.json()["gateway"]["action"] == "route_local"

def test_file_financial_route_local(tmp_path=None):
    # 用内存生成 xlsx
    import io, openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "工资表"
    ws.append(["姓名","身份证","工资"])
    ws.append(["张三","110101199001011234", "30000"])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    r = client.post("/v1/files/check", headers=H, files={"file": ("salary.xlsx", buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200
    assert r.json()["gateway"]["action"] == "route_local"
    assert r.json()["gateway"]["policy_rule"] == "financial_local_only"

def test_model_rate_limit(tmp_path, monkeypatch):
    # 模型限流：model_policy.model_limits 全局 RPM，超限 429；其他模型不受影响
    import yaml
    from src.gateway import model_policy as mp
    from src.gateway.rate_limit import get_limiter
    p = tmp_path / "model_policy.yaml"
    p.write_text(yaml.safe_dump({"model_policy": {
        "external_candidates": [],
        "internal_models": [],
        "model_limits": [{"model": "gpt-4o-mini", "rpm": 1, "enabled": True}],
        "review_model": {"threshold": 0.7},
    }}, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_MODEL_POLICY_PATH", str(p))
    mp.invalidate_model_policy_cache()
    get_limiter().reset()
    body = lambda m: {"model": m, "messages": [{"role": "user", "content": "hello"}]}
    try:
        r1 = client.post("/v1/chat/completions", headers=H, json=body("gpt-4o-mini"))
        assert r1.status_code == 200
        r2 = client.post("/v1/chat/completions", headers=H, json=body("gpt-4o-mini"))
        assert r2.status_code == 429
        assert r2.json()["detail"]["type"] == "model_rate_limited"
        r3 = client.post("/v1/chat/completions", headers=H, json=body("deepseek/deepseek-chat"))
        assert r3.status_code == 200
    finally:
        mp.invalidate_model_policy_cache()
        get_limiter().reset()

def test_file_drawing_route_local():
    from pathlib import Path
    # 静态 fixture PDF（须含 CJK 文本层；重新生成见 git 历史 reportlab 脚本）
    buf = Path(__file__).parent / "fixtures" / "drawing_a01.pdf"
    r = client.post("/v1/files/check", headers=H, files={"file": ("drawing_A01.pdf", open(buf, "rb"), "application/pdf")})
    assert r.status_code == 200
    assert r.json()["gateway"]["action"] == "route_local"

def test_rule_priority_suggestion_and_reorder(tmp_path, monkeypatch):
    # 规则 4：按触发次数重排，但按 action 分组各排各的——block 组与 route_local 组
    # 只在自己当前槽位内 permute，不跨组（2026-10-19：跨组混排会拦回 block 修复，
    # 高频 PII route_local 把block_secrets挤到身后 ⇒ reject 403 失效）。
    import yaml
    from src.gateway import policy as _pol
    from src.gateway.admin_store import get_admin_store
    p = tmp_path / "policy.yaml"
    p.write_text(yaml.safe_dump({"policies": [
        {"name": "rl_slow", "priority": 10, "action": "route_local",
         "when": {"any": [{"text contains_any": ["rs"]}]},
         "target": {"provider": "vllm_local", "model": "qwen2.5:7b"}},
        {"name": "rl_fast", "priority": 20, "action": "route_local",
         "when": {"any": [{"text contains_any": ["rf"]}]},
         "target": {"provider": "vllm_local", "model": "qwen2.5:7b"}},
        {"name": "b_slow", "priority": 30, "action": "block",
         "when": {"any": [{"text contains_any": ["bs"]}]},
         },
        {"name": "b_fast", "priority": 40, "action": "block",
         "when": {"any": [{"text contains_any": ["bf"]}]}},
    ]}, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_POLICY_PATH", str(p))
    # 规则窗口钉 24h ⇒ 优先级重排阈值 = 基准值（不放大），下面的次数才够得着
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_RULE_WINDOW_HOURS", "24")
    _pol.invalidate_policy_cache()
    store = get_admin_store()
    try:
        # 同一 (provider,model) 对 ≤25 行——别抢同 session 其他用例的 Top1 槽（limit 用例 100 行）
        seed = lambda reason, n: [store.insert_request_log(
            {"key_name": "pytest-reorder", "action": "block", "model": f"m{i % 4}",
             "provider": "p", "client_ip": "9.9.9.9", "blocked_reason": reason})
            for i in range(n)]
        # 两组各自错位：高频规则都排在组内靠后
        seed("l1:rl_fast", 50)
        seed("l1:rl_slow", 15)
        seed("l1:b_fast", 25)
        seed("l1:b_slow", 12)
        items = client.get("/admin/api/suggestions", params={"hours": 168}).json()["items"]
        ro = {s["id"]: s for s in items if (s.get("apply") or {}).get("kind") == "reorder"}
        assert set(ro) == {"reorder:route_local", "reorder:block"}, items
        assert ro["reorder:route_local"]["apply"]["order"] == ["rl_fast", "rl_slow"]
        assert ro["reorder:block"]["apply"]["order"] == ["b_fast", "b_slow"]
        # 不跨组：两组的 order 互不含对方规则
        assert not (set(ro["reorder:route_local"]["apply"]["order"]) & {"b_fast", "b_slow"})
        assert not (set(ro["reorder:block"]["apply"]["order"]) & {"rl_fast", "rl_slow"})
        for s in ro.values():
            r = client.post("/admin/api/suggestions/apply", json=s["apply"])
            assert r.status_code == 200, r.text
        prio = {x.name: x.priority for x in _pol.load_policy()}
        # 各组只在自己槽位内 permute：route_local 占 {10,20}、block 占 {30,40}，组间边界不动
        assert prio == {"rl_fast": 10, "rl_slow": 20, "b_fast": 30, "b_slow": 40}, prio
        items2 = client.get("/admin/api/suggestions", params={"hours": 168}).json()["items"]
        assert not [s for s in items2 if (s.get("apply") or {}).get("kind") == "reorder"], items2
    finally:
        _pol.invalidate_policy_cache()

def test_silent_rule_suggests_removal(tmp_path, monkeypatch):
    # 规则5：长窗口零触发的规则建议删除；一键删除后建议消失
    import yaml
    from src.gateway import policy as _pol
    from src.gateway.admin_store import get_admin_store
    p = tmp_path / "policy.yaml"
    p.write_text(yaml.safe_dump({"policies": [
        {"name": "rule_busy", "priority": 10, "action": "route_local",
         "when": {"any": [{"text contains_any": ["busy"]}]},
         "target": {"provider": "vllm_local", "model": "qwen2.5:7b"}},
        {"name": "rule_ghost", "priority": 20, "action": "block",
          "when": {"any": [{"text contains_any": ["ghost"]}]}},
     ]}, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_POLICY_PATH", str(p))
    # 规则5 first-seen 隔离：两规则都记成老规则（保底期外），ghost 才能正常上榜
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_RULE_SEEN_PATH",
                       str(tmp_path / "rule_first_seen.json"))
    (tmp_path / "rule_first_seen.json").write_text(
        '{"rule_busy": "2020-01-01", "rule_ghost": "2020-01-01"}', encoding="utf-8")
    _pol.invalidate_policy_cache()
    store = get_admin_store()
    try:
        for _ in range(12):
            store.insert_request_log({"key_name": "pytest-ghost", "action": "block",
                                      "model": "m", "provider": "p", "client_ip": "9.9.9.9",
                                      "blocked_reason": "l1:rule_busy"})
        items = client.get("/admin/api/suggestions", params={"hours": 168}).json()["items"]
        sil = [s for s in items if s.get("id", "").startswith("silent:")]
        assert [s["apply"]["rule_name"] for s in sil] == ["rule_ghost"], items
        r = client.post("/admin/api/suggestions/apply",
                        json={"kind": "remove_rule", "rule_name": "rule_ghost"})
        assert r.status_code == 200, r.text
        assert "rule_ghost" not in {x.name for x in _pol.load_policy()}
        items2 = client.get("/admin/api/suggestions", params={"hours": 168}).json()["items"]
        assert not [s for s in items2 if s.get("id", "").startswith("silent:")], items2
    finally:
        _pol.invalidate_policy_cache()

def test_silent_rule_new_rule_grace(tmp_path, monkeypatch):
    # 规则5 上线保底：从未触发过的新规则（first-seen=今天）不上榜；
    # 40 天前触发过、近 30 天哑火的老规则照常上榜
    import time as _t
    import yaml
    from src.gateway import policy as _pol
    from src.gateway.admin_store import get_admin_store
    p = tmp_path / "policy.yaml"
    p.write_text(yaml.safe_dump({"policies": [
        {"name": "rule_newbie", "priority": 10, "action": "route_local",
         "when": {"any": [{"text contains_any": ["newbie"]}]},
         "target": {"provider": "vllm_local", "model": "qwen2.5:7b"}},
        {"name": "rule_oldquiet", "priority": 20, "action": "block",
         "when": {"any": [{"text contains_any": ["oldquiet"]}]}},
    ]}, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_POLICY_PATH", str(p))
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_RULE_SEEN_PATH",
                       str(tmp_path / "rule_first_seen.json"))
    _pol.invalidate_policy_cache()
    store = get_admin_store()
    try:
        old_ts = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(_t.time() - 40 * 86400))
        store.insert_request_log({"key_name": "pytest-grace", "action": "block",
                                  "model": "m", "provider": "p", "client_ip": "9.9.9.10",
                                  "blocked_reason": "l1:rule_oldquiet", "ts": old_ts})
        items = client.get("/admin/api/suggestions", params={"hours": 168}).json()["items"]
        sil = [s["apply"]["rule_name"] for s in items if s.get("id", "").startswith("silent:")]
        assert "rule_oldquiet" in sil, items
        assert "rule_newbie" not in sil, items
    finally:
        _pol.invalidate_policy_cache()

def test_circuit_breaker_trip_and_recover(monkeypatch):
    # 熔断状态机（线上只敢手动 open 验门控，跳闸计数/半开恢复走单测）：
    # 2 次失败→open（fail-fast），冷却过→half_open 放 1 个探针，2 次成功→closed；
    # 半开探针失败→重开。key 用唯一名，不污染共享单例上其他 key。
    from src.gateway import circuit_breaker as _cb
    monkeypatch.setenv("AI_GATEWAY_CB_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("AI_GATEWAY_CB_WINDOW_SECONDS", "60")
    monkeypatch.setenv("AI_GATEWAY_CB_RECOVERY_SECONDS", "3600")
    monkeypatch.setenv("AI_GATEWAY_CB_SUCCESS_THRESHOLD", "2")
    b = _cb.get_breaker()
    key = "pytest-cb-trip"
    assert b.allow(key) and b.state(key) == "closed"
    b.record_failure(key)
    assert b.allow(key), "1 次失败不应跳闸"
    b.record_failure(key)
    assert not b.allow(key) and b.state(key) == "open", "2 次失败应跳闸"
    monkeypatch.setenv("AI_GATEWAY_CB_RECOVERY_SECONDS", "0")
    assert b.allow(key) and b.state(key) == "half_open", "冷却过应放探针"
    b.record_success(key)
    assert b.state(key) == "half_open", "1 次成功不够（要 2 次）"
    monkeypatch.setenv("AI_GATEWAY_CB_RECOVERY_SECONDS", "3600")
    b.record_failure(key)
    assert b.state(key) == "open", "半开探针失败应重开"
    monkeypatch.setenv("AI_GATEWAY_CB_RECOVERY_SECONDS", "0")
    assert b.allow(key), "冷却 0 应再放探针"
    b.record_success(key)
    b.record_success(key)
    assert b.state(key) == "closed" and b.allow(key)

def _cb_stub_upstream(monkeypatch, rt, behavior):
    """假上游：behavior(idx, url) 返回 httpx.Response 或抛 httpx.HTTPError，返回调用记录。

    只为「上游报错」这条分支存在：真连外网会 503 且不确定，连内网 mock 又走不到
    record_failure；这里替换掉 httpx client，路由/熔断代码本身全部是真身。
    """
    calls = []

    class _Ctx:
        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *exc):
            return False

    class _Stub:
        async def post(self, url, headers=None, json=None, timeout=None):
            calls.append(url)
            out = behavior(len(calls) - 1, url)
            if isinstance(out, Exception):
                raise out
            return out

        def stream(self, method, url, headers=None, json=None, timeout=None):
            calls.append(url)
            out = behavior(len(calls) - 1, url)
            if isinstance(out, Exception):
                raise out
            return _Ctx(out)

    monkeypatch.setattr(rt, "_get_client", lambda local=False: _Stub())
    return calls


def _prom_value(text, name):
    for line in text.splitlines():
        if line.startswith(name + " "):
            return float(line.split()[1])
    return 0.0


def test_upstream_5xx_trips_breaker_then_failfast(monkeypatch):
    # 「上游报错 → 熔断触发」的集成路径（线上只敢手动 open，跳闸走这层）：
    # 5xx 记失败 → 达阈值跳闸 → 后续请求 fail-fast（不再打上游）+ 指标计数
    import asyncio
    import httpx
    import pytest
    from src.gateway import routing as _rt, circuit_breaker as _cb
    from src.gateway.metrics import get_metrics
    from src.gateway.providers import Provider

    monkeypatch.setenv("AI_GATEWAY_CB_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("AI_GATEWAY_CB_WINDOW_SECONDS", "60")
    monkeypatch.setenv("AI_GATEWAY_CB_RECOVERY_SECONDS", "600")
    monkeypatch.setattr(_rt, "_mock_enabled", lambda *a, **k: False)
    prov = Provider(name="pytest-cb-upstream-5xx", base_url="http://cb5xx.invalid",
                    api_key_value="K")
    url = "http://cb5xx.invalid/v1/chat/completions"
    calls = _cb_stub_upstream(monkeypatch, _rt,
                              lambda i, u: httpx.Response(500, text="upstream boom",
                                                          request=httpx.Request("POST", u)))
    b = _cb.get_breaker()
    key = _cb.key_for(prov.name, "m")
    b.reset(key)

    async def _call():
        return await _rt._post(prov, url, {"model": "m"}, "K", "m")

    tripped0 = _prom_value(get_metrics().render(), "gateway_circuit_tripped_total")
    short0 = _prom_value(get_metrics().render(), "gateway_circuit_short_circuit_total")
    try:
        assert asyncio.run(_call()).status_code == 500
        assert b.state(key) == "closed", "1 次失败（阈值 2）不应跳闸"
        assert asyncio.run(_call()).status_code == 500
        assert b.state(key) == "open", "连续 5xx 达阈值应跳闸"
        assert b.snapshot()[key]["failure_count_window"] == 2
        assert _prom_value(get_metrics().render(), "gateway_circuit_tripped_total") == tripped0 + 1
        n = len(calls)
        with pytest.raises(_rt.RoutingError) as ei:
            asyncio.run(_call())
        assert ei.value.status_code == 503 and "circuit open" in str(ei.value)
        assert len(calls) == n, "熔断打开后必须 fail-fast，不得再打上游"
        assert _prom_value(get_metrics().render(),
                           "gateway_circuit_short_circuit_total") == short0 + 1
    finally:
        b.reset(key)


def test_stream_upstream_5xx_trips_breaker(monkeypatch):
    # 流式是生产主路径：status>=500 必须同样记失败（漏这条，主路径的熔断就是死的）
    import asyncio
    import httpx
    import pytest
    from src.gateway import routing as _rt, circuit_breaker as _cb
    from src.gateway.providers import Provider

    monkeypatch.setenv("AI_GATEWAY_CB_FAILURE_THRESHOLD", "1")
    monkeypatch.setenv("AI_GATEWAY_CB_RECOVERY_SECONDS", "600")
    monkeypatch.setattr(_rt, "_mock_enabled", lambda *a, **k: False)
    prov = Provider(name="pytest-cb-stream", base_url="http://cbs.invalid", local=True)
    calls = _cb_stub_upstream(monkeypatch, _rt,
                              lambda i, u: httpx.Response(500, text="upstream boom",
                                                          request=httpx.Request("POST", u)))
    b = _cb.get_breaker()
    key = _cb.key_for(prov.name, "m")
    b.reset(key)

    async def _once():
        async for _ in _rt._chat_stream_once({"model": "m", "messages": []},
                                             {"action": "allow"}, prov=prov, model="m"):
            pass

    try:
        with pytest.raises(_rt.RoutingError) as ei:
            asyncio.run(_once())
        assert ei.value.status_code == 500 and b.state(key) == "open"
        n = len(calls)
        with pytest.raises(_rt.RoutingError) as ei2:
            asyncio.run(_once())
        assert ei2.value.status_code == 503 and "circuit open" in str(ei2.value)
        assert len(calls) == n, "跳闸后流式请求也必须 fail-fast"
    finally:
        b.reset(key)


def test_upstream_network_error_trips_but_4xx_does_not(monkeypatch):
    # 超时/拒连与 5xx 同等记失败；4xx 是上游正常工作（客户端错）⇒ 不计失败、不跳闸
    import asyncio
    import httpx
    import pytest
    from src.gateway import routing as _rt, circuit_breaker as _cb
    from src.gateway.providers import Provider

    monkeypatch.setenv("AI_GATEWAY_CB_FAILURE_THRESHOLD", "1")
    monkeypatch.setenv("AI_GATEWAY_CB_RECOVERY_SECONDS", "600")
    monkeypatch.setattr(_rt, "_mock_enabled", lambda *a, **k: False)
    prov = Provider(name="pytest-cb-net", base_url="http://cbnet.invalid", api_key_value="K")
    url = "http://cbnet.invalid/v1/embeddings"
    b = _cb.get_breaker()
    key = _cb.key_for(prov.name, "m")
    b.reset(key)
    try:
        _cb_stub_upstream(monkeypatch, _rt,
                          lambda i, u: httpx.ConnectError("connection refused"))
        with pytest.raises(_rt.RoutingError) as ei:
            asyncio.run(_rt._post(prov, url, {"model": "m"}, "K", "m"))
        assert ei.value.status_code == 502 and "unreachable" in str(ei.value)
        assert b.state(key) == "open", "连不上上游应与 5xx 同等计失败"

        b.reset(key)
        _cb_stub_upstream(monkeypatch, _rt,
                          lambda i, u: httpx.Response(400, text="bad request",
                                                      request=httpx.Request("POST", u)))
        assert asyncio.run(_rt._post(prov, url, {"model": "m"}, "K", "m")).status_code == 400
        assert b.state(key) == "closed" and b.allow(key), "4xx 不该跳闸"
    finally:
        b.reset(key)


def test_routing_provider_lookup_case_insensitive():
    # provider 名大小写不敏感：routing.yaml 键是小写（deepseek/vllm_local），
    # 别名组成员常是展示名（Deepseek/Vllm_Local）——查不到就是运行期 503 unknown provider
    from src.gateway.providers import Provider, RoutingConfig

    p = Provider(name="deepseek", base_url="http://d.invalid", api_key_value="K")
    cfg = RoutingConfig(providers={"deepseek": p})
    assert cfg.get("Deepseek") is p and cfg.get("DEEPSEEK") is p
    assert cfg.get("nope") is None and cfg.get(None) is None


def test_alias_candidates_skip_open_candidate(monkeypatch):
    # 别名组按 provider::model 记熔断：跳闸成员被跳过（切下一候选）；全组跳闸 → 无候选（上层 503）
    # 记账在 routing 的上游尝试处（_post），这里直接喂熔断器模拟"该候选已跳闸"
    from src.gateway import alias_router as _ar, circuit_breaker as _cb

    monkeypatch.setenv("AI_GATEWAY_CB_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("AI_GATEWAY_CB_RECOVERY_SECONDS", "600")
    b = _cb.get_breaker()
    keys = [("pytest-alias-a", "ma"), ("pytest-alias-b", "mb")]
    group = {"name": "AB", "candidates": [{"provider": "pytest-alias-a", "model": "ma"},
                                          {"provider": "pytest-alias-b", "model": "mb"}]}
    for p, m in keys:
        b.reset(_ar._cb_key(p, m))
    try:
        assert [c["idx"] for c in _ar.alias_candidates(group)] == [0, 1]
        for _ in range(2):
            b.record_failure(_ar._cb_key("pytest-alias-a", "ma"))
        assert [c["provider"] for c in _ar.alias_candidates(group)] == ["pytest-alias-b"]
        assert _ar.alias_candidates(group, skip=2) == [], "候选用尽 → 无候选（上层给 503）"
        for _ in range(2):
            b.record_failure(_ar._cb_key("pytest-alias-b", "mb"))
        assert _ar.alias_candidates(group) == [], "整组熔断 → 无候选，不静默转本地"
    finally:
        for p, m in keys:
            b.reset(_ar._cb_key(p, m))


def test_breaker_key_shared_by_alias_and_direct(monkeypatch):
    # 熔断键 = provider::model：点名请求失败跳闸后，**别名组同一候选**必须一起被跳过
    # （两套键 = 上游挂了别名组照打，熔断形同虚设）
    import asyncio
    import httpx
    import pytest
    from src.gateway import alias_router as _ar, circuit_breaker as _cb
    from src.gateway import routing as _rt
    from src.gateway.providers import Provider

    monkeypatch.setenv("AI_GATEWAY_CB_FAILURE_THRESHOLD", "1")
    monkeypatch.setenv("AI_GATEWAY_CB_RECOVERY_SECONDS", "600")
    monkeypatch.setattr(_rt, "_mock_enabled", lambda *a, **k: False)
    prov = Provider(name="pytest-cb-shared", base_url="http://cbshared.invalid",
                    api_key_value="K")
    url = "http://cbshared.invalid/v1/chat/completions"
    _cb_stub_upstream(monkeypatch, _rt,
                      lambda i, u: httpx.Response(500, text="boom",
                                                  request=httpx.Request("POST", u)))
    b = _cb.get_breaker()
    key = _cb.key_for(prov.name, "m1")
    b.reset(key)
    group = {"name": "AB", "candidates": [{"provider": prov.name, "model": "m1"}]}
    try:
        asyncio.run(_rt._post(prov, url, {"model": "m1"}, "K", "m1"))
        assert b.state(key) == "open", "点名请求 5xx 应跳闸"
        assert _ar.alias_candidates(group) == [], "别名组必须看到同一份熔断状态"
        with pytest.raises(_rt.RoutingError) as ei:
            asyncio.run(_rt._post(prov, url, {"model": "m1"}, "K", "m1"))
        assert ei.value.status_code == 503 and "circuit open" in str(ei.value)
    finally:
        b.reset(key)


def test_alias_candidates_get_own_provider_key(monkeypatch):
    # 凭据池是 per-provider 的：别名组切到候选 #2 时若沿用候选 #1 的 key，
    # 就把 A 家的 key 打到 B 家（401，且审计里看不出来源）。
    # route_chat / route_messages / 两个 stream 路径都必须逐候选解析。
    import asyncio
    from src.gateway import routing as _rt
    from src.gateway.providers import Provider
    from src.gateway.identity import Identity, set_current_identity

    pa = Provider(name="provA", base_url="http://a.invalid", api_key_value="KEY-A")
    pb = Provider(name="provB", base_url="http://b.invalid", api_key_value="KEY-B")
    set_current_identity(Identity(key_id=1, name="t", owner="", source="registry",
                                  token_fp="fp"))

    class _Cfg:
        on_missing_key = "reject"

        def get(self, name):
            return {"provA": pa, "provB": pb}.get(name)

        def external_provider(self):
            return pa

    monkeypatch.setattr(_rt, "load_routing", lambda: _Cfg())
    monkeypatch.setattr(_rt, "_mock_enabled", lambda *a, **k: False)
    monkeypatch.setattr(_rt, "find_provider", lambda name: {"provA": pa, "provB": pb}.get(name))
    monkeypatch.setattr(_rt, "_alias_context", lambda *a, **k: {"name": "AB", "candidates": []})
    from src.gateway import alias_router as _ar
    monkeypatch.setattr(_ar, "alias_candidates",
                        lambda g, skip=0: [{"provider": "provA", "model": "mA", "idx": 0},
                                           {"provider": "provB", "model": "mB", "idx": 1}][skip:])
    monkeypatch.setattr(_ar, "record_alias_ok", lambda *a, **k: None)

    seen = []

    async def _fake_post(prov, url, body, ukey="", model=""):
        seen.append((prov.name, ukey))
        raise _rt.RoutingError(f"{prov.name} boom", 502)

    monkeypatch.setattr(_rt, "_post", _fake_post)

    async def _failover_once():
        seen.clear()
        try:
            await _rt.route_chat({"model": "AB", "messages": []}, {"action": "allow"})
        except _rt.RoutingError:
            pass
        return list(seen)

    # 候选 #1 失败后切 #2，#2 必须拿到 provB 的 key
    assert asyncio.run(_failover_once()) == [("provA", "KEY-A"), ("provB", "KEY-B")]

    async def _msg_once():
        seen.clear()
        try:
            await _rt.route_messages({"model": "AB"}, {"action": "allow"})
        except _rt.RoutingError:
            pass
        return list(seen)

    assert asyncio.run(_msg_once()) == [("provA", "KEY-A"), ("provB", "KEY-B")]

def test_keys_import_accepts_named_and_bare_key():
    # 批量导入（手写 key）：每行一条，"名称,key" 与只写 key 都收（名称按行号自动生成）；
    # 注释行忽略、key < 8 位无效、"sk-x,sk-y"（第 1 段疑似 key）无效、重复 key 记 duplicate
    text = "\n".join([
        "# 注释行",
        "批量甲,sk_pytest_import_a_0001",
        "sk_pytest_import_b_0002",
        "批量丙,short",
        "sk-x,sk-y",
    ])
    r = client.post("/admin/api/keys/import", json={"text": text})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["added"] == 2, d
    by_line = {x["line"]: x for x in d["results"]}
    assert by_line[2]["status"] == "ok" and by_line[2]["message"] == "批量甲"
    assert by_line[3]["status"] == "ok" and by_line[3]["message"] == "导入-03", by_line[3]
    assert by_line[4]["status"] == "invalid" and "8 位" in by_line[4]["message"]
    assert by_line[5]["status"] == "invalid" and "名称,key" in by_line[5]["message"]
    # 响应只回掩码，不回明文
    assert all("sk_pytest_import" not in x["key_masked"] for x in d["results"])
    # 再导一次同一个 key → duplicate（不重复登记）
    r2 = client.post("/admin/api/keys/import", json={"text": "sk_pytest_import_b_0002"})
    assert r2.json()["added"] == 0 and r2.json()["results"][0]["status"] == "duplicate"

def test_bare_internal_model_name_routes_local():
    # 2026-10-21 巡检修：裸内网模型名（model_policy.internal_models 里登记的，如
    # qwen2.5:7b）此前落 default_external ⇒ 客户端以为走内网、实际内容出境。
    # 现在钉回它登记的内网 provider；/v1/models 暴露的名字与实际路由必须一致。
    r = client.post("/v1/chat/completions", headers=H,
                    json={"model": "qwen2.5:7b", "messages": [{"role": "user", "content": "你好"}]})
    assert r.status_code == 200, r.text
    g = r.json()["gateway"]
    assert g["provider"] == "vllm_local", g
    assert g["local"] is True, g
    assert g["model"] == "qwen2.5:7b", g
    # 斜杠写法仍然走同一个内网 provider（老客户端不受影响）
    r2 = client.post("/v1/chat/completions", headers=H,
                     json={"model": "vllm_local/qwen2.5:7b", "messages": [{"role": "user", "content": "你好"}]})
    assert r2.json()["gateway"]["provider"] == "vllm_local"
    # 未登记的裸名仍按原样交给上游（不 404：线上有客户端用历史模型名请求）
    r3 = client.post("/v1/chat/completions", headers=H,
                     json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "你好"}]})
    assert r3.status_code == 200
    assert r3.json()["gateway"]["provider"] == "deepseek", r3.json()["gateway"]
    # 网关知道但不是对话模型的内部名（laya 的 /classify 决策模型）⇒ 404，
    # 不能把它当普通模型名转给外网上游（= 把内容发给一个不是它的上游）
    r4 = client.post("/v1/chat/completions", headers=H,
                     json={"model": "laya-multilingual", "messages": [{"role": "user", "content": "你好"}]})
    assert r4.status_code == 404, r4.text
    assert r4.json()["detail"]["type"] == "model_not_chat", r4.text


def test_public_models_excludes_non_chat_internal():
    # /v1/models 只暴露「能对话的内网模型」：laya 是 /classify 决策模型（非 chat），
    # model_policy 注释即写「不暴露」；暴露出去客户端会拿它当对话模型调 → 落外网。
    r = client.get("/v1/models", headers=H)
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    assert "qwen2.5:7b" in ids, ids
    assert "laya-multilingual" not in ids, ids


def test_rate_limit_rps_enforced_alongside_rpm(monkeypatch):
    # 2026-10-21 修：此前 rpm>0 时 rps 分支不可达 ⇒ AI_GATEWAY_RATE_LIMIT_RPS 是死配置
    # （实测 20 并发全 200、无 429）。现在两个桶都要过。
    from src.gateway.rate_limit import get_limiter
    monkeypatch.setenv("AI_GATEWAY_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("AI_GATEWAY_RATE_LIMIT_RPM", "60")
    monkeypatch.setenv("AI_GATEWAY_RATE_LIMIT_RPS", "2")
    get_limiter().reset()
    try:
        assert get_limiter().check("k1", scope="chat")[0] is True
        assert get_limiter().check("k1", scope="chat")[0] is True
        ok, retry, reason = get_limiter().check("k1", scope="chat")
        assert ok is False and reason == "rps", (ok, retry, reason)
        assert retry >= 1
    finally:
        get_limiter().reset()


def test_cand_key_gates_per_candidate():
    # 候选可转发性必须按**该候选自己**的 provider 判：用当前 prov 的 key 去筛
    # 会误杀「有自己凭据」的候选（跨 provider 混排别名组的典型形态）。
    from src.gateway import routing as _rt
    from src.gateway.providers import Provider
    from src.gateway.identity import Identity, set_current_identity

    set_current_identity(Identity(key_id=1, name="t", owner="", source="registry", token_fp="fp"))
    withkey = Provider(name="withkey", api_key_value="K1")
    nokey = Provider(name="nokey")
    assert _rt._cand_key("", withkey) == "K1"
    assert _rt._cand_key("", nokey) == ""
    # 客户端自带凭据优先，且对所有候选一致（BYOK 不分 provider）
    assert _rt._cand_key("client-key", nokey) == "client-key"
    # 未登记身份 fail-closed
    set_current_identity(None)
    assert _rt._cand_key("", withkey) == ""

def test_err_provider_prefers_exc_alias():
    # 别名 503 信封 provider 曾误标预解析的默认 provider（local-model 熔断标成 deepseek）：
    # RoutingError 自带 provider（别名名）优先，否则回落 prov.name，无则空串
    from src.gateway.main import _err_provider
    from src.gateway.routing import RoutingError
    e = RoutingError("alias local-model: no available candidate (circuit open)", 503,
                     provider="local-model")
    assert e.provider == "local-model" and e.status_code == 503
    assert RoutingError("boom", 502).provider == ""
    class P:
        name = "deepseek"
    assert _err_provider(e, P()) == "local-model"
    assert _err_provider(RoutingError("boom", 502), P()) == "deepseek"
    assert _err_provider(RoutingError("boom", 502), None) == ""

def test_classify_chunks_carries_shadow():
    # 多块聚合曾自建 out 丢掉各块的 shadow_*（生产 L2 多 scope，影子覆盖率被低估到 3/151）：
    # 聚合必须把影子键从决策块带出来
    import asyncio
    from unittest.mock import patch
    from src.gateway import small_model as _sm

    async def fake_classify(text, filename="", headers=None, sheet_names=None):
        return {"label": "NORMAL", "confidence": 0.9, "reason": "q", "latency_ms": 10,
                "shadow_label": "CONFIDENTIAL", "shadow_conf": 0.99,
                "shadow_probabilities": {"CONFIDENTIAL": 0.99, "NORMAL": 0.01},
                "shadow_reason": "s"}

    with patch.object(_sm, "classify", side_effect=fake_classify):
        out = asyncio.run(_sm.classify_chunks(["a", "b"], scopes=["last_user", "tail"]))
    assert out["label"] == "NORMAL"
    assert out["shadow_label"] == "CONFIDENTIAL" and out["shadow_conf"] == 0.99
    assert out["shadow_probabilities"] == {"CONFIDENTIAL": 0.99, "NORMAL": 0.01}

def test_idle_whitelist_suggests_revoke():
    # 规则6：30天零调用的白名单建议撤销（unwhite 复用）；建白不久的靠 created_at 跳过
    import time as _time
    from src.gateway.admin_store import get_admin_store
    store = get_admin_store()
    plain = "sk_idle_%d" % int(_time.time() * 1000)
    kid = store.create_api_key(plain, "pytest-idle", "", "pytest idle whitelist")["id"]
    rid = store.add_key_rule(kind="white", key_value=plain, note="pytest", source="strategy")["id"]
    try:
        store._key_rules[rid]["created_at"] = "2020-01-01 00:00:00"  # 模拟老白名单
        items = client.get("/admin/api/suggestions", params={"hours": 168}).json()["items"]
        idle = [s for s in items if s.get("type") == "key_white_idle"]
        assert len(idle) == 1 and idle[0]["apply"] == {"kind": "unwhite", "rule_id": rid}, items
        r = client.post("/admin/api/suggestions/apply", json={"kind": "unwhite", "rule_id": rid})
        assert r.status_code == 200, r.text
        assert rid not in {x["id"] for x in store.list_key_rules(kind="white")}
    finally:
        try:
            store.remove_key_rule(rid)
        except Exception:
            pass
        store.delete_api_key(kid)

def test_blacklist_block_keeps_key_identity():
    # 拉黑的 key 仍是登记身份：403 明细要写 key_name/key_id，别在 KEY 统计里落成 "(unknown)"
    import time as _time
    from src.gateway.admin_store import get_admin_store
    store = get_admin_store()
    plain = "sk_black_%d" % int(_time.time() * 1000)
    name = "pytest-blacklist"
    kid = store.create_api_key(plain, name, "", "pytest blacklist attribution")["id"]
    rid = store.add_key_rule(kind="black", key_value=plain, note="pytest")["id"]
    try:
        r = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {plain}"},
                        json={"model": "vllm_local/qwen2.5:7b",
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 403, r.text[:200]
        groups = {g["label"]: g for g in store.stats_group("2000-01-01 00:00:00", "2099-01-01 00:00:00", "key_name")}
        assert groups[name]["blocked"] >= 1, groups
    finally:
        store.remove_key_rule(rid)
        store.delete_api_key(kid)
        with store._mem_lock:  # 清掉本用例写入的明细行（共享 memory store）
            store._logs[:] = [x for x in store._logs if x.get("key_name") != name]


def test_unregistered_key_401_not_logged_as_traffic():
    # 鉴权层 401 没有身份/内容，不进 request_log（否则 KEY 榜出现 (unknown) 且算 allow）
    import time as _time
    from src.gateway.admin_store import get_admin_store
    store = get_admin_store()
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer sk_unregistered_probe"},
                    json={"model": "vllm_local/qwen2.5:7b",
                          "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401, r.text[:200]
    _time.sleep(0.2)  # 明细是 fire-and-forget（executor 线程），留一拍再看
    with store._mem_lock:
        assert not [x for x in store._logs if x.get("status_code") == 401]


def test_provider_group_excludes_unrouted():
    # 空 provider = 没到上游（拦截/未路由）：整行不进 provider 调用分布
    from src.gateway.admin_store import get_admin_store
    store = get_admin_store()
    since, until = "2000-01-01 00:00:00", "2099-01-01 00:00:00"
    before = {g["label"]: g["calls"] for g in store.stats_group(since, until, "provider")}
    store.insert_request_log({"provider": "", "action": "block", "status_code": 403,
                              "key_name": "pytest-prov", "blocked_reason": "key_blacklist"})
    store.insert_request_log({"provider": "vllm_local", "action": "route_local", "status_code": 200,
                              "key_name": "pytest-prov"})
    try:
        after = {g["label"]: g["calls"] for g in store.stats_group(since, until, "provider")}
        assert "" not in after and "未到上游" not in after and "(unknown)" not in after, after
        assert after.get("vllm_local", 0) == before.get("vllm_local", 0) + 1, (before, after)
        summ = {g["label"]: g["calls"] for g in store.stats_summary(since, until, top=20)["group_provider"]}
        assert summ.get("vllm_local", 0) == before.get("vllm_local", 0) + 1, summ
    finally:
        with store._mem_lock:
            store._logs[:] = [x for x in store._logs if x.get("key_name") != "pytest-prov"]


def test_audit_search_matches_chinese_rule_name():
    # 界面显示中文、库里存英文标识：搜「L2 判密」要能命中 rule=l2_confidential
    from src.gateway.audit_store import get_audit_store
    store = get_audit_store()
    entry = {"time": "2026-10-20 00:00:00", "ts": "2026-10-20 00:00:00", "type": "chat",
             "action": "route_local", "rule": "l2_confidential", "text_preview": "搜索用样本",
             "client_ip": "127.0.0.1", "provider": "vllm_local", "model": "vllm_local/qwen2.5:7b"}
    store.append(entry)
    try:
        for q in ("判密", "L2 判密", "l2_confidential", "系统内置"):
            d = client.get("/admin/audit/entries", params={"q": q}).json()
            items = d.get("entries") or d.get("items") or []
            assert any(e.get("text_preview") == "搜索用样本" for e in items), (q, d)
    finally:
        with store._mem_lock:
            store._mem[:] = [e for e in store._mem if e.get("text_preview") != "搜索用样本"]


def test_suggest_add_limit_and_apply(tmp_path, monkeypatch):
    # 规则7：调用量 Top 的 (provider,model) 未配限流则建议一键加限流；应用后建议消失
    import yaml
    from src.gateway import model_policy as _mp
    from src.gateway.admin_store import get_admin_store
    mp_path = tmp_path / "model_policy.yaml"
    mp_path.write_text(yaml.safe_dump({"model_policy": {
        "external_candidates": [], "internal_models": [], "model_limits": [],
        "review_model": {"threshold": 0.7}}}, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_MODEL_POLICY_PATH", str(mp_path))
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_LIMIT_MIN_CALLS", "5")
    _mp.invalidate_model_policy_cache()
    store = get_admin_store()
    try:
        # 100 行：压住同 session 其他用例的残留行，稳坐 Top1
        for _ in range(100):
            store.insert_request_log({"key_name": "pytest-limit", "action": "allow",
                                      "model": "hot-model", "provider": "hot-prov",
                                      "client_ip": "9.9.9.9"})
        items = client.get("/admin/api/suggestions", params={"hours": 168}).json()["items"]
        lim = [s for s in items if (s.get("apply") or {}).get("kind") == "add_limit"]
        # 规则7 最多报 max_per_rule 个模型 ⇒ 按模型名定位，别假设只有一条
        hit = [s for s in lim if s["apply"]["limit_model"] == "hot-prov/hot-model"]
        assert hit, items
        ap = hit[0]["apply"]
        assert ap["limit_rpm"] > 0
        r = client.post("/admin/api/suggestions/apply", json={"kind": "add_limit", **ap})
        assert r.status_code == 200, r.text
        assert _mp.model_rpm("hot-prov", "hot-model") == ap["limit_rpm"]
        items2 = client.get("/admin/api/suggestions", params={"hours": 168}).json()["items"]
        lim2 = [s for s in items2 if (s.get("apply") or {}).get("kind") == "add_limit"]
        assert all(s["apply"]["limit_model"] != "hot-prov/hot-model" for s in lim2), items2
    finally:
        _mp.invalidate_model_policy_cache()


def test_suggest_add_limit_rate_is_peak_based(tmp_path, monkeypatch):
    # 规则7 速率口径：平均 = 窗口摊平（÷60，旧算式 ×60 把平均放大 3600 倍）；
    # 限流建议按**峰值分钟 ×2**（突发流量才是限流依据）
    import time

    import yaml
    from src.gateway import model_policy as _mp
    from src.gateway.admin_store import get_admin_store
    mp_path = tmp_path / "model_policy.yaml"
    mp_path.write_text(yaml.safe_dump({"model_policy": {
        "external_candidates": [], "internal_models": [], "model_limits": [],
        "review_model": {"threshold": 0.7}}}, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_MODEL_POLICY_PATH", str(mp_path))
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_LIMIT_MIN_CALLS", "5")
    _mp.invalidate_model_policy_cache()
    store = get_admin_store()
    try:
        # 120 次：两个分钟各 60 次（窗口 1h）→ 峰值分钟 60、平均 120/60=2.00/分钟
        for off, n in ((90, 60), (150, 60)):
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - off))
            for i in range(n):
                store.insert_request_log({"key_name": "pytest-peak", "action": "allow",
                                          "model": "peak-model", "provider": "peak-prov",
                                          "client_ip": "8.8.8.8", "ts": ts})
        items = client.get("/admin/api/suggestions", params={"hours": 1}).json()["items"]
        lim = [s for s in items if (s.get("apply") or {}).get("kind") == "add_limit"]
        # 规则7 现在最多报 max_per_rule 个模型 ⇒ 按模型名定位，别假设只有一条
        hit = [s for s in lim if s["apply"]["limit_model"] == "peak-prov/peak-model"]
        assert hit, items
        s = hit[0]
        assert s["apply"]["limit_rpm"] == 120, s          # 峰值 60 × 2（默认倍数）
        assert "峰值 60/分钟" in s["message"], s
        assert "平均 2.00/分钟" in s["message"], s
        assert "7200" not in s["message"], s              # 旧算式（×60）的放大值不该再出现
        # AND 条件二：峰值下限更高 → 不再建议（累计够但没有分钟级突发）
        monkeypatch.setenv("AI_GATEWAY_SUGGEST_LIMIT_MIN_PEAK", "1000")
        items2 = client.get("/admin/api/suggestions", params={"hours": 1}).json()["items"]
        assert not [s for s in items2 if (s.get("apply") or {}).get("kind") == "add_limit"
                    and s["apply"]["limit_model"] == "peak-prov/peak-model"], items2
        # 0 = 关闭该条件 → 恢复建议
        monkeypatch.setenv("AI_GATEWAY_SUGGEST_LIMIT_MIN_PEAK", "0")
        items3 = client.get("/admin/api/suggestions", params={"hours": 1}).json()["items"]
        assert [s for s in items3 if (s.get("apply") or {}).get("kind") == "add_limit"], items3
        # 累计调用数条件也能单独关闭（0 = 该条件不参与判断，与峰值条件语义一致）
        monkeypatch.setenv("AI_GATEWAY_SUGGEST_LIMIT_MIN_CALLS", "1000")
        items4 = client.get("/admin/api/suggestions", params={"hours": 1}).json()["items"]
        assert not [s for s in items4 if (s.get("apply") or {}).get("kind") == "add_limit"
                    and s["apply"]["limit_model"] == "peak-prov/peak-model"], items4
        monkeypatch.setenv("AI_GATEWAY_SUGGEST_LIMIT_MIN_CALLS", "0")
        items5 = client.get("/admin/api/suggestions", params={"hours": 1}).json()["items"]
        assert [s for s in items5 if (s.get("apply") or {}).get("kind") == "add_limit"
                and s["apply"]["limit_model"] == "peak-prov/peak-model"], items5
    finally:
        _mp.invalidate_model_policy_cache()


def test_suggest_caps_per_rule_and_total(monkeypatch):
    # 条数上限：每规则 N 条 + 整卡 N 条（severity 高的先留）
    # 次数类阈值会按窗口放大 ⇒ 用量观察基准压到 1，保证 7 天窗（×7=7）也够得着
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD", "1")
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_MAX_PER_RULE", "1")
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_MAX_TOTAL", "1")
    items = client.get("/admin/api/suggestions", params={"hours": 168}).json()["items"]
    assert len(items) == 1, items
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_MAX_TOTAL", "2")
    items2 = client.get("/admin/api/suggestions", params={"hours": 168}).json()["items"]
    assert 1 <= len(items2) <= 2, items2
    types = [s.get("type") for s in items2]
    assert len(types) == len(set(types)), items2      # 每规则最多 1 条


def test_suggest_run_state_rules(monkeypatch):
    # 运行态维度规则：延迟 P95 / 本地路由占比 / token 突增（各自 0 = 关闭）
    from src.gateway.admin_store import get_admin_store
    store = get_admin_store()
    rows = ([{"action": "route_local", "duration_ms": 5000, "prompt_tokens": 10,
              "completion_tokens": 10, "provider": "p", "model": "m", "ts": "2026-01-01 00:00:00"}] * 3
            + [{"action": "allow", "duration_ms": 10, "prompt_tokens": 1,
                "completion_tokens": 1, "provider": "p", "model": "m", "ts": "2026-01-01 00:00:00"}])
    monkeypatch.setattr(store, "_log_rows", lambda since="", until="": rows)
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_LATENCY_P95_MS", "1000")
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_DEGRADE_RATE", "0.7")
    items = client.get("/admin/api/suggestions", params={"hours": 24}).json()["items"]
    by_type = {s.get("type"): s for s in items}
    assert "latency_high" in by_type, items
    assert "P95 5000ms" in by_type["latency_high"]["metric"], by_type["latency_high"]
    assert "degrade_rate" in by_type, items
    assert "75%" in by_type["degrade_rate"]["metric"], by_type["degrade_rate"]
    # 阈值置 0 = 关闭对应规则
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_LATENCY_P95_MS", "0")
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_DEGRADE_RATE", "0")
    items2 = client.get("/admin/api/suggestions", params={"hours": 24}).json()["items"]
    assert not [s for s in items2 if s.get("type") in ("latency_high", "degrade_rate")], items2


def test_suggest_token_growth(monkeypatch):
    # 规则10：token 用量相对上一等长窗口突增
    import time as _t
    from src.gateway.admin_store import get_admin_store
    store = get_admin_store()
    now = _t.time()
    cur_since = _t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime(now - 3600))
    cur = [{"action": "allow", "duration_ms": 10, "prompt_tokens": 1000,
            "completion_tokens": 1000, "provider": "p", "model": "m", "ts": cur_since}]
    prev = [{"action": "allow", "duration_ms": 10, "prompt_tokens": 10,
             "completion_tokens": 10, "provider": "p", "model": "m", "ts": "2026-01-01 00:00:00"}]

    def fake_rows(since="", until=""):
        return cur if (since or "") >= cur_since else prev

    monkeypatch.setattr(store, "_log_rows", fake_rows)
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_TOKEN_GROWTH", "3")
    items = client.get("/admin/api/suggestions", params={"hours": 1}).json()["items"]
    sp = [s for s in items if s.get("type") == "token_spike"]
    assert sp, items
    assert "100.0 倍" in sp[0]["metric"], sp[0]


def test_suggest_threshold_scales_with_window(monkeypatch):
    # 次数类阈值：只配一个 24h 基准值，各窗口按长度等比放大；比率/速率类不放大
    from src.gateway import stats_service as _st
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD", "20")
    th = _st._thresholds()
    assert th["violation_base"] == 20
    assert _st._scaled(th["violation_base"], 24) == 20
    assert _st._scaled(th["violation_base"], 168) == 140
    assert _st._scaled(th["violation_base"], 720) == 600
    # meta 回传的是**当前窗口**的有效值（UI 提示用）
    assert _st.suggestion_meta(24)["thresholds"]["violation"] == 20
    assert _st.suggestion_meta(168)["thresholds"]["violation"] == 140
    assert _st.suggestion_meta(720)["thresholds"]["violation"] == 600
    # 比率类（异常处置率）与限流建议的两个条件不随窗口放大
    monkeypatch.setenv("AI_GATEWAY_KEYTIER_VIOLATION_RATE", "0.3")
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_LIMIT_MIN_CALLS", "1000")
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_LIMIT_MIN_PEAK", "20")
    th2 = _st._thresholds()
    assert th2["kt_violation_rate"] == 0.3
    assert th2["limit_min_calls"] == 1000
    assert th2["limit_min_peak"] == 20
    # 历史分档形态按 24h 档当基准（老配置不用手改）
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD", "24:20,168:100,720:500")
    assert _st._thresholds()["violation_base"] == 20


def test_l2_gray_floor_hot_reload(monkeypatch):
    # 灰区下限改成每次读 env（即时生效），且上限钉 20（再高则 25/30 档关键词单命中不进灰区）
    from src.gateway import main as _main
    monkeypatch.setenv("AI_GATEWAY_L2_GRAY_FLOOR", "20")
    assert _main._l2_gray_floor() == 20
    assert _main._l2_gray_trigger({"risk_score": 20}) is True
    assert _main._l2_gray_trigger({"risk_score": 19}) is False
    monkeypatch.setenv("AI_GATEWAY_L2_GRAY_FLOOR", "30")
    assert _main._l2_gray_floor() == 20          # 超过 20 一律回落 20
    monkeypatch.setenv("AI_GATEWAY_L2_GRAY_FLOOR", "0")
    assert _main._l2_gray_floor() == 0
    assert _main._l2_gray_trigger({"risk_score": 0}) is True


def test_legacy_window_map_reads_24h_base(monkeypatch):
    # 历史分档形态 '24:20,168:100,720:500' 只用于读**24h 档当基准**（其余档按窗口长度自动算）
    from src.gateway import stats_service as ss
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD", "24:20,168:100,720:500")
    m = ss._window_thresholds("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD", "AI_GATEWAY_SUGGEST_BLOCK_THRESHOLD", 20)
    assert m == {24: 20, 168: 100, 720: 500}
    assert ss._base_threshold("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD",
                              "AI_GATEWAY_SUGGEST_BLOCK_THRESHOLD", 20) == 20
    # 纯数字 = 基准值
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD", "33")
    assert ss._window_thresholds("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD",
                                  "AI_GATEWAY_SUGGEST_BLOCK_THRESHOLD", 20) == {"*": 33}
    assert ss._base_threshold("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD",
                              "AI_GATEWAY_SUGGEST_BLOCK_THRESHOLD", 20) == 33
    # 非法 → 回落默认
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD", "abc")
    assert ss._window_thresholds("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD",
                                  "AI_GATEWAY_SUGGEST_BLOCK_THRESHOLD", 20) == {"*": 20}
    assert ss._base_threshold("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD",
                              "AI_GATEWAY_SUGGEST_BLOCK_THRESHOLD", 20) == 20

def test_suggest_effective_thresholds_per_rule_window(monkeypatch):
    # 钉死各规则在自己窗口下的**有效阈值**（次数类 = 基准 × 窗口/24）：
    # 基准值改动会同步移动这些数字，别让下一次改动悄悄挪走触发线
    from src.gateway import stats_service as _st
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD", "20")
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD", "1000")
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_ERROR_MIN_CALLS", "30")
    monkeypatch.setenv("AI_GATEWAY_KEYTIER_MIN_CALLS_BLACK", "30")
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_REORDER_MIN_HITS", "10")
    monkeypatch.setenv("AI_GATEWAY_KEYTIER_WINDOW_HOURS", "720")
    th = _st._thresholds()
    # 规则 1/2/3：面板窗口
    assert _st._scaled(th["violation_base"], 24) == 20
    assert _st._scaled(th["violation_base"], 168) == 140
    assert _st._scaled(th["violation_base"], 720) == 600
    assert _st._scaled(th["volume_base"], 168) == 7000
    assert _st._scaled(th["error_min_calls_base"], 168) == 210
    # 规则 0：KEY 分级窗口（720h）
    assert _st._scaled(th["kt_black_calls_base"], th["kt_window_hours"]) == 900
    # 规则 4：规则窗口（0 = 跟随 KEY 分级窗口）
    assert _st._scaled(th["reorder_min_hits_base"], _st._rule_window_hours(th)) == 300
    # 窗口短于 24h 不缩小（沿用基准值，避免"1 次事件就建议拉黑"）
    assert _st._scaled(20, 1) == 20
    assert _st._scaled(20, 0) == 20

def test_l2_gray_trigger_no_upper_bound_on_risk():
    # 回归（2026-09-30）：灰区上界曾是 `risk_score < 60`，但 risk_score 混了关键词分，
    # 而唯一按分值判的 policy 规则读的是 weighted_pii_score（不含关键词分）。
    # => 「关键词堆叠到 >=60」既超灰区上界（L2 不触发）又匹配不上 priority 5（L1 放行），
    #    两条路都漏，内容直出外网。实测「工资 薪资 奖金」kw=60 直出 deepseek，
    #    而单词「机密」kw=30 转内网 —— 堆得越多越危险。
    # 现在只保留下限，上界交给 policy 的 action != allow 承担。
    from src.gateway.inspection import inspect_text
    from src.gateway.main import _l2_gray_trigger

    # 关键词堆叠到 >=60：必须仍进 L2（这是本测试的核心断言）
    for t in ("工资 薪资 奖金", "机密 保密 绝密 不得外传", "预算 决算 合同 投标 中标 报价 单价"):
        f = inspect_text(t)
        assert f["weighted_pii_score"] == 0, f"{t}: 用例前提失效（竟有 PII）"
        assert f["risk_score"] >= 60, f"{t}: 用例前提失效（risk 未过 60）"
        assert _l2_gray_trigger(f) is True, f"{t}: 关键词堆叠未进 L2 门 —— 内容会出境"

    # 低于下限的正常流量不进 L2（避免无谓开销）
    for t in ("总结CAP定理", "今天天气如何", "报销流程怎么走"):
        assert _l2_gray_trigger(inspect_text(t)) is False, f"{t}: 正常流量被误送 L2"

    # 边界：恰好等于下限要进
    assert _l2_gray_trigger({"risk_score": 20}) is True
    assert _l2_gray_trigger({"risk_score": 19}) is False

    # PII>=60 不依赖灰区：priority 5 已判 route_local，门的 action 条件会先拦下。
    # 这条断言防止有人「顺手」把上界加回来（对 PII 是死代码，对关键词是漏洞）。
    from src.gateway.policy import load_policy, decide
    f = inspect_text("我的身份证110101199003072519")
    assert f["weighted_pii_score"] >= 60
    assert decide(load_policy(), {"text": "x", "file": {}, "findings": f,
                                  "session": {}, "tainted_file": {}}).action == "route_local"

def test_l1_rules_catch_obfuscated_negative_samples():
    # 回归（2026-10-08）：160 条矩阵里 7 条机密样本 L1 risk=0，L2 压根不被调用，直接出境。
    # 网关门逻辑不动，只补 L1 特征（RISK_RULES，各 20 档 = 恰好落灰区下界交 L2 判密）。
    from src.gateway.inspection import RISK_RULES, inspect_text
    from src.gateway.main import _l2_gray_trigger

    negatives = [
        "听说奚霞涨到38000了,真的假的",
        "那个数你懂的,奚霞那个38后面加个k",
        "上季度的那个数,11后面六个零,你懂的",
        "图上那个数,C后面35,你懂的",
        "会上定的那个月数,2加13,你懂的",
        "我 g 资 daoshou 12k,惨",
        "确认下:老板周三下午3点见红杉的人,行程没变吧",
    ]
    for t in negatives:
        f = inspect_text(t)
        assert f["risk_score"] >= 20, f"{t}: L1 仍零特征 —— 会直出外网"
        assert _l2_gray_trigger(f) is True, f"{t}: 未进 L2 门"

    # 规则各只计一次，不因重复出现放大
    assert inspect_text(negatives[1])["keyword_hits"].count("implied_amount") == 1

    # pass 侧不被误伤进直转本地；只与"行程"同现、无职务的不算高管行程
    for t in ("行程单私发我,别发群里", "工资一般是每月几号发放,遇到节假日会提前吗",
              "报价的事会上定,现在不说", "总结一下这段公开财报:营收同比增长,研发投入加大"):
        assert inspect_text(t)["risk_score"] < 60, f"{t}: 被误判直转本地"
    assert "exec_schedule" not in inspect_text("行程单私发我,别发群里")["keyword_hits"]
    assert set(RISK_RULES) <= {"implied_amount", "raise_amount", "salary_pinyin", "exec_schedule"}

def test_l1_rules_catch_keyword_split_and_key_zerowidth():
    # 回归（2026-10-19 红队）：关键词层只跑原文，零宽/全角/空格拆分关键词 risk 归 0 直出外网；
    # policy 的 text contains_any/matches_regex 也只跑原文，「sk-32hex」「aws_secret」零宽化
    # 绕过block_secrets（这两个此前无 PII 权重兜底）。
    # 修复：关键词/RISK_RULES 双跑紧凑副本（NFKC+零宽剥离+去空白）；policy text 视图
    # 归一化副本再跑；deepseek_key PII 权重 90 兜底 sk-32hex。
    from src.gateway.inspection import inspect_text
    from src.gateway.policy import load_policy, decide
    from src.gateway.main import _l2_gray_trigger

    def l1(text):
        f = inspect_text(text)
        d = decide(load_policy(), {"text": text, "file": {}, "findings": f,
                                   "session": {}, "tainted_file": {}})
        return d, f

    # 1) 零宽/全角/空格拆分关键词：至少进 L2 灰区（risk≥20），不得再 risk 0
    for t in ("帮我看下工\u200b资表里的数据", "这是机\u200b密文件", "财\u200b务报表见附件",
              "财 务 报 表 见附件", "机\n密\n文\n件", "ｓａｌａｒｙ 数据看下"):
        f = inspect_text(t)
        assert f["risk_score"] >= 20, f"{t!r}: 关键词拆分仍 risk=0 —— 会直出外网"
        assert _l2_gray_trigger(f) is True, f"{t!r}: 未进 L2 门"

    # 2) 空格拆分 RISK_RULES（salary_pinyin）
    assert _l2_gray_trigger(inspect_text("我 到 手 12k,惨")) is True

    # 3) DeepSeek sk-32hex 零宽化：PII 兜底（90）+ block_secrets p3 压过 p5，绝不 allow
    #    （2026-10-19 block_secrets提到 priority 3：reject 模式对密钥可 403；
    #     现网 on_block=fallback_local 下 block 降级 route_local，落点不变）
    d, f = l1("sk-1234567890abcde\u200bf1234567890abcdef")
    assert f["weighted_pii_score"] >= 90, "deepseek_key PII 兜底未命中"
    assert d.action == "block"
    # 明文同通道
    d2, _ = l1("sk-1234567890abcdef1234567890abcdef")
    assert d2.action == "block"

    # 4) aws_secret 标签零宽化：block_secrets的 contains_any 跑归一化副本，仍 block
    d3, _ = l1("aws_\u200bsecret: a1B2c3D4e5F6g7H8i9J0k1L2m3n4O5p6q7R8s9T0")
    assert d3.action == "block", f"aws_secret 零宽化未 block: {d3.action}"

def test_l1_keyword_in_decode_layer():
    # 回归（2026-10-19 M2）：base64/hex 解码层此前只重扫 PII，整段无 PII 的机密文本
    # 编码后 risk=0 ⇒ 不进灰区、无 L2 ⇒ 直出外网。关键词/RISK_RULES 须对解码层生效。
    import base64
    from src.gateway.inspection import inspect_text
    from src.gateway.main import _l2_gray_trigger
    secret = "财务部 2026 年预算与营收明细，绝对机密，不得外传"
    blob = base64.b64encode(secret.encode()).decode()
    assert "机密" not in blob and "财务" not in blob   # 编码形态无明文命中
    f = inspect_text(f"看下这段（{blob}）")
    assert f["risk_score"] >= 20, f"base64 关键词层未命中: {f}"
    assert _l2_gray_trigger(f) is True
    # hex 同口径（hex 串也命中 b64 run，b64 解出非 UTF-8 后落 hex 层）
    f2 = inspect_text("看下这段（" + secret.encode().hex() + "）")
    assert f2["risk_score"] >= 20, f"hex 关键词层未命中: {f2}"
    # 原文与编码同现：同名关键词跨层只计一次
    b1 = base64.b64encode("机密内部资料".encode()).decode()
    both = inspect_text("机密 " + b1)
    assert both["keyword_hits"].count("机密") == 1, both
    # 解码层独有的命中必须直接 route_local：生产实测 L2 看 base64 判 NORMAL 放行
    from src.gateway.main import _encoded_keyword_decision
    assert f["encoded_keyword_hits"], f
    assert _encoded_keyword_decision(f)["action"] == "route_local"
    fp = inspect_text("机密文件")           # 原文命中仍走 L2 灰区，不抢判定权
    assert not fp["encoded_keyword_hits"]
    assert _encoded_keyword_decision(fp) is None

def test_l2_config_suggest_window_roundtrip(tmp_path, monkeypatch):
    # /l2-config：次数类阈值只存**基准值**（24h 口径）；PUT 传 dict（历史分档形态）取 24h 档，
    # 其余窗口由系统按窗口长度自动换算（stats_service._scaled）
    import os
    from src.gateway import stats_service as _st
    monkeypatch.setenv("AI_GATEWAY_L2_OVERRIDES_PATH", str(tmp_path / "ov.yaml"))
    saved = {k: os.environ.get(k) for k in
             ("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD", "AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD")}
    try:
        r = client.put("/admin/api/l2-config", json={
            "AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD": {"24": 20, "168": 100, "720": 500},
            "AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD": {"24": 100, "168": 2000, "720": 9000},
        })
        assert r.status_code == 200, r.text
        # 落 env 的是基准值（24h 档），不是分档表
        assert os.environ["AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD"] == "20"
        assert os.environ["AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD"] == "100"
        d = client.get("/admin/api/l2-config").json()
        assert d["AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD"] == 20
        assert d["AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD"] == 100
        # 各窗口的有效值 = 基准 × 窗口/24（自动换算）
        assert _st._scaled(20, 24) == 20
        assert _st._scaled(20, 168) == 140
        assert _st._scaled(20, 720) == 600
        # 越界 422
        r2 = client.put("/admin/api/l2-config", json={"AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD": {"24": 0}})
        assert r2.status_code == 422
        # 纯数字仍兼容（= 基准值）
        r3 = client.put("/admin/api/l2-config", json={"AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD": 7})
        assert r3.status_code == 200 and os.environ["AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD"] == "7"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

def test_l2_config_suggest_all_thresholds_roundtrip(tmp_path, monkeypatch):
    # /l2-config：建议引擎其余 10 项阈值也可读写并落盘（2026-10 补齐，原只能改 env 文件）
    import os
    import yaml
    monkeypatch.setenv("AI_GATEWAY_L2_OVERRIDES_PATH", str(tmp_path / "ov.yaml"))
    keys = ("AI_GATEWAY_SUGGEST_ERROR_MIN_CALLS", "AI_GATEWAY_KEYTIER_MIN_CALLS_BLACK",
            "AI_GATEWAY_KEYTIER_MIN_VIOLATIONS", "AI_GATEWAY_KEYTIER_VIOLATION_RATE",
            "AI_GATEWAY_KEYTIER_MIN_CALLS_WHITE", "AI_GATEWAY_KEYTIER_REVOKE_VIOLATIONS",
            "AI_GATEWAY_KEYTIER_WINDOW_HOURS", "AI_GATEWAY_SUGGEST_REORDER_MIN_HITS",
            "AI_GATEWAY_SUGGEST_LIMIT_MIN_CALLS", "AI_GATEWAY_SUGGEST_SILENT_GRACE_DAYS")
    body = {"AI_GATEWAY_SUGGEST_ERROR_MIN_CALLS": 50, "AI_GATEWAY_KEYTIER_MIN_CALLS_BLACK": 30,
            "AI_GATEWAY_KEYTIER_MIN_VIOLATIONS": 7, "AI_GATEWAY_KEYTIER_VIOLATION_RATE": 0.4,
            "AI_GATEWAY_KEYTIER_MIN_CALLS_WHITE": 200, "AI_GATEWAY_KEYTIER_REVOKE_VIOLATIONS": 3,
            "AI_GATEWAY_KEYTIER_WINDOW_HOURS": 240, "AI_GATEWAY_SUGGEST_REORDER_MIN_HITS": 15,
            "AI_GATEWAY_SUGGEST_LIMIT_MIN_CALLS": 500, "AI_GATEWAY_SUGGEST_SILENT_GRACE_DAYS": 7}
    saved = {k: os.environ.get(k) for k in keys}
    try:
        r = client.put("/admin/api/l2-config", json=body)
        assert r.status_code == 200, r.text
        assert os.environ["AI_GATEWAY_SUGGEST_ERROR_MIN_CALLS"] == "50"
        assert os.environ["AI_GATEWAY_KEYTIER_VIOLATION_RATE"] == "0.4"
        d = client.get("/admin/api/l2-config").json()
        for k, v in body.items():
            assert d[k] == v, (k, d[k], v)
        # 落盘走 l2_overrides 白名单（ALLOWED_KEYS 已同步），重启后由 apply_overrides 回灌
        data = yaml.safe_load((tmp_path / "ov.yaml").read_text(encoding="utf-8"))
        assert data["AI_GATEWAY_KEYTIER_WINDOW_HOURS"] == 240
        assert data["AI_GATEWAY_KEYTIER_VIOLATION_RATE"] == 0.4
        # 越界 422
        assert client.put("/admin/api/l2-config",
                          json={"AI_GATEWAY_KEYTIER_VIOLATION_RATE": 2}).status_code == 422
        assert client.put("/admin/api/l2-config",
                          json={"AI_GATEWAY_SUGGEST_ERROR_MIN_CALLS": 0}).status_code == 422
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

def test_normalize_usage_cached():
    # cached/creation 提取：OpenAI details / Anthropic / 缺失 / 超 prompt 钳制
    from src.gateway.main import _normalize_usage as nu
    assert nu({"prompt_tokens": 1000, "completion_tokens": 200,
               "prompt_tokens_details": {"cached_tokens": 300}}) == (1000, 200, 300, 0)
    assert nu({"input_tokens": 500, "output_tokens": 50, "cache_read_input_tokens": 100,
               "cache_creation_input_tokens": 60}) == (500, 50, 100, 60)
    assert nu({"prompt_tokens": 100, "completion_tokens": 10}) == (100, 10, 0, 0)
    assert nu({"prompt_tokens": 100, "completion_tokens": 10,
               "prompt_tokens_details": {"cached_tokens": 999}}) == (100, 10, 100, 0)
    assert nu(None) == (0, 0, 0, 0)

def test_billing_endpoint(tmp_path, monkeypatch):
    # 计费：缓存档/全价回退/未定价三行 + 合计
    import yaml
    from src.gateway import model_policy as _mp
    from src.gateway.admin_store import get_admin_store
    mp_path = tmp_path / "model_policy.yaml"
    mp_path.write_text(yaml.safe_dump({"model_policy": {
        "external_candidates": [
            {"provider": "hot-prov", "model": "hot-model",
             "price_per_1m_in": 2.0, "price_per_1m_out": 8.0, "price_per_1m_cached": 0.5},
            {"provider": "p2", "model": "m2",
             "price_per_1m_in": 4.0, "price_per_1m_out": 4.0},
        ],
        "internal_models": [], "model_limits": [],
        "review_model": {"threshold": 0.7}}}, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_MODEL_POLICY_PATH", str(mp_path))
    _mp.invalidate_model_policy_cache()
    store = get_admin_store()
    seed = lambda prov, m, p, c, h, w=0: store.insert_request_log(
        {"key_name": "pytest-bill", "action": "allow", "model": m, "provider": prov,
         "client_ip": "9.9.9.9", "prompt_tokens": p, "completion_tokens": c,
         "cached_tokens": h, "cache_creation_tokens": w})
    seed("hot-prov", "hot-model", 1000000, 500000, 200000, 100000)
    seed("p2", "m2", 100000, 0, 50000)
    seed("noprov", "nomodel", 1000, 1000, 0)
    try:
        d = client.get("/admin/api/stats/billing", params={"hours": 168}).json()
        by = {(g["key"], g["model"]): g for g in d["items"]}
        a = by[("pytest-bill", "hot-prov/hot-model")]
        assert a["cost"] == 5.9 and a["creation"] == 100000, a
        assert a["priced"] and not a["cached_at_full"], a
        b = by[("pytest-bill", "p2/m2")]
        assert b["cost"] == 0.4 and b["cached_at_full"], b
        u = by[("pytest-bill", "noprov/nomodel")]
        assert not u["priced"] and u["cost"] == 0, u
        assert d["total_cost"] == 6.3, d
    finally:
        _mp.invalidate_model_policy_cache()

def test_model_spend_endpoint(tmp_path, monkeypatch):
    # 推荐等级数据源：/model-policy/spend 按模型实际花费（计费口径，KEY×模型行按模型求和）
    import yaml
    from src.gateway import model_policy as _mp
    from src.gateway.admin_store import get_admin_store
    mp_path = tmp_path / "model_policy.yaml"
    mp_path.write_text(yaml.safe_dump({"model_policy": {
        "external_candidates": [
            {"provider": "spend-prov", "model": "spend-model",
             "price_per_1m_in": 2.0, "price_per_1m_out": 8.0},
        ],
        "internal_models": [], "model_limits": [],
        "review_model": {"threshold": 0.7}}}, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_MODEL_POLICY_PATH", str(mp_path))
    _mp.invalidate_model_policy_cache()
    store = get_admin_store()
    try:
        # 唯一 key/model：同 session 共享 memory store，防其他测试种子行串扰
        store.insert_request_log({"key_name": "pytest-spend-unique", "action": "allow",
                                  "model": "spend-model", "provider": "spend-prov",
                                  "client_ip": "9.9.9.9",
                                  "prompt_tokens": 1000000, "completion_tokens": 500000,
                                  "cached_tokens": 0, "cache_creation_tokens": 0})
        d = client.get("/admin/api/model-policy/spend").json()
        assert d["hours"] == 168
        m = d["models"]["spend-prov/spend-model"]
        # 1M×2 + 0.5M×8 = ¥6；有效费率 = 6/1.5M×1e6 = 4.0
        assert m["tokens"] == 1500000 and m["cost"] == 6.0, m
        assert m["rate_per_1m"] == 4.0, m
    finally:
        _mp.invalidate_model_policy_cache()

def test_billing_daily_split(tmp_path, monkeypatch):
    # 按天拆分：UTC 16:00 为北京日界（15:00 UTC=北京当日，16:30 UTC=北京次日）
    import yaml
    from src.gateway import model_policy as _mp
    from src.gateway.admin_store import get_admin_store
    mp_path = tmp_path / "model_policy.yaml"
    mp_path.write_text(yaml.safe_dump({"model_policy": {
        "external_candidates": [
            {"provider": "bill-daily", "model": "dm",
             "price_per_1m_in": 2.0, "price_per_1m_out": 8.0, "price_per_1m_cached": 0.5}],
        "internal_models": [], "model_limits": [],
        "review_model": {"threshold": 0.7}}}, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_MODEL_POLICY_PATH", str(mp_path))
    _mp.invalidate_model_policy_cache()
    store = get_admin_store()
    from datetime import datetime, timedelta, timezone
    base = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
        hour=15, minute=0, second=0, microsecond=0)
    d1 = (base + timedelta(hours=8)).strftime("%Y-%m-%d")      # UTC 15:00 → 北京当日
    d2 = (base + timedelta(hours=8, minutes=90)).strftime("%Y-%m-%d")  # UTC 16:30 → 北京次日
    seed = lambda ts, p, c, h: store.insert_request_log(
        {"key_name": "pytest-bill-daily", "action": "allow", "model": "dm",
         "provider": "bill-daily", "client_ip": "9.9.9.8", "prompt_tokens": p,
         "completion_tokens": c, "cached_tokens": h, "ts": ts})
    seed(base.strftime("%Y-%m-%d 15:00:00"), 1000000, 500000, 200000)
    seed(base.strftime("%Y-%m-%d 16:30:00"), 100000, 100000, 0)
    try:
        d = client.get("/admin/api/stats/billing", params={"hours": 72}).json()
        assert "daily" in d and "daily_total" in d, d
        mine = {g["day"]: g for g in d["daily"] if g["model"] == "bill-daily/dm"}
        assert set(mine) == {d1, d2}, mine
        assert mine[d1]["cost"] == 5.7 and mine[d1]["cached"] == 200000, mine
        assert mine[d2]["cost"] == 1.0 and mine[d2]["calls"] == 1, mine
        assert d["daily_total"] >= 6.7, d  # 含同窗口其他行，只断言下限
    finally:
        _mp.invalidate_model_policy_cache()

def test_audit_entries_show_key_name():
    # 审计列表调用方显示登记名：token=名称也能过滤命中
    from src.gateway.admin_store import mask_key
    r = client.post("/v1/chat/completions", headers=H, json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "审计名称回归"}]})
    assert r.status_code == 200
    d = client.get("/admin/audit/entries", params={"token": "pytest"}).json()
    assert d["entries"], "token=pytest 应按调用方名称命中"
    assert any(e.get("key_name") == "pytest" and e.get("token_masked") == mask_key(KEY) for e in d["entries"])

def test_audit_entries_filter_and_time_window_are_and():
    # 回归：动作筛选 + 时间窗必须叠加（早前时间窗分支从 all_entries 重算，
    # 把 action/rule 过滤整个冲掉 → 筛「本地路由」却看到放行行）
    import time
    from src.gateway.audit_store import get_audit_store
    store = get_audit_store()
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    rows = [
        {"time": now, "ts": now, "type": "chat", "action": "route_local",
         "rule": "l2_confidential", "text_preview": "AND叠加样本-本地路由"},
        {"time": now, "ts": now, "type": "chat", "action": "allow",
         "rule": "default_allow", "text_preview": "AND叠加样本-放行"},
    ]
    for e in rows:
        store.append(e)
    try:
        d = client.get("/admin/audit/entries",
                       params={"action": "route_local", "since": "24h"}).json()
        items = d.get("entries") or []
        assert items, d
        assert all(e.get("action") == "route_local" for e in items), \
            [e.get("action") for e in items]
        assert not any(e.get("text_preview") == "AND叠加样本-放行" for e in items), items
        # 规则筛选 + 时间窗同理
        d2 = client.get("/admin/audit/entries",
                        params={"rule": "l2_confidential", "since": "24h"}).json()
        items2 = d2.get("entries") or []
        assert all(e.get("rule") == "l2_confidential" for e in items2), \
            [e.get("rule") for e in items2]
    finally:
        with store._mem_lock:
            store._mem[:] = [e for e in store._mem
                             if e.get("text_preview") not in ("AND叠加样本-本地路由", "AND叠加样本-放行")]


def test_audit_csv_hot_path_applies_filters():
    # 回归：CSV 热窗路径（无 since/until）也要按动作/规则筛选，
    # 早前完全不过滤 → 导出 5000 行全量（含放行行）
    import time
    from src.gateway.audit_store import get_audit_store
    store = get_audit_store()
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    rows = [
        {"time": now, "ts": now, "type": "chat", "action": "route_local",
         "rule": "l2_confidential", "text_preview": "CSV样本-本地路由"},
        {"time": now, "ts": now, "type": "chat", "action": "allow",
         "rule": "default_allow", "text_preview": "CSV样本-放行"},
    ]
    for e in rows:
        store.append(e)
    try:
        r = client.get("/admin/csv", params={"action": "route_local"})
        assert r.status_code == 200, r.status_code
        lines = [ln for ln in r.text.splitlines()[1:] if ln.strip()]
        assert lines, r.text[:500]
        assert all(",allow," not in ln for ln in lines), lines[:3]
        assert any("CSV样本-本地路由" in ln for ln in lines), lines[:3]
        assert not any("CSV样本-放行" in ln for ln in lines), lines[:3]
    finally:
        with store._mem_lock:
            store._mem[:] = [e for e in store._mem
                             if e.get("text_preview") not in ("CSV样本-本地路由", "CSV样本-放行")]


def test_audit_entries_kpi_from_mysql(monkeypatch):
    # 筛选可用 SQL 表达时，KPI 走审计库真值；顶层 total/pages 仍是热窗口径（分页不受影响）
    from src.gateway.audit_store import get_audit_store
    store = get_audit_store()
    monkeypatch.setattr(store, "count_mysql",
                        lambda **kw: {"total": 4402, "c_allow": 4000, "c_route": 398, "c_block": 4})
    d = client.get("/admin/audit/entries").json()
    assert d["kpi"]["source"] == "mysql"
    assert d["kpi"]["total"] == 4402 and d["kpi"]["c_allow"] == 4000 and d["kpi"]["c_block"] == 4
    assert d["kpi"]["c_other"] == 4402 - 4000 - 398 - 4
    assert d["total"] <= 5000 and d["pages"] <= 100

def test_audit_entries_kpi_window_fallback(monkeypatch):
    # q/ip/token/provider/shadow 筛选推不进 SQL → 回退热窗口径（审计库值可用也不用）
    from src.gateway.audit_store import get_audit_store
    store = get_audit_store()
    monkeypatch.setattr(store, "count_mysql",
                        lambda **kw: {"total": 9999, "c_allow": 9999, "c_route": 0, "c_block": 0})
    d = client.get("/admin/audit/entries", params={"q": "x"}).json()
    assert d["kpi"]["source"] == "window"
    assert d["kpi"]["total"] == d["total"]

def test_audit_count_sql_and_failopen(monkeypatch):
    # count SQL 形状（block 口径 = block% 或 egress_denied；LIKE 用 %%）+ 连接失败 fail-open 返 None
    import datetime as dt
    from src.gateway import audit_store
    captured = {}
    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, args): captured["sql"], captured["args"] = sql, args
        def fetchone(self): return {"total": 10, "c_allow": 8, "c_route": 1, "c_block": 1}
    class _Conn:
        def cursor(self): return _Cur()
        def close(self): pass
    class _PM:
        @staticmethod
        def connect(**kw):
            if kw.get("database") == "boom": raise RuntimeError("down")
            return _Conn()
    monkeypatch.setattr(audit_store, "pymysql", _PM)
    cfg = {"host": "h", "port": 3306, "user": "u", "password": "p", "database": "d"}
    r = audit_store._MySQLWriter.count(cfg, since=dt.datetime(2026, 1, 1), action="allow")
    assert r == {"total": 10, "c_allow": 8, "c_route": 1, "c_block": 1}
    assert "FROM audit_logs" in captured["sql"] and "block%%" in captured["sql"]
    assert captured["args"][0] == dt.datetime(2026, 1, 1) and captured["args"][-1] == "allow"
    assert audit_store._MySQLWriter.count({**cfg, "database": "boom"}) is None

def test_window_cache_dedupes_concurrent_fetches():
    # 统计页 7 端点同秒并发同窗口：应只打一次 DB（8→1），跨窗重取
    import threading
    import time as _t
    from src.gateway.admin_store import get_admin_store
    store = get_admin_store()
    saved_engine, saved_backend = store._engine, store._backend
    calls = []
    call_lock = threading.Lock()

    class _FakeResult:
        def mappings(self):
            return self
        def all(self):
            return [{"id": i, "ts": "2026-09-22 10:00:00", "key_id": None,
                     "key_name": "w", "client_ip": "1.1.1.1", "model": "m",
                     "provider": "p", "action": "allow", "status_code": 200,
                     "blocked_reason": "", "duration_ms": 1,
                     "gateway_internal_ms": 1, "upstream_ms": 0, "prompt_tokens": 0,
                     "completion_tokens": 0, "cached_tokens": 0,
                     "cache_creation_tokens": 0} for i in range(3)]

    class _FakeConn:
        def execute(self, q):
            with call_lock:
                calls.append(q)
            _t.sleep(0.05)  # 模拟 DB 时延，放大并发竞态窗
            return _FakeResult()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class _FakeEngine:
        def connect(self):
            return _FakeConn()

    store._engine, store._backend = _FakeEngine(), "mysql"
    try:
        since, until = "2026-09-16 00:00:00", "2026-09-23 00:00:00"
        results = [None] * 8
        barrier = threading.Barrier(8)
        def worker(i):
            barrier.wait()
            results[i] = store._fetch_window_raw(since, until)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(calls) == 1, f"同窗口并发应只打一次 DB，实际 {len(calls)}"
        assert all(r is results[0] for r in results), "命中缓存应共享同一原始行列表"
        store._fetch_window_raw(since, "2026-09-23 00:00:01")  # 跨窗
        assert len(calls) == 2, "不同窗口必须重取"
    finally:
        store._engine, store._backend = saved_engine, saved_backend
        store._window_cache = None

def test_stats_summary_equivalent_to_individual_funcs():
    # 单遍聚合 == 各独立 stats_*（逐字段等价；固定窗口避免 now 秒边界漂移）
    from src.gateway.admin_store import get_admin_store
    from src.gateway import stats_service as _ss
    store = get_admin_store()
    seed = lambda **kw: store.insert_request_log({
        "key_name": "pytest-summary", "action": "allow", "model": "sum-model",
        "provider": "sum-prov", "client_ip": "7.7.7.7", "status_code": 200,
        "prompt_tokens": 0, "completion_tokens": 0, **kw})
    seed(ts="2026-09-20 15:00:00", prompt_tokens=100, completion_tokens=50,
         cached_tokens=10, cache_creation_tokens=5, duration_ms=100)
    seed(ts="2026-09-20 15:00:01", action="block", blocked_reason="l1:sum-rule",
         prompt_tokens=7, completion_tokens=3, duration_ms=50)
    seed(ts="2026-09-20 16:30:00", action="route_local", key_name="",
         prompt_tokens=11, completion_tokens=2, duration_ms=30)
    seed(ts="2026-09-21 02:00:00", status_code=500, model="sum-model-2",
         prompt_tokens=1, completion_tokens=1)
    s, u = "2020-01-01 00:00:00", "2030-01-01 00:00:00"
    d = store.stats_summary(s, u, top=20)
    assert d["overview"] == store.stats_overview(s, u)
    assert d["group_key_name"] == store.stats_group(s, u, "key_name")
    assert d["group_provider"] == store.stats_group(s, u, "provider")
    assert d["group_rule"] == store.stats_group(s, u, "blocked_reason")
    assert d["timeseries"] == store.stats_timeseries(s, u)
    assert d["key_model"] == store.stats_key_model(s, u, 20)
    assert d["billing"] == store.stats_billing(s, u)
    assert d["billing_daily"] == store.stats_billing_daily(s, u)
    # 定价与 build_billing 同源
    assert _ss._price_billing(d["billing"], d["billing_daily"], 168) == \
        _ss._price_billing(store.stats_billing(s, u), store.stats_billing_daily(s, u), 168)

def test_real_model_label_normalizes_dirty_rows():
    # 2026-10-12 生产实测计费页 model 脏行：空 model → "vllm_local/"、客户端斜杠式
    # model → "vllm_local/vllm_local/qwen2.5:7b"（双前缀行在外网 provider 上还会让
    # _price_billing 按首个 / 拆名漏定价）。_real_model_label 是 key_model/billing/
    # billing_daily/summary 四个口径的唯一汇聚点，在读取侧归一、不动历史数据。
    from src.gateway.admin_store import _real_model_label
    assert _real_model_label({}, "vllm_local", "") == "vllm_local"
    assert _real_model_label({}, "deepseek", "   ") == "deepseek"
    assert _real_model_label({}, "vllm_local", "vllm_local/qwen2.5:7b") == "vllm_local/qwen2.5:7b"
    assert _real_model_label({}, "vllm_local", "qwen2.5:7b") == "vllm_local/qwen2.5:7b"
    assert _real_model_label({}, "vllm_local", "deepseek/deepseek-flash") == \
        "vllm_local/deepseek/deepseek-flash"  # 前缀不匹配 provider，不敢猜、原样保留
    assert _real_model_label({}, "", "") == "(unknown)"
    assert _real_model_label({}, "deepseek", "ab-flash") == "deepseek/ab-flash"
    routes = {"ab-flash": {"candidates": [{"provider": "deepseek", "model": "deepseek-flash"}]}}
    assert _real_model_label(routes, "deepseek", "ab-flash") == "deepseek/deepseek-flash"

def test_stats_timeseries_counts_overlapping_local_and_block_separately():
    # 回归 2026-10-09：loc/blk 曾用 elif 互斥，而 stats_overview 的 SQL 侧是两条
    # 独立 sum(case) —— 一行同时命中两种口径时趋势图少算一笔，与概览打架。
    # 生产实测 route_local 行 status_code 恒 200（两套口径当前相等），
    # 但口径必须同构，否则哪天出现 route_local+403 就静默少算。
    from src.gateway.admin_store import get_admin_store
    store = get_admin_store()
    seed = lambda **kw: store.insert_request_log({
        "key_name": "pytest-overlap", "action": "allow", "model": "ov-model",
        "provider": "ov-prov", "client_ip": "8.8.8.8", "status_code": 200,
        "prompt_tokens": 0, "completion_tokens": 0, "duration_ms": 10, **kw})
    ts = "2026-09-20 15:00:00"
    seed(ts=ts, action="allow")                                  # 纯放行
    seed(ts=ts, action="block", blocked_reason="l1:ov-rule")# 纯拦截
    seed(ts=ts, action="route_local")                       # 纯本地路由
    s, u = "2020-01-01 00:00:00", "2030-01-01 00:00:00"
    # 同 session 共用 memory store，别的用例也会 seed route_local/block 行落进同一宽窗口，
    # 所以断言「自己 4 条的相对差值」，不写绝对数（踩过：写死 2 结果读到别的用例的 3）
    base_r = sum(store.stats_timeseries(s, u)["routes"])
    base_b = sum(store.stats_timeseries(s, u)["blocks"])
    base_c = sum(store.stats_timeseries(s, u)["counts"])
    seed(ts=ts, action="route_local", status_code=403)            # 同时命中两种口径
    for d in (store.stats_timeseries(s, u), store.stats_summary(s, u)["timeseries"]):
        # 差值都是 1：这行必须同时进 routes 和 blocks。elif 口径下 blocks 差值会是 0
        assert sum(d["routes"]) - base_r == 1, f"重叠行须计入本地路由：{d['routes']}"
        assert sum(d["blocks"]) - base_b == 1, f"重叠行须计入拦截：{d['blocks']}"
        assert sum(d["counts"]) - base_c == 1
    ov = store.stats_overview(s, u)
    ts_ = store.stats_timeseries(s, u)
    assert (ov["local_routed"], ov["blocked"]) == (sum(ts_["routes"]), sum(ts_["blocks"])), \
        "概览与趋势的本地路由/拦截口径必须一致"

def test_stats_chart_series_buckets():
    # 总览折线图分桶：计数/QPS/分位同桶；gi=0 计次不计分位；空桶 qps=0/分位 None
    # 窗口取 2030（其他用例 seed 全在 2026，零碰撞）
    import calendar, datetime
    from src.gateway.admin_store import get_admin_store
    store = get_admin_store()
    seed = lambda **kw: store.insert_request_log({
        "key_name": "pytest-chart", "action": "allow", "model": "ch-model",
        "provider": "ch-prov", "client_ip": "7.7.7.9", "status_code": 200,
        "prompt_tokens": 0, "completion_tokens": 0, **kw})
    seed(ts="2030-06-01 12:00:10", gateway_internal_ms=10)
    seed(ts="2030-06-01 12:00:20", gateway_internal_ms=20)
    seed(ts="2030-06-01 12:00:30", gateway_internal_ms=30)
    seed(ts="2030-06-01 12:00:40", gateway_internal_ms=0)
    seed(ts="2030-06-01 12:02:10", status_code=500)
    seed(ts="2030-06-01 12:02:20", status_code=429)
    seed(ts="2030-06-01 12:02:30", status_code=403)
    s, u = "2030-06-01 12:00:00", "2030-06-01 13:00:00"
    d = store.stats_chart_series(s, u, n=60)
    assert len(d["ts"]) == len(d["counts"]) == len(d["qps"]) == 60
    assert d["ts"][0] == calendar.timegm(datetime.datetime(2030, 6, 1, 12, 0, 0).timetuple())
    assert d["counts"][0] == 4 and d["qps"][0] == round(4 / 60, 3)
    assert (d["p50"][0], d["p95"][0], d["p99"][0]) == (20, 30, 30)
    # 峰值保持：4 行各占一个 10s 子槽 → qps_max=0.1；延迟极值 30/10
    assert d["qps_max"][0] == 0.1 and d["lat_max"][0] == 30 and d["lat_min"][0] == 10
    assert d["qps"][1] == 0 and d["p50"][1] is None and d["p99"][1] is None
    assert d["qps_max"][1] == 0 and d["lat_max"][1] is None
    # 错误率拆 5xx/403/429（桶 2 三行各一）：占比 1/3，空桶 0
    assert d["counts"][2] == 3
    assert (d["err_5xx"][2], d["err_403"][2], d["err_429"][2]) == (round(1 / 3, 4),) * 3
    assert d["err_5xx"][0] == 0 and d["err_5xx"][1] == 0
    c = client.get("/admin/api/stats/chart", params={"hours": 1}).json()
    assert {"ts", "counts", "qps", "qps_max", "p50", "p95", "p99", "lat_max", "lat_min",
            "err_5xx", "err_403", "err_429"} <= set(c)
    assert len(c["ts"]) == len(c["qps"]) == len(c["p50"])

def test_stats_summary_endpoint():
    import time
    from src.gateway.admin_store import get_admin_store
    store = get_admin_store()
    # 本测试独立 seed（不依赖其他用例的行），唯一 KEY 便于按 key 切片对比
    store.insert_request_log({
        "key_name": "pytest-summary-ep", "action": "allow", "model": "sum-ep-model",
        "provider": "sum-ep-prov", "client_ip": "7.7.7.8", "status_code": 200,
        "prompt_tokens": 42, "completion_tokens": 7, "duration_ms": 10,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S",
                            time.localtime(time.time() - 36 * 3600))})
    d = client.get("/admin/api/stats/summary", params={"hours": 168}).json()
    for k in ("hours", "overview", "group_key_name", "group_provider", "group_rule",
              "timeseries", "key_model", "billing"):
        assert k in d, list(d.keys())
    assert {"total", "blocked", "local_routed", "active_ips", "avg_ms",
            "tokens_total"} <= set(d["overview"])
    assert {"items", "total_cost", "daily", "daily_total"} <= set(d["billing"])
    # 与独立 billing 端点同 KEY 逐字段一致（只比本测试唯一 KEY，
    # 避开两请求间 now 秒窗口 1 秒漂移的其他行）
    b = client.get("/admin/api/stats/billing", params={"hours": 168}).json()
    mine_s = {it["key"]: it for it in d["billing"]["items"] if it["key"] == "pytest-summary-ep"}
    mine_b = {it["key"]: it for it in b["items"] if it["key"] == "pytest-summary-ep"}
    assert mine_s == mine_b
    assert mine_s, "seed 行应出现在 billing"


def test_admin_metrics_timeseries_shape():
    # 防半截 revert 复现：metrics_timeseries 函数体曾留未定义 hours/RING_POINTS
    # 致线上 500，端点无测试覆盖（见 agents/gotchas.md）
    r = client.get("/admin/metrics/timeseries")
    assert r.status_code == 200
    d = r.json()
    assert {"interval_s", "ticks", "panel"} <= set(d)
    assert "l2_up" in d["panel"] and "ring" in d["panel"]
    # recent>0 只回环尾 N 个 tick（总览页 30s 轮询用，省 24h 全环体积）
    r2 = client.get("/admin/metrics/timeseries", params={"recent": 3})
    assert r2.status_code == 200
    d2 = r2.json()
    assert {"interval_s", "ticks", "panel"} <= set(d2)
    assert len(d2["ticks"]) <= 3


def test_l2_divergences_shape():
    # 测试环境无 audit MySQL（l2 列无落库数据）→ 验端点形状/导出 content-type
    r2 = client.get("/admin/api/l2-divergences", params={"hours": 24, "cell": "nc"})
    assert r2.status_code == 200
    assert {"hours", "total", "items"} <= set(r2.json())
    r3 = client.get("/admin/api/l2-divergences/export", params={"hours": 24, "labeled": "labeled"})
    assert r3.status_code == 200
    assert r3.headers["content-type"].startswith("application/jsonl")
    assert "attachment" in r3.headers.get("content-disposition", "")


def test_l2_divergences_label_roundtrip():
    from src.gateway.admin_store import get_admin_store
    st = get_admin_store()
    r = client.post("/admin/api/l2-divergences/label", json={"req_id": "pytest-l2label-1", "label": 1})
    assert r.status_code == 200
    assert st.get_l2_labels(["pytest-l2label-1"]) == {"pytest-l2label-1": 1}
    client.post("/admin/api/l2-divergences/label", json={"req_id": "pytest-l2label-1", "label": 0})
    assert st.get_l2_labels(["pytest-l2label-1"]) == {"pytest-l2label-1": 0}
    client.post("/admin/api/l2-divergences/label", json={"req_id": "pytest-l2label-1", "label": None})
    assert st.get_l2_labels(["pytest-l2label-1"]) == {}
    bad = client.post("/admin/api/l2-divergences/label", json={"req_id": "", "label": 1})
    assert bad.status_code == 422


def test_l2_divergences_auto_label_guard(monkeypatch):
    """模型自动标注：外网 provider 拒绝（审计文本不出境）；建议不落库（人工采纳才写）。"""
    from src.gateway import l2_retrain
    from src.gateway.admin_store import get_admin_store

    r = client.post("/admin/api/l2-divergences/auto-label",
                    json={"model": "deepseek", "req_ids": ["x"]})
    assert r.status_code == 400

    models = client.get("/admin/api/l2/auto-label/models").json()["models"]
    assert any(m["value"] == "vllm_local" for m in models)
    assert all(m["value"] != "deepseek" for m in models)  # 外网模型不进下拉

    # 用假分歧行替掉审计源（不依赖生产库），模型调用也替掉
    monkeypatch.setattr("src.gateway.admin_api._l2_divergence_rows",
                        lambda *a, **k: [{"req_id": "pytest-auto-1", "ts": "t", "cell": "cn",
                                          "main": "CONFIDENTIAL", "shadow": "NORMAL",
                                          "shadow_prob": 0.1, "text_preview": "工资表",
                                          "filename": ""}])
    monkeypatch.setattr(l2_retrain, "auto_label",
                        lambda items, provider, **k: [
                            {"req_id": i["req_id"], "label": 1, "confidence": 0.9, "reason": "薪酬"}
                            for i in items])
    d = client.post("/admin/api/l2-divergences/auto-label",
                    json={"model": "vllm_local", "req_ids": ["pytest-auto-1"]}).json()
    assert d["total"] == 1 and d["items"][0]["label"] == 1
    # 建议不落库：l2_labels 是训练真值，必须人工采纳才写
    assert get_admin_store().get_l2_labels(["pytest-auto-1"]) == {}


def test_auto_label_parses_model_output():
    """模型三种真实输出形态都要能解析：标准 JSON / 畸形前缀（实测 '{"label":  "label": 0,…}'）/ JSON 后带散文。"""
    import asyncio
    from src.gateway import l2_retrain

    class _Resp:
        def __init__(self, content, finish="stop"):
            self._c, self._f = content, finish

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"finish_reason": self._f, "message": {"content": self._c}}]}

    class _Client:
        def __init__(self, resp):
            self.resp = resp

        async def post(self, *a, **k):
            return self.resp

    def run(raw, finish="stop"):
        return asyncio.run(l2_retrain._auto_label_one(
            _Client(_Resp(raw, finish)), "http://x/v1/chat/completions", "m", {},
            {"req_id": "r", "text": "t"}))

    assert run('{"label": 1, "confidence": 0.95, "reason": "薪酬"}')["label"] == 1
    d = run('{"label":  "label": 0, "confidence": 0.9, "reason": "普通"}')  # 畸形前缀：走正则兜底
    assert (d["label"], d["confidence"]) == (0, 0.9)
    d = run('说明：\n{"label": 0, "confidence": 0.8, "reason": "普通"}\n以上。')  # JSON 后带散文
    assert (d["label"], d["confidence"]) == (0, 0.8)
    d = run("", finish="length")                                             # 空串/截断：不猜，报 error
    assert d["label"] is None and "无法解析" in d["error"]


def test_policy_version_rollback(monkeypatch, tmp_path):
    # 独立 policy 文件，绝不碰生产 policy.yaml
    import yaml as _yaml
    from src.gateway import policy_admin
    from src.gateway.policy import invalidate_policy_cache

    pol = tmp_path / "policy_test_versions.yaml"
    pol.write_text("policies:\n  - name: base-allow\n    priority: 100\n    action: allow\n    when: always\n",
                   encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_POLICY_PATH", str(pol))
    invalidate_policy_cache()
    assert [p["name"] for p in policy_admin.list_policies()] == ["base-allow"]

    r = client.post("/admin/api/policies", json={
        "name": "pytest-rollback-rule", "priority": 5, "action": "block",
        "when": {"text contains_any": ["pytest-rollback-marker"]}})
    assert r.status_code == 200, r.text
    invalidate_policy_cache()
    assert "pytest-rollback-rule" in [p["name"] for p in policy_admin.list_policies()]

    vs = client.get("/admin/api/policy/versions", params={"limit": 50}).json()["items"]
    vid = next(v["id"] for v in vs if v["source"] == "save")
    full = client.get(f"/admin/api/policy/versions/{vid}").json()
    assert [p["name"] for p in _yaml.safe_load(full["yaml_text"])["policies"]] == ["base-allow"]
    assert full["rule_count"] == 1

    rb = client.post(f"/admin/api/policy/versions/{vid}/rollback")
    assert rb.status_code == 200, rb.text
    invalidate_policy_cache()
    assert "pytest-rollback-rule" not in [p["name"] for p in policy_admin.list_policies()]
    vs2 = client.get("/admin/api/policy/versions", params={"limit": 50}).json()["items"]
    assert vs2[0]["source"] == "rollback"
    assert vs2[0]["rule_count"] == 2  # 回滚前现状（base-allow + pytest 规则）
    assert client.get("/admin/api/policy/versions/999999").status_code == 404


def test_events_stream_shape():
    # 测试环境无 audit MySQL → 零事件；验端点形状/桶数/summary 键
    r = client.get("/admin/api/events", params={"hours": 6})
    assert r.status_code == 200
    d = r.json()
    assert d["hours"] == 6
    assert d["summary"] == {"degrade": 0, "route_local": 0, "l2_unavailable": 0}
    assert len(d["by_hour"]) == 7
    assert {"t", "degrade", "route_local", "l2_unavailable"} <= set(d["by_hour"][0])
    assert d["recent"] == []


def test_admin_csv_export_shape():
    # 防半截重构复发：486bf71 瘦身C1 曾丢 export_csv 的 cols/buf/w 初始化，
    # 管理台"导出 CSV"按钮恒 500（NameError: buf），同 timeseries 半截 revert 事故类
    r = client.get("/admin/csv", params={"since": "24h"})
    assert r.status_code == 200, r.text[:200]
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[0].startswith("time,type,rule,action")
    r2 = client.get("/admin/csv", params={})
    assert r2.status_code == 200, r2.text[:200]


def test_rule_hits_shape():
    # 测试环境无 audit MySQL → 零命中；验端点形状 + default_allow 排除语义
    r = client.get("/admin/api/rule-hits", params={"hours": 168})
    assert r.status_code == 200
    d = r.json()
    assert d["hours"] == 168 and d["total"] == 0 and d["items"] == []


def test_rule_hits_excludes_l2_decisions(monkeypatch):
    # 规则命中榜 = 审计页同一集合：L2 判决与系统内置判定都计入（界面由 ruleZh 标
    # 「（系统内置）」），只排除「default_allow」
    from src.gateway.audit_store import get_audit_store
    rows = [
        {"rule": "l2_confidential", "action": "route_local", "ts": "2026-10-20 01:00:00"},
        {"rule": "l2_unavailable", "action": "route_local", "ts": "2026-10-20 02:00:00"},
        {"rule": "encoded_keyword", "action": "route_local", "ts": "2026-10-20 02:10:00"},
        {"rule": "review_text_overflow", "action": "route_local", "ts": "2026-10-20 02:20:00"},
        {"rule": "first_chunk_fallback", "action": "route_local", "ts": "2026-10-20 02:30:00"},
        {"rule": "default_allow", "action": "allow", "ts": "2026-10-20 03:00:00"},
        {"rule": "financial_local_only", "action": "route_local", "ts": "2026-10-20 04:00:00"},
    ]
    monkeypatch.setattr(get_audit_store(), "query_mysql", lambda **kw: rows)
    d = client.get("/admin/api/rule-hits", params={"hours": 168}).json()
    assert d["total"] == 6, d
    assert {it["rule"] for it in d["items"]} == {
        "l2_confidential", "l2_unavailable", "encoded_keyword", "review_text_overflow",
        "first_chunk_fallback", "financial_local_only"}



def test_laya_apply_labeled_priority():
    # 人工标注覆盖 rule 推导（含冲突取密）；不在真实集的标注样本直接进训练
    import hashlib
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_laya_dataset", "scripts/build_laya_dataset.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    rt = [{"text": "salary payload", "label": 0, "source": "audit:default_allow"}]
    re_ = [{"text": "chat text", "label": 1, "source": "audit:l2_confidential"}]
    labeled = {
        hashlib.sha1(b"salary payload").hexdigest():
            {"text": "salary payload", "label": 1, "source": "human:abc", "filename": "", "ts": "x"},
        hashlib.sha1(b"brand new labeled sample over ten chars").hexdigest():
            {"text": "brand new labeled sample over ten chars", "label": 0, "source": "human:def", "filename": "", "ts": "x"},
    }
    rt2, re2, n_over, n_add = m.apply_labeled(rt, re_, labeled)
    assert n_over == 1
    assert rt[0]["label"] == 1 and rt[0]["source"] == "human:abc"  # 人工 1 覆盖 rule 推导 0
    assert n_add == 1 and len(rt2) == 2 and len(re2) == 1
    # 幂等：同 source 二次应用不重复计数
    _, _, n_over2, n_add2 = m.apply_labeled(rt2, re2, labeled)
    assert n_over2 == 0 and n_add2 == 0


def test_l2_retrain_guardrails():
    r = client.get("/admin/api/l2/retrain/status")
    assert r.status_code == 200
    d = r.json()
    assert {"state", "stage", "min_labels", "labeled_count", "candidate"} <= set(d)
    assert d["state"] in ("idle", "running", "done", "failed")
    # 测试环境标注不足 → 启动必须 422（绝不真训练）
    r2 = client.post("/admin/api/l2/retrain")
    assert r2.status_code == 422
    # promote 不存在的版本 → 404（绝不真重启服务）
    r3 = client.post("/admin/api/l2/retrain/promote", json={"version": 9999})
    assert r3.status_code == 404


def test_l2_retrain_classify_posts_to_classify_path(monkeypatch):
    # 回归：_classify 必须打 /classify（裸 URL POST / 会 404，v10 eval 即死于此）
    import json
    from src.gateway import l2_retrain
    seen = {}

    class FakeResp:
        def read(self):
            return json.dumps({"probabilities": {"CONFIDENTIAL": 0.9}}).encode()

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return FakeResp()

    monkeypatch.setattr(l2_retrain.urllib.request, "urlopen", fake_urlopen)
    assert l2_retrain._classify("http://127.0.0.1:8004", "hi") == 0.9
    assert seen["url"] == "http://127.0.0.1:8004/classify"


def test_l2_retrain_score_detail(monkeypatch):
    # 报告明细：分项 cats + 错题 wrong（含预览截断），供晋升决策
    from src.gateway import l2_retrain
    monkeypatch.setattr(l2_retrain, "_classify",
                        lambda url, t, fn="": 0.9 if t == "secret text" else 0.1)
    cases = [("probe", "p1", "secret text", "", 1),
             ("real", "r1", "hello world, this is a much longer benign sentence for preview", "", 0),
             ("real", "r2", "secret text", "", 0)]
    s = l2_retrain._score("http://x", cases)
    assert (s["total"], s["correct"]) == (3, 2)
    assert s["cats"] == {"probe": {"total": 1, "correct": 1},
                         "real": {"total": 2, "correct": 1}}
    assert len(s["wrong"]) == 1 and s["wrong"][0]["name"] == "real:r2"
    assert s["wrong"][0]["got"] == 1 and s["wrong"][0]["want"] == 0


def test_l2_retrain_versions_api():
    r = client.get("/admin/api/l2/retrain/versions")
    assert r.status_code == 200
    d = r.json()
    assert "versions" in d and "current" in d and isinstance(d["versions"], list)


def test_l2_retrain_latest_prompt_fix_reads_last(monkeypatch, tmp_path):
    # 候选 prompt 持久化：读最后一行合法 JSON；坏行忽略；无文件返回 {}
    from src.gateway import l2_retrain
    p = tmp_path / "prompt-proposals.jsonl"
    p.write_text('{"candidate": {"correct": 1, "total": 2}}\nbad\n'
                 '{"candidate": {"correct": 2, "total": 2}}\n', encoding="utf-8")
    monkeypatch.setattr(l2_retrain, "PROMPT_PROPOSAL_FILE", str(p))
    assert l2_retrain.latest_prompt_fix()["candidate"]["correct"] == 2
    monkeypatch.setattr(l2_retrain, "PROMPT_PROPOSAL_FILE", str(tmp_path / "missing.jsonl"))
    assert l2_retrain.latest_prompt_fix() == {}


def test_l2_retrain_prompt_propose_latest_api():
    r = client.get("/admin/api/l2/retrain/prompt-propose")
    assert r.status_code == 200 and "proposal" in r.json()


def test_laya_balance_and_hard(tmp_path, monkeypatch):
    # 配比：多数类下采样到 0.5，只删不造；真实/人工/难例优先保留
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_laya_dataset", "scripts/build_laya_dataset.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    train = ([{"text": f"pos{i}", "label": 1, "source": "synth_x"} for i in range(80)]
             + [{"text": f"neg{i}", "label": 0, "source": "synth_x"} for i in range(20)]
             + [{"text": "real keep", "label": 1, "source": "audit:z"}])
    out = m.balance(list(train), target=0.5)
    pos = sum(v["label"] for v in out)
    assert abs(pos / len(out) - 0.5) <= 0.05
    assert any(v["text"] == "real keep" for v in out)  # 真实审计没被洗掉
    # 难例文件缺失 → []，不挡构建
    monkeypatch.setattr(m, "OUT_DIR", str(tmp_path))
    assert m.load_hard() == []
    (tmp_path / "hard.jsonl").write_text('{"text": "hard case one two three", "label": 1}\n', encoding="utf-8")
    assert m.load_hard() == [{"text": "hard case one two three", "label": 1}]


def test_l2_retrain_add_hard(tmp_path, monkeypatch):
    # 错题入 hard：去重/拦短文本/拦非法 label，文件落盘永不入库
    from src.gateway import l2_retrain
    monkeypatch.setattr(l2_retrain, "DATASET_DIR", str(tmp_path))
    r = l2_retrain.add_hard([
        {"text": "salary leak sample for hard mining case", "label": 1},
        {"text": "salary leak sample for hard mining case", "label": 1},
        {"text": "短", "label": 0},
        {"text": "benign mail text placeholder case", "label": 2},
    ])
    assert r["added"] == 1 and r["skipped"] == 3 and "labeled" in r
    r2 = client.post("/admin/api/l2/retrain/hard",
                     json={"items": [{"text": "salary leak sample for hard mining case", "label": 1}]})
    assert r2.status_code == 200  # 端点只管透传（写真实 ~/models，不在此断言）


def test_l2_retrain_prompt_test_guards():
    # prompt-test 门槛：太短 422（不碰网络）；版本戳形状
    from src.gateway import l2_retrain
    import pytest
    with pytest.raises(ValueError):
        l2_retrain.prompt_test("太短")
    info = l2_retrain.current_prompt_info()
    assert len(info["sha1"]) == 12 and info["chars"] > 100
    r = client.post("/admin/api/l2/retrain/prompt-test", json={"system_prompt": "x" * 19})
    assert r.status_code == 422


def test_l2_retrain_score_qwen(monkeypatch):
    # _score_qwen 走候选 prompt 直调 qwen（与 _score 同报告形状）
    from src.gateway import l2_retrain
    from src.gateway import small_model as _sm

    async def fake_call_qwen(text, filename="", headers=None, sheet_names=None, system=None,
                            temperature=0.1):
        assert system == "CANDIDATE"
        assert temperature == 0.0, "打分必须 temperature=0（否则同 prompt 两次跑有采样噪声）"
        return {"label": "CONFIDENTIAL", "confidence": 0.95, "degraded": False}

    monkeypatch.setattr(_sm, "_call_qwen", fake_call_qwen)
    s = l2_retrain._score_qwen([("probe", "p1", "t", "", 1), ("real", "r1", "t", "", 0)], "CANDIDATE")
    assert (s["total"], s["correct"]) == (2, 1)
    assert s["wrong"][0]["name"] == "real:r1" and s["wrong"][0]["p"] == 0.95


def test_l2_retrain_prompt_test_hashes_raw(monkeypatch):
    # stamp 必须等于即将应用的原文（strip 哈希曾导致线上永远对不上）
    import hashlib
    from src.gateway import l2_retrain
    monkeypatch.setattr(l2_retrain, "_score_qwen",
                        lambda cases, system: {"total": 0, "correct": 0, "cats": {}, "wrong": []})
    raw = "  candidate prompt padding text here  \n"
    d = l2_retrain.prompt_test(raw)
    assert d["prompt_sha1"] == hashlib.sha1(raw.encode()).hexdigest()[:12]


def test_l2_prompt_versions_roundtrip(monkeypatch):
    # PUT 前快照 → 列表可见 → 回滚还原（改完必须恢复线上 v1 prompt）。
    # _l2ov 打 fake：真文件 l2_overrides.yaml 是生产共用，测试绝不碰（曾洗掉线上 v1 prompt）。
    from src.gateway import admin_api as _aa

    class FakeOv:
        def __init__(self):
            self.d = {}
        def load_overrides(self):
            return dict(self.d)
        def save_overrides(self, values, *, delete=None):
            for k, v in (values or {}).items():
                self.d[k] = v
            for k in delete or set():
                self.d.pop(k, None)
            return dict(self.d)

    monkeypatch.setattr(_aa, "_l2ov", FakeOv())
    cur = client.get("/admin/api/l2-prompts").json()["system_prompt"]
    new = cur + "\n测试追加行（谨慎判密）。"
    r = client.put("/admin/api/l2-prompts", json={"AI_GATEWAY_L2_SYSTEM_PROMPT": new})
    assert r.status_code == 200 and r.json()["system_prompt"] == new
    items = client.get("/admin/api/l2-prompt-versions").json()["items"]
    assert len(items) >= 1 and items[0]["system_preview"].startswith(cur[:200])
    assert len(items[0].get("system_sha1", "")) == 12
    vid = items[0]["id"]
    d = client.get(f"/admin/api/l2-prompt-versions/{vid}").json()
    assert d["system_prompt"] == cur
    rb = client.post(f"/admin/api/l2-prompt-versions/{vid}/rollback").json()
    assert rb["ok"] is True and rb["system_prompt"] == cur
    assert client.get("/admin/api/l2-prompt-versions").json()["items"][0]["source"] == "rollback"
    assert client.get("/admin/api/l2-prompts").json()["system_prompt"] == cur
    assert client.post("/admin/api/l2-prompt-versions/999999/rollback").status_code == 404


def test_l2_divergences_dedup_collapse():
    # 同文本+文件名+方向折叠：total 去重，dup 计数，防双行双标
    r = client.get("/admin/api/l2-divergences?hours=1&limit=500")
    assert r.status_code == 200
    items = r.json()["items"]
    keys = [(x["text_preview"], x["filename"], x["cell"]) for x in items]
    assert len(keys) == len(set(keys))
    assert all(x.get("dup", 1) >= 1 for x in items)


def test_l2_retrain_diff_reports():
    # 回归定义：上版对、这版错 = 新错；反之为修好
    from src.gateway import l2_retrain
    prev = {"battery": {"wrong": [{"name": "real:a"}, {"name": "reg:b"}]}}
    cur = {"battery": {"wrong": [{"name": "real:a"}, {"name": "probe:c"}]}}
    d = l2_retrain._diff_reports(prev, cur)
    assert d == {"new": ["probe:c"], "fixed": ["reg:b"], "still": ["real:a"]}
    assert l2_retrain._diff_reports(None, cur)["new"] == ["probe:c", "real:a"]


def test_l2_propose_apply_patch_dedup():
    # 补丁拼接：追加一次有效，重复追加不再叠加，空补丁原样返回
    from src.gateway import l2_retrain
    cur = "你是分类器。\n规则A。"
    p1 = l2_retrain._apply_patch(cur, "规则B。")
    assert p1 == cur + "\n规则B。"
    assert l2_retrain._apply_patch(p1, "规则B。") == p1
    assert l2_retrain._apply_patch(cur, "  ") == cur


def test_l2_propose_meta_contract():
    # 自省元提示词 frozen 约束：只允许追加 + 严格 JSON（含 patch 键）
    from src.gateway import l2_retrain
    m = l2_retrain.PROPOSE_META_SYSTEM
    assert "追加" in m and '"patch"' in m and "不准修改现有文字" in m


def test_l2_retrain_ensure_overrides(tmp_path, monkeypatch):
    """量 prompt 前必须应用持久化覆盖，否则独立进程读到内置默认（线上值≠评的值）"""
    from src.gateway import l2_overrides as ov, l2_retrain
    f = tmp_path / "ov.yaml"
    f.write_text("AI_GATEWAY_L2_SYSTEM_PROMPT: PERSISTED_PROMPT\n", encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_L2_OVERRIDES_PATH", str(f))
    monkeypatch.delenv("AI_GATEWAY_L2_SYSTEM_PROMPT", raising=False)
    l2_retrain._ensure_overrides()
    assert ov.load_overrides()["AI_GATEWAY_L2_SYSTEM_PROMPT"] == "PERSISTED_PROMPT"
    # 契约是「持久化值进 env」（get_system_prompt 优先读内存覆盖，别的测试可能已设过）
    import os as _os
    assert _os.environ["AI_GATEWAY_L2_SYSTEM_PROMPT"] == "PERSISTED_PROMPT"


def test_div_group_key_ignores_direction():
    """方向翻转仍是同一条样本：键不含 cell，否则 cn↔nc 翻转会裂成两条（标注看着丢）"""
    from src.gateway.admin_api import _div_group_key
    a = {"text_preview": "涨薪结果：王五 月薪 15000", "filename": "", "cell": "cn"}
    b = dict(a, cell="nc")
    assert _div_group_key(a) == _div_group_key(b)
    c = dict(a, filename="salary.xlsx")
    assert _div_group_key(a) != _div_group_key(c)


def test_l2_round_marker_and_since_window():
    """轮次标记 + since 窗口：since 起点优先于 hours（不然两轮灌数同窗 → dup×2）"""
    import src.gateway.admin_api as aa
    r = client.get("/admin/api/l2-round")
    assert r.status_code == 200 and "since" in r.json()
    import datetime as _dt
    late = _dt.datetime.utcnow() - _dt.timedelta(seconds=5)
    c1 = aa._cutoff(24, late.strftime("%Y-%m-%d %H:%M:%S"))
    assert c1 >= late - _dt.timedelta(seconds=2)
    c2 = aa._cutoff(1, "垃圾值")           # 坏 since 退回 hours
    assert (_dt.datetime.utcnow() - c2) <= _dt.timedelta(hours=1, minutes=1)


def test_set_prompt_overrides_clears_cache():
    """改提示词必须清 L2 判定缓存：否则同一文本在 TTL(600s) 内继续吐旧 prompt 结论"""
    from src.gateway import small_model as sm
    sm._CACHE["k"] = (9e9, {"label": "NORMAL"})
    sm.set_prompt_overrides(system="NEW PROMPT")
    assert not sm._CACHE
    sm.set_prompt_overrides(system="")


def test_labeled_regression_dedup_by_text(monkeypatch):
    """灌数多轮同一句 → 报告按文本去重（8 条 miss 实为 2 句 ×4 轮），且只调一次模型"""
    from src.gateway import l2_retrain, admin_store, audit_store
    calls = []

    class FakeAudit:
        def query_mysql(self, **kw):
            return [{"req_id": f"r{i}", "text_preview": "同一句话", "filename": ""} for i in range(4)]
    class FakeAdmin:
        def list_l2_labels(self, n):
            return [{"req_id": f"r{i}", "label": 1} for i in range(4)]
    monkeypatch.setattr(audit_store, "get_audit_store", lambda: FakeAudit())
    monkeypatch.setattr(admin_store, "get_admin_store", lambda: FakeAdmin())
    monkeypatch.setattr(l2_retrain, "_classify", lambda *a: (calls.append(1), 0.01)[1])
    s = l2_retrain._labeled_regression("http://x")
    assert s["total"] == 1 and s["matched"] == 0
    assert len(s["missed"]) == 1 and s["missed"][0]["dup"] == 4
    assert len(calls) == 1  # 同文本只打一次模型


def test_divergence_regression_dedup_and_want(monkeypatch):
    """历史分歧回归：只考 label!=shadow 的行、按文本去重、截断文本靠前缀匹配 want"""
    from src.gateway import l2_retrain, audit_store

    class FakeAudit:
        def query_mysql(self, **kw):
            return [
                {"req_id": "a", "text_preview": "同一句分歧", "filename": "",
                 "l2": '{"label": "NORMAL", "shadow_label": "CONFIDENTIAL"}'},
                {"req_id": "b", "text_preview": "同一句分歧", "filename": "",
                 "l2": '{"label": "NORMAL", "shadow_label": "CONFIDENTIAL"}'},  # 多轮重复
                {"req_id": "c", "text_preview": "主影一致的不考", "filename": "",
                 "l2": '{"label": "NORMAL", "shadow_label": "NORMAL"}'},
                {"req_id": "d", "text_preview": "候选表里没有的句子", "filename": "",
                 "l2": '{"label": "NORMAL", "shadow_label": "CONFIDENTIAL"}'},
                {"req_id": "e", "text_preview": "没进 L2 的不考", "filename": "", "l2": None},
            ]

    monkeypatch.setattr(audit_store, "get_audit_store", lambda: FakeAudit())
    monkeypatch.setattr(l2_retrain, "_candidate_wants", lambda: [("同一句分歧", 1)])
    monkeypatch.setattr(l2_retrain, "_classify", lambda *a: 0.02)
    d = l2_retrain._divergence_regression("http://x")
    assert d["total"] == 1 and d["matched"] == 0
    assert d["missed"][0]["want"] == 1 and d["missed"][0]["got"] == 0
    assert d["missed"][0]["dup"] == 2
    assert d["unmatched"] == 1


def test_l1_match_types_new_ops():
    """5.2 新增匹配类型：枚举 in/枚举皆非/布尔开关/时段区间 + 引擎 != 运算符。

    回归点：(1) 新运算符在 _eval_cond 里真的生效（不是静默失效）；
            (2) build_when 产出的 when 结构能被 decide() 直接消费。
    """
    from src.gateway.policy import _match_when
    from src.gateway import policy_admin

    ctx = {
        "model": "AB-flash",
        "client": {"key": "workbuddy"},
        "time": {"hour": 23},
        "session": {"confidential": False},
        "findings": {"parse_failed": True},
    }
    # 枚举任一 / 枚举皆非（大小写与空格不敏感）：引擎保留运算符，
    # 构造器下拉已精简不提供 model/client 组合，直接手写 when 测
    assert _match_when({"model in": ["ab-flash", "qwen3"]}, ctx)
    assert not _match_when({"model not_in": ["AB-flash", "qwen3"]}, ctx)
    assert _match_when({"client.key not_in": ["ccswitch", "codex"]}, ctx)
    # 布尔开关（文本别名 1/true/是 皆真，其余假）
    assert _match_when(policy_admin.build_when("parse_failed", "bool", "是"), ctx)
    assert not _match_when(policy_admin.build_when("session_confidential", "bool", "true"), ctx)
    # 时段区间：22-6 跨午夜 -> any（23 点命中）；9-18 同日 -> all（23 点不命中）
    assert _match_when(policy_admin.build_when("prompt", "hour_range", "22-6"), ctx)
    assert not _match_when(policy_admin.build_when("prompt", "hour_range", "9-18"), ctx)
    # 引擎 != （只可手写 when，构造器无等价运算符）
    assert _match_when({"model !=": "ccswitch"}, ctx)
    assert not _match_when({"model !=": "AB-flash"}, ctx)
    # 老运算符不受影响（!= 插入不得抢 ==/>=/contains_any 的匹配）
    assert _match_when({"model ==": "AB-flash"}, ctx)
    assert _match_when({"time.hour >=": 22}, ctx)
    assert _match_when({"model contains_any": ["flash"]}, ctx)
    # 非法输入要报出来，不能静默产出永不命中的规则
    import pytest as _pytest
    for bad in [("prompt", "contains", ""), ("prompt", "hour_range", "22"), ("prompt", "hour_range", "8-8"),
                ("prompt", "hour_range", "9-99"), ("nope", "in", "x")]:
        with _pytest.raises(ValueError):
            policy_admin.build_when(*bad)
    # 白名单必须跟着映射表走（BuildWhenReq 的 pattern 曾与映射表脱钩 ⇒ 新类型全被 422 挡掉）
    for ct, mt, mv in [("prompt", "regex", "AKIA[0-9A-Z]{16}"), ("filename", "contains", "salary"),
                       ("parse_failed", "bool", "true"), ("prompt", "hour_range", "22-6")]:
        r = client.post("/admin/api/policies/build-when",
                        json={"content_type": ct, "match_type": mt, "match_value": mv})
        assert r.status_code == 200, f"{ct}/{mt} 被 BuildWhenReq 挡掉: {r.text}"
    assert client.post("/admin/api/policies/build-when",
                       json={"content_type": "nope", "match_type": "in",
                             "match_value": "x"}).status_code == 422
    assert client.post("/admin/api/policies/build-when",
                       json={"content_type": "model", "match_type": "nope",
                             "match_value": "x"}).status_code == 422
    # 精简出下拉的组合：引擎支持不变（手写 yaml 有效），构造器拒
    assert client.post("/admin/api/policies/build-when",
                       json={"content_type": "model", "match_type": "in",
                             "match_value": "x"}).status_code == 422


def test_match_choices_reject_never_matching_combos():
    """构造器不再拼笛卡尔积：布尔字段配正则/包含会产出永不命中的 when。

    实测 12 内容类型 × 7 匹配类型 = 66 种组合里 10 种是这种静默失效
    （session.confidential matches_regex 之类，引擎恒 False，保存成功、
    干跑通过、线上永不触发）。现在这类组合在 build_when 层就拒。
    """
    from src.gateway import policy_admin
    import pytest

    bool_fields = ("session_confidential", "tainted_file", "unparsed_binary",
                   "parse_failed", "ocr_empty")
    for ct in bool_fields:
        for mt in ("regex", "contains", "in", "not_in"):
            assert not policy_admin.match_pair_ok(ct, mt), f"{ct}/{mt} 会静默产出永不命中的规则"
            with pytest.raises(ValueError):
                policy_admin.build_when(ct, mt, "x")
    # 合法组合不许被误伤（含 self-field 的 size / hour_range 不绑内容类型）
    for ct, mt in policy_admin.MATCH_PAIRS:
        assert policy_admin.match_pair_ok(ct, mt)
    for mt in ("size", "hour_range"):
        assert policy_admin.match_pair_ok("prompt", mt)
    # 前端下拉的数据源与后端唯一真源一致：每个 choice 都能真的构造出 when
    r = client.get("/admin/api/policies/match-choices")
    assert r.status_code == 200
    d = r.json()
    assert d["choices"] and d["self_field"]
    for c in d["choices"] + d["self_field"]:
        assert policy_admin.match_pair_ok(c["content_type"], c["match_type"]), c
        assert c["key"] == f'{c["content_type"]}|{c["match_type"]}', c
        assert c["label"]
    # 前端拿到的组合走 build-when 必须 200（不会再有 422 的下拉项）
    for c in d["choices"][:4] + d["self_field"]:
        rr = client.post("/admin/api/policies/build-when",
                         json={"content_type": c["content_type"], "match_type": c["match_type"],
                                   "match_value": "1" if c["match_type"] == "size" else
                                                  ("22-6" if c["match_type"] == "hour_range" else
                                                   ("60" if c["match_type"] == "pii_score" else "x"))})
        assert rr.status_code == 200, f"{c['key']} 在下拉里但 build-when 报 {rr.status_code}: {rr.text}"


def test_parse_when_roundtrip_and_switch_rules():
    """parse_when 是 build_when 的逆：编辑已有规则时把 when 预填回构造器。

    回归点：(1) build_when 产物能逆解析回等价构造器控件值；
            (2) policy.yaml 里值嵌 key 的开关规则（"session.confidential == true": true）能识别；
            (3) 认不出的形状（多条件 / != / 非开关 == / 裸 client 字段）返回 None，
                调用方保持构造器空白——绝不拿默认值去覆盖用户已写好的 when。
    """
    from src.gateway import policy_admin

    # build_when -> parse_when 往返
    for args in [("filename", "contains", "a, b"), ("prompt", "regex", "AKIA"),
                 ("prompt", "contains", "x,y"), ("watermark", "contains", "a,b"),
                 ("session_confidential", "bool", "是"), ("parse_failed", "bool", "false")]:
        w = policy_admin.build_when(*args)
        p = policy_admin.parse_when(w)
        assert p, f"build_when 产物无法逆解析: {w}"
        assert p["content_type"] == args[0] and p["match_type"] == args[1], (args, p)
    # 列表值回成逗号分隔串
    assert policy_admin.parse_when({"text contains_any": ["a", "b"]})["match_value"] == "a, b"
    # policy.yaml 值嵌 key 的开关规则（引擎只取 dict value）
    p = policy_admin.parse_when({"any": [{"session.confidential == true": True}]})
    assert p == {"content_type": "session_confidential", "match_type": "bool", "match_value": "true"}
    assert policy_admin.parse_when({"tainted_file.hit == true": True})["content_type"] == "tainted_file"
    assert policy_admin.parse_when({"findings.parse_failed == false": False})["match_value"] == "false"
    # self-field：文件大小
    assert policy_admin.parse_when({"file.size >=": 100})["match_type"] == "size"
    # 多条件 any/all -> 条件组形状（每行都能逆解析才还原，供构造器多行预填）
    p = policy_admin.parse_when({"any": [{"session.confidential == true": True}, {"tainted_file.hit == true": True}]})
    assert p == {"combiner": "any", "rows": [
        {"content_type": "session_confidential", "match_type": "bool", "match_value": "true"},
        {"content_type": "tainted_file", "match_type": "bool", "match_value": "true"}]}
    assert policy_admin.parse_when({"all": [{"text contains_any": ["a"]}, {"file.size >=": 100}]})["combiner"] == "all"
    # 认不出 -> None（保持空白，不覆盖）
    for w in (
        {"any": [{"model !=": "x"}, {"prompt contains_any": ["k"]}]},  # 组内有非构造器运算符
        {"any": [{"any": [{"prompt contains_any": ["k"]}, {"model in": ["a"]}]},
                 {"file.size >=": 100}]},                               # 组内嵌套条件组
        {"model !=": "x"},        # 无构造器等价物
        {"model ==": "x"},        # 非开关字段的 ==
        {"model in": ["a"]},      # 精简出构造器的组合（引擎有效，不再预填）
        {"client not_in": ["x"]},  # 裸 client（构造器写的是 client.key）
        "always", None, {"when": 1},
    ):
        assert policy_admin.parse_when(w) is None, w
    # 端点
    r = client.post("/admin/api/policies/parse-when",
                    json={"when": {"any": [{"tainted_file.hit == true": True}]}})
    assert r.status_code == 200 and r.json()["parsed"]["content_type"] == "tainted_file"
    assert client.post("/admin/api/policies/parse-when", json={"when": "always"}).json()["parsed"] is None


def test_build_when_group_multi_conditions():
    """条件组（多行 any/all）：现网多条件规则（financial_local_only 等）可构造 + 可逆解析。

    回归点：(1) 单行退化为单条件（与老 match 同形状，兼容）；
            (2) 多行产出 {"any"/"all": [...]} 且被 decide 直接消费；
            (3) 组内逐行走 build_when 校验（非法组合/hour_range 拒）；
            (4) build-when-group 端点与 parse-when 组形状往返。
    """
    from src.gateway.policy import _match_when
    from src.gateway import policy_admin
    import pytest

    # 单行退化：不加 any 壳，与 build_when 同形状
    assert policy_admin.build_when_group("any", [
        {"content_type": "prompt", "match_type": "contains", "match_value": "a,b"}]) == {"text contains_any": ["a", "b"]}
    # 多行 any：现网 financial_local_only 形状（4 行，含 sheet_names/headers 新字段）
    w = policy_admin.build_when_group("any", [
        {"content_type": "sheet_names", "match_type": "contains", "match_value": "工资, 财务"},
        {"content_type": "headers", "match_type": "contains", "match_value": "姓名, 金额"},
        {"content_type": "filename", "match_type": "contains", "match_value": "salary"},
        {"content_type": "prompt", "match_type": "contains", "match_value": "财务报表"},
    ])
    assert w["any"][0] == {"file.sheet_names contains_any": ["工资", "财务"]}
    assert w["any"][1] == {"file.headers contains_any": ["姓名", "金额"]}
    # 引擎直接消费：表名命中 -> True；全不沾 -> False
    ctx = {"file": {"sheet_names": ["Sheet1", "工资表"], "headers": [], "text": "", "watermark": "", "filename": ""}}
    assert _match_when(w, ctx)
    assert not _match_when(w, {"file": {"sheet_names": ["S1"], "headers": [], "text": "", "watermark": "", "filename": "a.txt"}})
    # all：两行布尔开关（unparsed_binary_route_local 形状）
    w = policy_admin.build_when_group("all", [
        {"content_type": "unparsed_binary", "match_type": "bool", "match_value": "true"},
        {"content_type": "parse_failed", "match_type": "bool", "match_value": "true"},
    ])
    assert w == {"all": [{"findings.unparsed_binary ==": True}, {"findings.parse_failed ==": True}]}
    # hour_range 自身就是 any/all 壳，展不平进组 -> 拒
    with pytest.raises(ValueError):
        policy_admin.build_when_group("any", [
            {"content_type": "prompt", "match_type": "hour_range", "match_value": "9-18"},
            {"content_type": "prompt", "match_type": "contains", "match_value": "x"}])
    # 组内非法组合照拒（逐行走 build_when 校验，不放宽）
    with pytest.raises(ValueError):
        policy_admin.build_when_group("any", [
            {"content_type": "session_confidential", "match_type": "regex", "match_value": "x"},
            {"content_type": "prompt", "match_type": "contains", "match_value": "x"}])
    # 组 -> parse_when 往返（组形状）
    g = policy_admin.parse_when(policy_admin.build_when_group("any", [
        {"content_type": "headers", "match_type": "contains", "match_value": "姓名, 金额"},
        {"content_type": "watermark", "match_type": "contains", "match_value": "机密"},
    ]))
    assert g == {"combiner": "any", "rows": [
        {"content_type": "headers", "match_type": "contains", "match_value": "姓名, 金额"},
        {"content_type": "watermark", "match_type": "contains", "match_value": "机密"}]}
    # 下拉新增三个文件字段，且都能真的构造出 when（match-choices 唯一真源）
    r = client.get("/admin/api/policies/match-choices")
    cts = {c["content_type"] for c in r.json()["choices"]}
    assert {"sheet_names", "headers", "watermark"} <= cts
    for c in r.json()["choices"]:
        if c["content_type"] in ("sheet_names", "headers", "watermark"):
            rr = client.post("/admin/api/policies/build-when",
                             json={"content_type": c["content_type"], "match_type": c["match_type"],
                                   "match_value": "工资|salary" if c["match_type"] == "regex" else "工资, 预算"})
            assert rr.status_code == 200, rr.text
    # 端点
        r = client.post("/admin/api/policies/build-when-group", json={
            "combiner": "any",
            "rows": [{"content_type": "headers", "match_type": "contains", "match_value": "姓名, 金额"},
                     {"content_type": "filename", "match_type": "contains", "match_value": "工资, salary"}]})
    assert r.status_code == 200
    assert r.json()["when"] == {"any": [{"file.headers contains_any": ["姓名", "金额"]},
                                        {"file.filename contains_any": ["工资", "salary"]}]}
    assert client.post("/admin/api/policies/build-when-group", json={
        "combiner": "nope", "rows": [{"content_type": "prompt", "match_type": "contains", "match_value": "x"}]}).status_code == 422
    assert client.post("/admin/api/policies/build-when-group", json={"combiner": "any", "rows": []}).status_code == 422


def test_rule_ai_parse_suggestion():
    """AI 生成匹配条件：模型输出是幻觉高危区，parse_suggestion 必须逐行校验。

    回归点：(1) rows/combiner 新格式校验通过并归一化（regex 留串/contains 归列表）；
            (2) 旧格式 {match_type, value} 按 prompt 单行兜底（提示词升级期兼容）；
            (3) 幻觉字段/坏正则/布尔配非布尔/空值/空组/坏 combiner 整组拒；
            (4) 提示词字段表与构造器同源（AI 可选集=构造器可用集）。
    """
    from src.gateway import rule_ai
    from src.gateway import policy_admin
    import pytest as _pytest

    r = rule_ai.parse_suggestion(
        '```json\n' + '{"rows": [{"content_type": "sheet_names", "match_type": "contains", "value": ["工资"]},'
        ' {"content_type": "prompt", "match_type": "regex", "value": "AKIA[0-9A-Z]{16}"}],'
        ' "combiner": "any", "explanation": "财务"}\n```')
    assert r["combiner"] == "any" and len(r["rows"]) == 2
    assert r["rows"][0]["value"] == ["工资"]
    assert r["rows"][1]["value"] == "AKIA[0-9A-Z]{16}"
    # 旧格式兼容
    r = rule_ai.parse_suggestion('{"match_type": "contains", "value": ["机密"], "explanation": "old"}')
    assert r["rows"] == [{"content_type": "prompt", "match_type": "contains", "value": ["机密"]}]
    # 字段表与构造器同源
    for ct in dict.fromkeys(c for c, *_ in policy_admin.MATCH_CHOICES):
        assert ct in rule_ai._FIELD_TABLE, ct
    for bad in [
        '{"rows": [{"content_type": "nope", "match_type": "contains", "value": "x"}], "combiner": "any"}',
        '{"rows": [{"content_type": "prompt", "match_type": "regex", "value": "AKIA["}], "combiner": "any"}',
        '{"rows": [{"content_type": "session_confidential", "match_type": "regex", "value": "x"}], "combiner": "any"}',
        '{"rows": [{"content_type": "prompt", "match_type": "contains", "value": ""}], "combiner": "any"}',
        '{"rows": [], "combiner": "any"}',
        '{"rows": [{"content_type": "prompt", "match_type": "contains", "value": "x"}], "combiner": "nope"}',
        '这不是 JSON',
    ]:
        with _pytest.raises(RuntimeError):
            rule_ai.parse_suggestion(bad)


def test_rule_ai_exclude_user_message():
    """「换一组」：exclude 非空时 user 消息带上要避开的词；空/空白项不产生约束。"""
    from src.gateway import rule_ai

    assert rule_ai._gen_user_message("工资表") == "工资表"
    assert rule_ai._gen_user_message("工资表", None) == "工资表"
    assert rule_ai._gen_user_message("工资表", ["", "  "]) == "工资表"
    m = rule_ai._gen_user_message("工资表", ["工资", "薪资", " "])
    assert m.startswith("工资表") and "换一组" in m and "工资、薪资" in m
    # 超长排除列表截断，不拖爆提示词
    big = [f"词{i}" for i in range(100)]
    assert rule_ai._gen_user_message("x", big).count("、") == 39


def test_rule_ai_scopes_message_and_check():
    """覆盖范围：scopes 非空时 user 消息带限定；越界字段整组拒；空=不限。"""
    import pytest as _pytest
    from src.gateway import rule_ai

    assert rule_ai._gen_user_message("工资表", scopes=None) == "工资表"
    m = rule_ai._gen_user_message("工资表", scopes=["filename", "prompt"])
    assert "覆盖范围限定" in m and "filename、prompt" in m
    rows = [{"content_type": "filename", "match_type": "contains", "value": ["工资"]}]
    rule_ai._check_scopes(rows, ["filename"])  # 范围内不抛
    rule_ai._check_scopes(rows, None)          # 不限不抛
    with _pytest.raises(RuntimeError):
        rule_ai._check_scopes(rows, ["prompt"])


def test_policy_test_endpoint_matches_model_client_hour(tmp_path, monkeypatch):
    """规则测试区（干跑）不再对 model/client.key/time.hour 假阴性。

    这三个字段 2026-10-08 进 ctx 与 build_when，但 test_rule 曾恒填空 ⇒
    按模型/客户端/时段写的规则在干跑里永远不命中，看着像「规则没生效」。
    """
    from src.gateway import policy_admin
    from src.gateway.policy import invalidate_policy_cache

    pol = tmp_path / "policy_test_dryrun.yaml"
    pol.write_text(
        "policies:\n"
        "  - name: dryrun-model\n    priority: 5\n    action: block\n"
        "    when:\n      any:\n      - model in: [AB-flash]\n"
        "  - name: dryrun-hour\n    priority: 6\n    action: block\n"
        # 注意必须写 ">=: 22"：裸的 "- time.hour >= 22" 会被 YAML 解析成字符串
        # 列表元素，_eval_cond 拿到 str 会 AttributeError（生产 policy.yaml 同理）
        "    when:\n      any:\n      - time.hour >=: 22\n"
        "  - name: base-allow\n    priority: 100\n    action: allow\n    when: always\n",
        encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_POLICY_PATH", str(pol))
    invalidate_policy_cache()

    r = client.post("/admin/api/policies/test",
                    json={"text": "无关内容", "model": "AB-flash"})
    assert r.status_code == 200, r.text
    assert r.json()["rule"] == "dryrun-model", r.text

    r = client.post("/admin/api/policies/test",
                    json={"text": "无关内容", "model": "qwen3", "hour": 23})
    assert r.json()["rule"] == "dryrun-hour", r.text

    r = client.post("/admin/api/policies/test",
                    json={"text": "无关内容", "model": "qwen3", "hour": 10})
    assert r.json()["rule"] == "base-allow", r.text

    # hour 越界要 422，别默默当 0 点用
    assert client.post("/admin/api/policies/test",
                       json={"text": "x", "hour": 24}).status_code == 422
    # hour 缺省 = 当前小时（服务端回落，不是 0）
    assert client.post("/admin/api/policies/test", json={"text": "x"}).status_code == 200


def test_policy_test_dryrun_carries_findings(tmp_path, monkeypatch):
    """干跑必须带 findings，否则按 weighted_pii_score 判的规则永不命中（假阴性）。

    复刻生产实况：AKIA 密钥样本 weighted_pii_score=90 ⇒ priority 5 的
    「pii_weighted_route_local」route_local 先命中；干跑曾恒填 findings={} ⇒ 报成 priority 12
    的密钥 block，与真实请求不一致。
    """
    from src.gateway.policy import invalidate_policy_cache

    pol = tmp_path / "policy_dryrun_findings.yaml"
    pol.write_text(
        "policies:\n"
        "  - name: pii-score-rule\n    priority: 5\n    action: route_local\n"
        "    when:\n      any:\n      - findings.weighted_pii_score >=: 60\n"
        "  - name: key-rule\n    priority: 12\n    action: block\n"
        "    when:\n      any:\n      - text matches_regex: AKIA[0-9A-Z]{16}\n"
        "  - name: base-allow\n    priority: 100\n    action: allow\n    when: always\n",
        encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_POLICY_PATH", str(pol))
    invalidate_policy_cache()

    r = client.post("/admin/api/policies/test",
                    json={"text": "我的 AKIAIOSFODNN7EXAMPLE 泄露了"})
    assert r.status_code == 200, r.text
    assert r.json()["rule"] == "pii-score-rule", r.text
