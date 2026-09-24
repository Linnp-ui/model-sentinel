"""
pytest 快速验证 - 无需 postgres/redis
tests/test_gateway.py
"""
from fastapi.testclient import TestClient
from src.gateway.main import app

client = TestClient(app)
KEY = "sk_test_dummy0123456789abcdef0123456789abcdef01"
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
    # 规则4：触发次数高的 L1 规则应排前面；一次算出全量顺序，一键应用全排到底后收敛
    import yaml
    from src.gateway import policy as _pol
    from src.gateway.admin_store import get_admin_store
    p = tmp_path / "policy.yaml"
    p.write_text(yaml.safe_dump({"policies": [
        {"name": "rule_lo", "priority": 10, "action": "route_local",
          "when": {"any": [{"text contains_any": ["lo"]}]},
          "target": {"provider": "vllm_local", "model": "qwen2.5:7b"}},
        {"name": "rule_mid", "priority": 20, "action": "block",
          "when": {"any": [{"text contains_any": ["mid"]}]}},
        {"name": "rule_hi", "priority": 30, "action": "block",
          "when": {"any": [{"text contains_any": ["hi"]}]}},
    ]}, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_POLICY_PATH", str(p))
    _pol.invalidate_policy_cache()
    store = get_admin_store()
    try:
        seed = lambda reason, n: [store.insert_request_log(
            {"key_name": "pytest-reorder", "action": "block", "model": "m",
             "provider": "p", "client_ip": "9.9.9.9", "blocked_reason": reason})
            for _ in range(n)]
        seed("l1:rule_hi", 30)
        seed("l1:rule_lo", 20)
        seed("l1:rule_mid", 10)
        items = client.get("/admin/api/suggestions", params={"hours": 168}).json()["items"]
        ro = [s for s in items if (s.get("apply") or {}).get("kind") == "reorder"]
        assert len(ro) == 1 and ro[0]["apply"].get("order") == ["rule_hi", "rule_lo", "rule_mid"], items
        r = client.post("/admin/api/suggestions/apply", json=ro[0]["apply"])
        assert r.status_code == 200, r.text
        prio = {x.name: x.priority for x in _pol.load_policy()}
        assert (prio["rule_hi"], prio["rule_lo"], prio["rule_mid"]) == (10, 20, 30), prio
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
        assert len(lim) == 1, items
        ap = lim[0]["apply"]
        assert ap["limit_model"] == "hot-prov/hot-model" and ap["limit_rpm"] > 0
        r = client.post("/admin/api/suggestions/apply", json={"kind": "add_limit", **ap})
        assert r.status_code == 200, r.text
        assert _mp.model_rpm("hot-prov", "hot-model") == ap["limit_rpm"]
        items2 = client.get("/admin/api/suggestions", params={"hours": 168}).json()["items"]
        lim2 = [s for s in items2 if (s.get("apply") or {}).get("kind") == "add_limit"]
        assert all(s["apply"]["limit_model"] != "hot-prov/hot-model" for s in lim2), items2
    finally:
        _mp.invalidate_model_policy_cache()

def test_window_thresholds_parse_and_pick(monkeypatch):
    # 规则1/2 按窗口取阈值：'24:20,168:100,720:500'；纯数字=全窗口同值；未登记窗口取最近
    from src.gateway import stats_service as ss
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD", "24:20,168:100,720:500")
    m = ss._window_thresholds("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD", "AI_GATEWAY_SUGGEST_BLOCK_THRESHOLD", 20)
    assert m == {24: 20, 168: 100, 720: 500}
    assert ss.pick_window(m, 24, 0) == 20
    assert ss.pick_window(m, 168, 0) == 100
    assert ss.pick_window(m, 720, 0) == 500
    assert ss.pick_window(m, 48, 0) == 20  # 未登记窗口取最近：|48-24|=24 < |48-168|=120
    # 纯数字 = 全窗口同值
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD", "33")
    m2 = ss._window_thresholds("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD", "AI_GATEWAY_SUGGEST_BLOCK_THRESHOLD", 20)
    assert m2 == {"*": 33} and ss.pick_window(m2, 168, 0) == 33
    # 非法 → 回落默认
    monkeypatch.setenv("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD", "abc")
    m3 = ss._window_thresholds("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD", "AI_GATEWAY_SUGGEST_BLOCK_THRESHOLD", 20)
    assert m3 == {"*": 20}

def test_l2_config_suggest_window_roundtrip(tmp_path, monkeypatch):
    # /l2-config：两项阈值按窗口 {24/168/720} 存取；PUT 传 dict → env '24:20,168:100,720:500'
    import os
    monkeypatch.setenv("AI_GATEWAY_L2_OVERRIDES_PATH", str(tmp_path / "ov.yaml"))
    saved = {k: os.environ.get(k) for k in
             ("AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD", "AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD")}
    try:
        r = client.put("/admin/api/l2-config", json={
            "AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD": {"24": 20, "168": 100, "720": 500},
            "AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD": {"24": 100, "168": 2000, "720": 9000},
        })
        assert r.status_code == 200, r.text
        assert os.environ["AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD"] == "24:20,168:100,720:500"
        assert os.environ["AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD"] == "24:100,168:2000,720:9000"
        d = client.get("/admin/api/l2-config").json()
        assert d["AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD"] == {"24": 20, "168": 100, "720": 500}
        assert d["AI_GATEWAY_SUGGEST_VOLUME_THRESHOLD"] == {"24": 100, "168": 2000, "720": 9000}
        # 越界 422
        r2 = client.put("/admin/api/l2-config", json={"AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD": {"24": 0}})
        assert r2.status_code == 422
        # 纯数字仍兼容（全窗口同值）
        r3 = client.put("/admin/api/l2-config", json={"AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD": 7})
        assert r3.status_code == 200 and os.environ["AI_GATEWAY_SUGGEST_VIOLATION_THRESHOLD"] == "24:7,168:7,720:7"
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
