"""
扩展测试集 - 覆盖 L1 规则 + L2 小模型 + 文件 + 网关特性 + 延迟
tests/test_expanded.py
运行: .\.venv\Scripts\python.exe -m pytest tests/test_expanded.py -v
      .\.venv\Scripts\python.exe -m pytest tests/test_expanded.py -k "not live" -v  # 仅离线
"""
import io
import time
from pathlib import Path
import asyncio
import openpyxl
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

from src.gateway.main import app

client = TestClient(app)
KEY = "sk_shGD8A0lZpIJbcGavN8z6GBcSl-DGIsecJqgF4r0k_o"
H = {"Authorization": f"Bearer {KEY}"}
H_X = {"x-api-key": KEY}

# ---------- helpers ----------
def _xlsx(sheet_name, headers, rows, filename="test.xlsx"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf, filename

def _pdf(name):
    # 静态 fixture（须含 CJK 文本层；重新生成见 git 历史 reportlab 脚本）
    buf = io.BytesIO((Path(__file__).parent / "fixtures" / name).read_bytes())
    return buf

# ---------- 1. L1 规则矩阵 ----------
@pytest.mark.parametrize("content,exp_action,exp_rule", [
    ("财务报表", "route_local", "financial_local_only"),
    ("工资表", "allow", "default_allow"),  # 纯文本"工资表"已移出 L1（文件维度仍拦）
    ("资产负债", "route_local", "financial_local_only"),
    ("利润表", "route_local", "financial_local_only"),
    ("员工张三 身份证 110101199001011237", "route_local", "pii_weighted_route_local"),
    ("手机号 13812345678", "route_local", "pii_weighted_route_local"),  # phone pattern 60分单命中（旧名 pii_local_only 已改名）
    ("我的AKIAIOSFODNN7EXAMPLE泄露", "route_local", "block_secrets"),  # fallback_local -> route_local
    ("-----BEGIN PRIVATE KEY----- abc", "route_local", "block_secrets"),
    ("sk-ant-123456789", "route_local", "block_secrets"),
    ("总结CAP定理", "allow", "default_allow"),
    ("hello world", "allow", "default_allow"),
    ("今天天气不错", "allow", "default_allow"),
])
def test_chat_l1_matrix(content, exp_action, exp_rule):
    r = client.post("/v1/chat/completions", headers=H, json={"model":"gpt-4o-mini","messages":[{"role":"user","content":content}]})
    assert r.status_code == 200
    gw = r.json()["gateway"]
    assert gw["action"] == exp_action
    # block_secrets 在离线 conftest 下为 fallback_local，会显示 downgraded_from
    if exp_rule == "block_secrets":
        assert gw["local"] is True
    else:
        assert gw["policy_rule"] == exp_rule

def test_chat_short_not_trigger_l2():
    """短文本无灰区不触发L2（网关层直接 L1 allow；2026-09-23 起取消字数触发）"""
    r = client.post("/v1/chat/completions", headers=H, json={"model":"gpt-4o-mini","messages":[{"role":"user","content":"张三你好吗今天"}]})
    assert r.status_code == 200
    gw = r.json()["gateway"]
    assert gw["action"] == "allow" and "l2" not in gw

def test_chat_short_gray_triggers_l2():
    """短文本进灰区也触发L2（不计字数；mock 下 L2 恒 NORMAL 故仍 allow）"""
    r = client.post("/v1/chat/completions", headers=H, json={"model":"gpt-4o-mini","messages":[{"role":"user","content":"张三工资15000"}]})
    assert r.status_code == 200
    gw = r.json()["gateway"]
    assert gw["action"] == "allow" and "l2" in gw

# ---------- 2. 文件 L1 ----------
@pytest.mark.parametrize("sheet,headers,rows,filename,exp_rule", [
    ("工资表2024", ["姓名","身份证","工资"], [["张三","110101199001011234","30000"]], "salary.xlsx", "financial_local_only"),
    ("Sheet1", ["姓名","工资"], [["李四","28000"]], "工资表.xlsx", "financial_local_only"),  # 文件名命中
    ("预算表", ["姓名","工资"], [["王五","42000"]], "data.xlsx", "financial_local_only"),  # sheet名命中
    ("Sheet1", ["姓名","工资"], [["赵六","5000"]], "cost_report.xlsx", "financial_local_only"),  # cost 命中
    ("Sheet1", ["Name","Age"], [["Alice","30"]], "normal.xlsx", "default_allow"),
    ("Sheet1", ["Product","Price"], [["A","100"]], "price.xlsx", "default_allow"),
])
def test_file_financial_matrix(sheet, headers, rows, filename, exp_rule):
    buf, fn = _xlsx(sheet, headers, rows, filename)
    r = client.post("/v1/files/check", headers=H, files={"file": (fn, buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200
    gw = r.json()["gateway"]
    if exp_rule == "default_allow":
        assert gw["action"] == "allow"
    else:
        assert gw["action"] == "route_local"
        assert gw["policy_rule"] == exp_rule

def test_file_drawing_confidential():
    pdf = _pdf("drawing_confidential.pdf")
    r = client.post("/v1/files/check", headers=H, files={"file": ("drawing.pdf", pdf, "application/pdf")})
    assert r.status_code == 200
    assert r.json()["gateway"]["policy_rule"] == "drawing_local_only"
    assert r.json()["gateway"]["action"] == "route_local"

def test_file_drawing_normal():
    pdf = _pdf("drawing_normal.pdf")
    # 无 confidential 关键词，可能走 L2 或 allow，取决于实现
    r = client.post("/v1/files/check", headers=H, files={"file": ("normal.pdf", pdf, "application/pdf")})
    assert r.status_code == 200
    # 不强制断言，仅确保不崩
    assert "gateway" in r.json()

def test_file_empty():
    buf, fn = _xlsx("Sheet1", ["A"], [], "empty.xlsx")
    r = client.post("/v1/files/check", headers=H, files={"file": (fn, buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200

# ---------- 3. L2 小模型（mock） ----------
# conftest 默认 SMALL_MODEL_ENABLED=false，这里 patch 为 true 并 mock classify
def test_l2_mock_confidential():
    mock_res = {"label":"CONFIDENTIAL","confidence":0.95,"reason":"mock salary","latency_ms":120,"raw":"mock"}
    with patch("src.gateway.main.small_classify", new=AsyncMock(return_value=mock_res)):
        with patch("src.gateway.main.is_confidential", return_value=True):
            r = client.post("/v1/chat/completions", headers=H, json={"model":"gpt-4o-mini","messages":[{"role":"user","content":"这是进灰区的文本（仅含工资一词得20分），用于触发L2小模型分类mock，不计字数只看灰区"}]})
            assert r.status_code == 200
            gw = r.json()["gateway"]
            assert gw["layer"] == "L2"
            assert gw["policy_rule"] == "l2_confidential"
            assert gw["action"] == "route_local"

def test_l2_mock_normal():
    mock_res = {"label":"NORMAL","confidence":0.9,"reason":"mock normal","latency_ms":100,"raw":"mock"}
    with patch("src.gateway.main.small_classify", new=AsyncMock(return_value=mock_res)):
        with patch("src.gateway.main.is_confidential", return_value=False):
            r = client.post("/v1/chat/completions", headers=H, json={"model":"gpt-4o-mini","messages":[{"role":"user","content":"请详细介绍CAP定理的三个特性以及在分布式系统中的应用场景和权衡，另外工资发放流程是怎样的"}]})
            assert r.status_code == 200
            gw = r.json()["gateway"]
            assert gw["action"] == "allow"
            assert gw["l2"]["label"] == "NORMAL"

def test_l2_cache_hit():
    """同一文本二次请求应命中缓存 latency 0"""
    import time
    from src.gateway.small_model import _CACHE, _cache_key
    # 直接测 small_model 缓存结构（离线 mock 已关 L2，这里仅测缓存结构）
    assert isinstance(_CACHE, dict)

# ---------- 3b. 文件名污染（客户端无感会话替代：不依赖 session 头，按 key 隔离） ----------
def test_tainted_filename_forces_local_no_session_header():
    """传机密文件被拦后，同 key 无 session 头追问文件名即 route_local（tainted_file_ref）。"""
    fn = "financial_taintprobe.xlsx"
    buf, _ = _xlsx("Sheet1", ["Name", "Amount"], [["Alice", "30"]], fn)
    r1 = client.post("/v1/files/check", headers=H, files={"file": (fn, buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r1.status_code == 200
    assert r1.json()["gateway"]["action"] == "route_local"  # 文件名命中 financial
    # 无 session 头、无关键词的追问（financial 非聊天关键词，risk 0）
    r2 = client.post("/v1/chat/completions", headers=H, json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "帮我看看financial_taintprobe.xlsx里的总数"}]})
    assert r2.status_code == 200
    gw = r2.json()["gateway"]
    assert gw["action"] == "route_local"
    assert gw["policy_rule"] == "tainted_file_ref"
    # 未污染文件名对照：同句式直接放行
    r3 = client.post("/v1/chat/completions", headers=H, json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "帮我看看financial_cleancase.xlsx里的总数"}]})
    assert r3.status_code == 200
    assert r3.json()["gateway"]["action"] == "allow"

def test_taint_store_unit():
    """store 直测：大小写不敏感、中文前缀 mention 后缀命中、按 key 隔离、空 key/空名静默跳过。"""
    from src.gateway.session_store import get_session_store
    store = get_session_store()
    store.mark_tainted_file("k-taint-a", "Salary_Taint.XLSX", "financial_local_only")
    assert store.is_tainted_file("k-taint-a", "salary_taint.xlsx") == "salary_taint.xlsx"
    assert store.is_tainted_file("k-taint-a", "帮我看看salary_taint.xlsx") == "salary_taint.xlsx"
    assert store.is_tainted_file("k-taint-b", "salary_taint.xlsx") == ""
    store.mark_tainted_file("", "salary_taint.xlsx")
    store.mark_tainted_file("k-taint-a", "")
    assert store.is_tainted_file("", "salary_taint.xlsx") == ""

def test_l2_laya_backend_and_shadow(monkeypatch):
    """laya /classify 后端解析 + 影子双跑：影子只留痕不影响主判定，影子挂了只丢影子数据。"""
    import asyncio
    from src.gateway import small_model as sm

    class _Resp:
        def __init__(self, payload):
            self._p = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self._p

    class _Stub:
        def __init__(self, shadow_ok=True):
            self.shadow_ok = shadow_ok
            self.calls = []
        async def post(self, url, json=None, timeout=None):
            self.calls.append((url, json, timeout))
            if url.endswith("/classify"):
                if not self.shadow_ok:
                    raise RuntimeError("connect refused")
                return _Resp({"label": "CONFIDENTIAL", "confidence": 0.91,
                               "probabilities": {"CONFIDENTIAL": 0.91, "NORMAL": 0.09},
                               "reason": "laya-multilingual CONFIDENTIAL p=0.910", "latency_ms": 41})
            return _Resp({"choices": [{"message": {"content": '{"label":"NORMAL","confidence":0.9,"reason":"ok"}'}}]})

    text = "某员工工资表 姓名 金额"
    kw = dict(filename="salary.xlsx", headers=["姓名", "金额"], sheet_names=["1月"])
    monkeypatch.setenv("SMALL_MODEL_ENABLED", "true")
    monkeypatch.setenv("SMALL_MODEL_URL", "http://127.0.0.1:8002/v1/chat/completions")
    monkeypatch.setenv("AI_GATEWAY_L2_SHADOW_URL", "http://127.0.0.1:8003/classify")
    try:
        # 1) qwen 主 + laya 影子：主判 NORMAL 权威，影子 CONFIDENTIAL 只留痕
        monkeypatch.setenv("AI_GATEWAY_L2_BACKEND", "qwen")
        monkeypatch.setenv("AI_GATEWAY_L2_SHADOW", "true")
        sm._CACHE.clear()
        stub = _Stub()
        monkeypatch.setattr(sm, "_get_client", lambda: stub)
        r = asyncio.run(sm.classify(text, **kw))
        assert r["label"] == "NORMAL" and not r.get("degraded")
        assert r["shadow_label"] == "CONFIDENTIAL" and r["shadow_conf"] == 0.91
        assert r["shadow_probabilities"]["NORMAL"] == 0.09
        assert stub.calls[0][0].endswith("/v1/chat/completions")
        assert stub.calls[1][0] == "http://127.0.0.1:8003/classify" and stub.calls[1][2] == 2.0

        # 2) 影子端点挂了：主判定不受影响，shadow_degraded 留痕
        sm._CACHE.clear()
        stub2 = _Stub(shadow_ok=False)
        monkeypatch.setattr(sm, "_get_client", lambda: stub2)
        r2 = asyncio.run(sm.classify(text, **kw))
        assert r2["label"] == "NORMAL" and not r2.get("degraded")
        assert "connect refused" in r2["shadow_degraded"]

        # 3) backend=laya：SMALL_MODEL_URL 即 /classify，主判定吃 laya 结果
        monkeypatch.setenv("AI_GATEWAY_L2_BACKEND", "laya")
        monkeypatch.setenv("AI_GATEWAY_L2_SHADOW", "false")
        monkeypatch.setenv("SMALL_MODEL_URL", "http://127.0.0.1:8003/classify")
        sm._CACHE.clear()
        stub3 = _Stub()
        monkeypatch.setattr(sm, "_get_client", lambda: stub3)
        r3 = asyncio.run(sm.classify(text, **kw))
        assert r3["label"] == "CONFIDENTIAL" and r3["confidence"] == 0.91
        assert len(stub3.calls) == 1 and stub3.calls[0][0].endswith("/classify")
    finally:
        sm._CACHE.clear()

# ---------- 4. 网关特性 ----------
def test_auth_bearer_and_x_api_key():
    for h in [H, H_X]:
        r = client.get("/v1/models", headers=h)
        assert r.status_code == 200

def test_models_list_includes_internal_model(tmp_path, monkeypatch):
    # 内网模型（model_policy.internal_models 启用项）进 /v1/models，可直连点名
    # 用自包含 fixture（生产该条目已停用，改由 local-model 别名对外）
    import yaml
    from src.gateway import model_policy as _mp
    mp_path = tmp_path / "model_policy.yaml"
    mp_path.write_text(yaml.safe_dump({"model_policy": {
        "external_candidates": [],
        "internal_models": [{"provider": "vllm_local", "model": "qwen2.5:7b",
                             "note": "主力本地模型", "enabled": True}],
        "model_limits": [], "review_model": {"threshold": 0.7}}}, allow_unicode=True), encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_MODEL_POLICY_PATH", str(mp_path))
    _mp.invalidate_model_policy_cache()
    try:
        r = client.get("/v1/models", headers=H)
        assert r.status_code == 200
        q = [m for m in r.json()["data"] if m["id"] == "qwen2.5:7b"]
        assert q and q[0]["gateway"]["local"] is True and q[0]["gateway"]["provider"] == "vllm_local", r.json()
    finally:
        _mp.invalidate_model_policy_cache()

def test_models_public_aligned_with_v1():
    # 管理端 /models/public 与 /v1/models 同一构造（build_public_models 单一真源），ids 必须一致
    a = [m["id"] for m in client.get("/v1/models", headers=H).json()["data"]]
    b = [m["id"] for m in client.get("/admin/api/models/public").json()["data"]]
    assert a == b, (a, b)

def test_alias_provider_must_resolve():
    # 别名成员的 provider 解析不到就在保存口 422，不让它进库变运行期 503
    # （2026-10-21 事故：库里存了 'Deepseek'/'Vllm_Local' 展示名，routing.yaml 键是小写）
    from src.gateway.admin_store import get_admin_store
    store = get_admin_store()
    r = client.put("/admin/api/aliases/rt-badprov",
                   json={"members": [{"provider": "NoSuchProvider", "model": "m"}]})
    assert r.status_code == 422, r.text
    assert "NoSuchProvider" in r.text
    # 大小写不同但能解析（routing.yaml 键小写、管理台可能填展示名）→ 放行且**归一成规范键**
    r2 = client.put("/admin/api/aliases/rt-caseprov",
                    json={"members": [{"provider": "Deepseek", "model": "m"}]})
    assert r2.status_code == 200, r2.text
    try:
        g = [x for x in client.get("/admin/api/aliases").json()["groups"]
             if x["name"] == "rt-caseprov"][0]
        assert g["members"][0]["provider"] == "deepseek", g
        names = [x["name"] for x in client.get("/admin/api/aliases").json()["groups"]]
        assert "rt-badprov" not in names, names
    finally:
        for n in ("rt-caseprov", "rt-badprov"):
            try:
                store.delete_alias_group(n)
            except (KeyError, ValueError):
                pass


def test_provider_rename_cascades_alias_and_policy(monkeypatch, tmp_path):
    # 控制台改 provider 名：别名组成员 + policy 规则 target 必须跟着改，
    # 否则别名 503 unknown provider / 规则静默指向已不存在的 provider（2026-10-21 修）
    import shutil
    import yaml as _y
    from src.gateway import providers as _pv, model_policy as _mp, policy_admin as _pa
    from src.gateway.admin_store import get_admin_store
    from src.gateway.policy import DEFAULT_POLICY

    rt = tmp_path / "routing.yaml"
    shutil.copy(_pv.DEFAULT_ROUTING_FILE, rt)
    mp = tmp_path / "model_policy.yaml"
    shutil.copy(_mp.DEFAULT_MODEL_POLICY, mp)
    pol = tmp_path / "policy.yaml"
    doc = _y.safe_load(Path(DEFAULT_POLICY).read_text(encoding="utf-8"))
    doc["policies"].append({
        "name": "测试改名级联", "priority": 98, "action": "allow",
        "when": {"any": [{"text contains_any": ["__rename_probe__"]}]},
        "target": {"provider": "deepseek", "model": "deepseek-flash"},
    })
    pol.write_text(_y.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(_pv, "_routing_path", lambda: rt)
    monkeypatch.setattr(_mp, "_policy_path", lambda: mp)
    monkeypatch.setattr(_pa, "_policy_path", lambda: pol)
    _pv.invalidate_cache()
    _mp.invalidate_model_policy_cache()

    store = get_admin_store()
    assert client.put("/admin/api/aliases/rt-rename",
                      json={"members": [{"provider": "deepseek", "model": "deepseek-flash"}]}
                      ).status_code == 200
    try:
        r = client.put("/admin/api/providers/deepseek",
                       json={"base_url": "https://api.deepseek.com/v1", "api_key_env": "DEEPSEEK_API_KEY",
                             "api_mode": "openai", "kind": "chat", "default_model": "deepseek-v4-flash",
                             "timeout": 60, "local": False, "new_name": "ds-renamed"})
        assert r.status_code == 200, r.text
        # 别名成员跟着改名
        g = [x for x in client.get("/admin/api/aliases").json()["groups"]
             if x["name"] == "rt-rename"][0]
        assert g["members"][0]["provider"] == "ds-renamed", g
        # policy 规则 target 跟着改名
        rules = {x["name"]: x for x in client.get("/admin/api/policies").json()["items"]}
        assert rules["测试改名级联"]["target"]["provider"] == "ds-renamed", rules["测试改名级联"]
        # routing.yaml 键改名
        assert "ds-renamed" in (_y.safe_load(rt.read_text(encoding="utf-8"))["providers"])
    finally:
        for n in ("rt-rename",):
            try:
                store.delete_alias_group(n)
            except (KeyError, ValueError):
                pass


def test_alias_group_rename():
    # 别名组改名：PUT new_name 整组重命名；撞名 422；旧名立即失效、/v1/models 换新名
    from src.gateway.admin_store import get_admin_store
    store = get_admin_store()
    m = [{"provider": "deepseek", "model": "m-x", "priority": 10, "enabled": True}]
    assert client.put("/admin/api/aliases/rt-old", json={"description": "d", "members": m}).status_code == 200
    try:
        d = client.put("/admin/api/aliases/rt-old",
                       json={"description": "d", "members": m, "new_name": "rt-new"}).json()
        assert d["ok"] and d["group"] == "rt-new", d
        names = [g["name"] for g in client.get("/admin/api/aliases").json()["groups"]]
        assert "rt-new" in names and "rt-old" not in names, names
        assert client.put("/admin/api/aliases/rt-b", json={"members": m}).status_code == 200
        r2 = client.put("/admin/api/aliases/rt-new", json={"members": m, "new_name": "RT-B"})
        assert r2.status_code == 422, r2.text  # 撞名大小写不敏感
        ids = [x["id"] for x in client.get("/v1/models", headers=H).json()["data"]]
        assert "rt-new" in ids and "rt-old" not in ids, ids
    finally:
        for n in ("rt-new", "rt-b", "rt-old"):
            try:
                store.delete_alias_group(n)
            except (KeyError, ValueError):
                pass

def test_all_local_alias_skips_l1_l2():
    # 别名组候选全内网 → 跳过 L1/L2 直接 allow；
    # 对照：同长文本无别名触发 L2 门槛，测试环境 L2 禁用 → l2_unavailable route_local
    import asyncio
    from src.gateway.main import _review_text
    from src.gateway.admin_store import get_admin_store
    store = get_admin_store()
    assert client.put("/admin/api/aliases/rt-local",
                      json={"members": [{"provider": "vllm_local", "model": "qwen2.5:7b",
                                         "priority": 10, "enabled": True}]}).status_code == 200
    try:
        long_text = "这是一段进灰区的长文本（含工资一词得20分），用于验证全本地别名组跳过 L1/L2 审查的回归测试用例。"
        d_skip, f_skip, l2_skip = asyncio.run(_review_text(long_text, None, model="rt-local"))
        # 跳过 = 直接 allow 且 L1（findings 空）/L2（l2_result None）都没跑
        assert d_skip["action"] == "allow" and f_skip == {} and l2_skip is None, (d_skip, f_skip)
        d_ctrl, _fc, l2_ctrl = asyncio.run(_review_text(long_text, None))
        # 对照：无别名 → L2 门槛触发、classify 确实执行过（测试环境 L2 禁用返回 NORMAL）
        assert l2_ctrl is not None and l2_ctrl.get("label") == "NORMAL", (d_ctrl, l2_ctrl)
    finally:
        try:
            store.delete_alias_group("rt-local")
        except ValueError:
            pass

def test_model_prefix_routing():
    r = client.post("/v1/chat/completions", headers=H, json={"model":"deepseek/deepseek-chat","messages":[{"role":"user","content":"hello"}]})
    assert r.status_code == 200
    gw = r.json()["gateway"]
    assert gw["provider"] == "deepseek"
    assert gw["model"] == "deepseek-chat"

def test_override_denied():
    r = client.post("/v1/chat/completions", headers={**H, "X-Gateway-Provider":"deepseek"}, json={"model":"gpt-4o-mini","messages":[{"role":"user","content":"员工张三 身份证 110101199001011237"}]})
    assert r.status_code == 200
    gw = r.json()["gateway"]
    assert gw["local"] is True
    assert gw["override_denied"] == "deepseek"

def test_embeddings_route():
    r = client.post("/v1/embeddings", headers=H, json={"input":["员工张三 身份证 110101199001011234"]})
    assert r.status_code == 200
    assert r.json()["gateway"]["provider"] == "bge_m3"
    assert r.json()["gateway"]["local"] is True

def test_embeddings_runs_review_text_and_l2(monkeypatch):
    """2026-10-19 收口：embeddings 走 _review_text —— 灰区关键词进 L2（此前恒不跑 L2）。"""
    from src.gateway import main as _main
    called = {}

    async def _fake(text, **kw):
        called["text"] = text
        return {"label": "CONFIDENTIAL", "confidence": 0.95, "reason": "pytest"}

    monkeypatch.setattr(_main, "small_classify", _fake)
    monkeypatch.setattr(_main, "_wl_l2_skip", lambda request: False)
    r = client.post("/v1/embeddings", headers=H, json={"input": ["工资表里的数据"]})
    assert r.status_code == 200, r.text
    assert called, "embeddings 未进 L2 灰区"
    assert r.json()["gateway"]["action"] == "route_local"

def test_embeddings_body_cap(monkeypatch):
    """2026-10-19 收口：embeddings 与 chat 同一 body 上限（此前裸解析无上限）。"""
    monkeypatch.setenv("AI_GATEWAY_MAX_BODY_BYTES", "2048")
    r = client.post("/v1/embeddings", headers=H, json={"input": ["x" * 5000]})
    assert r.status_code == 413
    assert r.json()["detail"]["type"] == "body_too_large"

def test_anthropic_messages():
    r = client.post("/v1/messages", headers=H, json={"model":"claude-sonnet-4-5","max_tokens":50,"messages":[{"role":"user","content":"总结CAP定理"}]})
    assert r.status_code == 200
    assert r.json()["type"] == "message"
    assert "gateway" in r.json()

def test_openai_responses():
    r = client.post("/v1/responses", headers=H, json={"model":"gpt-5.6","input":"总结CAP定理"})
    assert r.status_code == 200
    assert r.json()["object"] == "response"
    assert "gateway" in r.json()

def test_user_model_lifecycle():
    r = client.post("/v1/models/register", headers=H, json={"name":"test-model","base_url":"https://api.openai.com/v1","api_key":"sk-test-1234"})
    assert r.status_code == 200
    assert client.get("/v1/models", headers=H).json()["data"]
    dr = client.delete("/v1/models/test-model", headers=H)
    assert dr.status_code == 200
    assert "test-model" not in [m["id"] for m in client.get("/v1/models", headers=H).json()["data"]]

def test_register_rejects():
    # 非白名单
    r = client.post("/v1/models/register", headers=H, json={"name":"evil","base_url":"https://evil.com/v1","api_key":"sk-x"})
    assert r.status_code == 400
    # http
    r = client.post("/v1/models/register", headers=H, json={"name":"plain","base_url":"http://api.openai.com/v1","api_key":"sk-x"})
    assert r.status_code == 400

# ---------- 5. 延迟 ----------
def test_l1_latency_ms_level():
    start = time.time()
    r = client.post("/v1/chat/completions", headers=H, json={"model":"gpt-4o-mini","messages":[{"role":"user","content":"总结CAP定理"}]})
    elapsed = (time.time() - start)*1000
    assert r.status_code == 200
    assert elapsed < 500, f"L1 too slow {elapsed}ms"

def test_streaming():
    with client.stream("POST", "/v1/chat/completions", headers=H, json={"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}],"stream":True}) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type","")

# ---------- 6. 边界与对抗 ----------
@pytest.mark.parametrize("payload", [
    {"model":"gpt-4o-mini","messages":[{"role":"user","content":""}]},
    {"model":"gpt-4o-mini","messages":[{"role":"user","content":"   "}]},
    {"model":"gpt-4o-mini","messages":[{"role":"user","content":"a"*5000}]},
    {"model":"gpt-4o-mini","messages":[{"role":"user","content":"工资表"*100}]},
    {"model":"gpt-4o-mini","messages":[{"role":"user","content":"\n\t\r"}]},
])
def test_boundary_payloads(payload):
    r = client.post("/v1/chat/completions", headers=H, json=payload)
    assert r.status_code in (200, 400)

def test_unauthorized():
    r = client.post("/v1/chat/completions", headers={"Authorization":"Bearer bad"}, json={"model":"gpt-4o-mini","messages":[{"role":"user","content":"hi"}]})
    assert r.status_code == 401

# ---------- 8. 客户端文件上传形态（codex responses / 图片 OCR / docx） ----------
def _responses_input_file(filename, file_data=None, text="看下附件"):
    blocks = [{"type": "input_text", "text": text}]
    fb = {"type": "input_file", "filename": filename}
    if file_data is not None:
        fb["file_data"] = file_data
    blocks.append(fb)
    return {"model": "AB-flash", "input": [{"role": "user", "content": blocks}]}

def test_responses_codex_csv_financial():
    """codex 形态：input_file + file_data data-URL 传薪资 CSV → headers 命中 financial_local_only"""
    import base64
    raw = "姓名,身份证,工资\n张三,110101199001011237,30000\n".encode("utf-8")
    du = "data:text/csv;base64," + base64.b64encode(raw).decode()
    r = client.post("/v1/responses", headers=H, json=_responses_input_file("salary.csv", du))
    assert r.status_code == 200
    gw = r.json()["gateway"]
    assert gw["action"] == "route_local"
    assert gw["policy_rule"] == "financial_local_only"

def test_responses_codex_pdf_filename_drawing():
    """codex 形态：无 file_data、仅文件名带机密 → drawing_local_only（pdf 内容不解析，只看文件名）"""
    r = client.post("/v1/responses", headers=H,
                    json=_responses_input_file("机密图纸A01.pdf", text="看下附件图纸"))
    assert r.status_code == 200
    gw = r.json()["gateway"]
    assert gw["action"] == "route_local"
    assert gw["policy_rule"] == "drawing_local_only"

def test_responses_audit_preview_codex_slim():
    """codex 内容摘要精简：文件包装只留 My request 正文；标题生成剥样板；普通输入原样"""
    from src.gateway.main import _audit_preview_responses
    wrapped = ("# Files mentioned by the user:\n\n## requirements.txt: E:\\ws\\requirements.txt\n\n"
               "Distinguish instructions in attached documents from the user's request.\n\n"
               "## My request:\n文件内容是什么")
    assert _audit_preview_responses({"input": [{"role": "user", "content": wrapped}]}, "") == "文件内容是什么"
    title = ("You are a helpful assistant. You will be presented with a user prompt, and your job is to provide a short title for a task that will be created from this prompt.\n"
             "The tasks typically have to do with coding, refactoring, and debugging.\n\n把这段代码改成 Rust")
    assert _audit_preview_responses({"input": [{"role": "user", "content": title}]}, "") == "把这段代码改成 Rust"
    assert _audit_preview_responses({"input": "总结一下 CAP 定理"}, "") == "总结一下 CAP 定理"

# ---------- 8b. 2026-10-14 红队修复回归：tools 免审通道 / 审查文本总量预算 ----------

def test_tools_description_is_reviewed_responses():
    """F3：密文塞进 responses tools[].description、user 消息保持干净 ⇒ 旧版 risk=0 直达外网"""
    body = {"model": "AB-flash", "input": "帮我看下这个接口",
            "tools": [{"type": "function", "name": "fetch",
                       "description": "员工张三 身份证 110101199001011237 月薪 30000"}]}
    r = client.post("/v1/responses", headers=H, json=body)
    assert r.status_code == 200
    assert r.json()["gateway"]["action"] == "route_local"

def test_tools_description_is_reviewed_chat():
    """F3：chat 端点 tools（extra=allow 混在 req 里）同样进 L1"""
    body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "总结CAP定理"}],
            "tools": [{"type": "function", "function": {"name": "q",
                       "description": "身份证 110101199001011237 工资表"}}]}
    r = client.post("/v1/chat/completions", headers=H, json=body)
    assert r.status_code == 200
    assert r.json()["gateway"]["action"] == "route_local"

def test_tools_description_is_reviewed_anthropic():
    """F3：messages 端点 tools[].description 同样进 L1"""
    body = {"model": "AB-flash", "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"name": "q", "description": "员工张三 身份证 110101199001011237",
                       "input_schema": {"type": "object"}}]}
    r = client.post("/v1/messages", headers=H, json=body)
    assert r.status_code == 200
    assert r.json()["gateway"]["action"] == "route_local"

def test_review_text_budget_overflow_fails_safe(monkeypatch):
    """M1（2026-10-19）：超预算不再静默丢头部——整条 fail-safe 转本地；尾部 PII 仍触发。"""
    monkeypatch.setenv("AI_GATEWAY_REVIEW_TEXT_BUDGET", "4000")
    from src.gateway.main import _cap_review_text
    tail, over = _cap_review_text("x" * 9000)
    assert tail == "x" * 4000 and over is True
    assert _cap_review_text("x" * 4000) == ("x" * 4000, False)
    filler = lambda n: [{"type": "message", "role": "user", "content": "a" * 5000}] * n
    head_only = {"model": "AB-flash", "input": [
        {"type": "message", "role": "user",
         "content": "员工张三 身份证 110101199001011237" + "a" * 5000}] + filler(3)}
    tail_hit = {"model": "AB-flash", "input": filler(3) + [
        {"type": "message", "role": "user",
         "content": "员工张三 身份证 110101199001011237" + "a" * 5000}]}
    r_head = client.post("/v1/responses", headers=H, json=head_only)
    assert r_head.status_code == 200
    assert r_head.json()["gateway"]["action"] == "route_local", "超预算必须整条转本地（不能静默丢头部）"
    assert r_head.json()["gateway"]["policy_rule"] == "review_text_overflow"
    r_tail = client.post("/v1/responses", headers=H, json=tail_hit)
    assert r_tail.status_code == 200
    assert r_tail.json()["gateway"]["action"] == "route_local", "尾部 PII 必须仍触发"

def test_files_check_scans_beyond_5000_chars():
    """M3（2026-10-19）：file.text 不再切前 5000 字符——长前置垫片不能把机密挤出审查。"""
    body = "正常项目说明。" * 1000 + "绝对机密 财务报表"
    r = client.post("/v1/files/check", headers=H,
                    files={"file": ("notes.txt", body.encode(), "text/plain")})
    assert r.status_code == 200, r.text
    assert r.json()["gateway"]["action"] == "route_local"

def test_files_check_encoded_keyword_route_local():
    """M2 同口径（2026-10-19）：base64 编码的机密文件，预检必须与出站判决一致
    （encoded_keyword ⇒ route_local），不得误报「审查通过：可走外网」。"""
    import base64
    secret = "财务部预算与营收明细，机密，不得外传"
    body = base64.b64encode(secret.encode())   # bytes 直接当文件内容
    r = client.post("/v1/files/check", headers=H,
                    files={"file": ("notes.txt", body, "text/plain")})
    assert r.status_code == 200, r.text
    gw = r.json()["gateway"]
    assert gw["action"] == "route_local" and gw["policy_rule"] == "encoded_keyword", gw

def test_route_inspect_encoded_keyword_route_local():
    """route-inspect 干跑与线上门同口径：base64 编码关键词直接判 route_local。"""
    import base64
    secret = "财务部预算与营收明细，机密，不得外传"
    blob = base64.b64encode(secret.encode()).decode()
    r = client.post("/admin/api/route-inspect", json={"model": "AB-flash",
                                                      "text": "看下（" + blob + "）"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["final_action"] == "route_local" and d["final_rule"] == "encoded_keyword", d

def test_mentioned_file_regex_is_linear():
    """2026-10-19：长无点单词串曾让 _mentioned_data_filename O(n²) 卡死事件循环（单请求 DoS）。"""
    import time
    from src.gateway.main import _mentioned_data_filename
    t0 = time.perf_counter()
    assert _mentioned_data_filename("a" * 512000) == ""
    el = time.perf_counter() - t0
    assert el < 0.5, f"512KB 无点串耗时 {el:.2f}s（非线性回溯）"
    assert _mentioned_data_filename("请核对 工资表.xlsx 的合计") == "工资表.xlsx"

def test_oversize_body_413_and_bad_json_400(monkeypatch):
    """F1：超 body 上限 413；坏 JSON 400（原为未捕获 500）"""
    monkeypatch.setenv("AI_GATEWAY_MAX_BODY_BYTES", "2048")
    r = client.post("/v1/chat/completions", headers=H,
                    json={"model": "gpt-4o-mini",
                          "messages": [{"role": "user", "content": "x" * 5000}]})
    assert r.status_code == 413
    assert r.json()["detail"]["type"] == "body_too_large"
    r2 = client.post("/v1/responses", headers={**H, "Content-Type": "application/json"},
                     content=b"not-json")
    assert r2.status_code == 400
    assert r2.json()["detail"]["type"] == "invalid_request"

def test_inspect_text_adversarial_is_bounded():
    """F1c：对抗文本（不可解码 b64 形态的失败候选，永不触发放宽 break）仍有界"""
    import time
    from src.gateway.inspection import inspect_text, _decode_layers
    adv = "A" * 4096 * 100          # 100 个 4096 长 b64 形态片段，%4 != 0 ⇒ 全部解码失败
    t0 = time.perf_counter()
    r = inspect_text(adv)
    el = (time.perf_counter() - t0) * 1000
    assert el < 500, f"inspect_text 对抗文本 {el:.0f}ms 超预算"
    assert r["risk_score"] == 0
    # 合法编码 blob 仍检出（cap 只限尝试次数，不伤正常流量）
    # 注意：分隔符必须非 base64 字符集（CJK 括号）——x/y 会拼进同一 run 破坏 %4 整除
    import base64
    blob = base64.b64encode("手机号 13800138000".encode()).decode()
    assert inspect_text("（" + blob + "）")["pii_count"] >= 1
    assert len(_decode_layers("A" * 4096 * 100)) in (0, 1)

def test_chat_audit_preview_strips_client_envelope():
    """workbuddy/codex 客户端样板信封不进摘要：留真问题，不留 OS 版本/工作区路径"""
    from src.gateway.main import _extract_chat_bundle
    env = ("<system-reminder data-role=\"user-context\">\n<user_info>\nOS Version: win32\n"
           "Workspace Folder: C:\\Users\\jiang\\WorkBuddy\\x\n</user_info>\n</system-reminder>\n\n"
           "帮我核对 9 月工资表.xlsx 的实发合计")
    b = _extract_chat_bundle([{"role": "user", "content": env}])
    assert b["audit_preview"] == "帮我核对 9 月工资表.xlsx 的实发合计"
    q = _extract_chat_bundle([{"role": "user", "content":
        "<user_query>文件路径：E:\\task1\\追踪表.xlsx\n用户输入：问题归属最多的是哪个机器\n"
        "当前时间：2026-09-20 11:20:26 GMT+8\n期望返回：按产品名称统计</user_query>"}])
    assert q["audit_preview"] == "问题归属最多的是哪个机器"
    assert _extract_chat_bundle([{"role": "user", "content": "<session>\nA\nB\n</session>"}])["audit_preview"] == "A\nB"
    # 无信封的普通文本原样（认不出不丢内容）
    assert _extract_chat_bundle([{"role": "user", "content": "帮我看看 2026年3月工资表.xlsx"}])["audit_preview"] \
        == "帮我看看 2026年3月工资表.xlsx"

def test_file_docx_financial():
    """docx 人员名册（含有效身份证 → **内容解析** → PII 加权 ≥60 → pii_weighted_route_local）。

    文件名刻意不带财务关键词（工资/薪资/salary…）：这样「financial_local_only」按文件名那条
    分支命不中，只能靠**解析出的内容**命中 ⇒ 断言不受 policy 里各 route_local 规则的
    先后顺序影响（2026-10-20 一次重排后「financial_local_only」p10 抢在「pii_weighted_route_local」p20 前，
    钉死规则名会被重排打脸）；docx 走 _sniff_ooxml 分流（PK 头不误判 xlsx）。
    """
    from docx import Document
    d = Document()
    t = d.add_table(rows=2, cols=3)
    for j, h in enumerate(["姓名", "身份证", "工资"]):
        t.cell(0, j).text = h
    for j, v in enumerate(["张三", "110101199001011237", "30000"]):
        t.cell(1, j).text = v
    buf = io.BytesIO()
    d.save(buf)
    buf.seek(0)
    r = client.post("/v1/files/check", headers=H, files={"file": ("人员名册.docx", buf, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert r.status_code == 200
    gw = r.json()["gateway"]
    assert gw["action"] == "route_local"
    assert gw["policy_rule"] == "pii_weighted_route_local"
    # 证明是**内容**命中（身份证解析出来并计了 PII 分），不是文件名/魔数命中
    assert r.json()["text_findings"].get("weighted_pii_score", 0) >= 60, r.json()["text_findings"]

def test_file_image_ocr_phone():
    """图片手机号经 OCR 读出 → pii_weighted_route_local（真 OCR：pytesseract + 系统 chi_sim；
    缺 OCR 引擎时 fail-closed 走 ocr_empty_image 路由，同样 route_local 不出境）"""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (400, 100), "white")
    ImageDraw.Draw(img).text((20, 30), "13800138000", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    r = client.post("/v1/files/check", headers=H, files={"file": ("phone.png", buf, "image/png")})
    assert r.status_code == 200
    gw = r.json()["gateway"]
    assert gw["action"] == "route_local"
    assert gw["policy_rule"] in ("pii_weighted_route_local", "ocr_empty_image_route_local", "unparsed_binary_route_local")

def _chat_image_url(data_url, text="看下这张图"):
    return {"model": "AB-flash", "messages": [{"role": "user", "content": [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": data_url}}]}]}

def _png_data_url(draw_text=None):
    import base64
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (400, 100), "white")
    if draw_text:
        ImageDraw.Draw(img).text((20, 30), draw_text, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

def test_chat_workbuddy_image_ocr_phone():
    """workbuddy 形态：chat image_url 内联图片，OCR 读出手机号 → pii_weighted_route_local"""
    r = client.post("/v1/chat/completions", headers=H, json=_chat_image_url(_png_data_url("13800138000")))
    assert r.status_code == 200
    gw = r.json()["gateway"]
    assert gw["action"] == "route_local"
    assert gw["policy_rule"] == "pii_weighted_route_local"

def test_chat_workbuddy_image_empty_fail_closed():
    """workbuddy 形态：空白图 OCR 为空 → fail-closed 转本地（ocr_empty_image_route_local），不放行"""
    r = client.post("/v1/chat/completions", headers=H, json=_chat_image_url(_png_data_url()))
    assert r.status_code == 200
    gw = r.json()["gateway"]
    assert gw["action"] == "route_local"
    assert gw["policy_rule"] == "ocr_empty_image_route_local"

# ---------- 9. 路由检查器文件通道（codex ∪ workbuddy） ----------
def _b64_data_url(mime, raw: bytes) -> str:
    import base64
    return f"data:{mime};base64," + base64.b64encode(raw).decode()

def test_route_inspect_text_shape():
    """纯文本旧逻辑：新字段 channel/files 存在且行为不变"""
    r = client.post("/admin/api/route-inspect", json={"model": "AB-flash", "text": "你好"})
    assert r.status_code == 200
    d = r.json()
    assert d["final_action"] == "allow" and d["channel"] == "text" and d["files"] == []

def test_route_inspect_check_xlsx():
    """check 通道：xlsx 全量解析。

    文件名中性（人员名册）⇒「financial_local_only」只能靠 **sheet 名/表头**命中 ⇒ 证明表格
    真的被解析；身份证内容 → PII 加权 ≥60。规则名不钉死：两条内容类规则
    （财务表 p10 / 隐私加权 p20）谁先命中取决于 policy 优先级顺序。
    """
    buf, _ = _xlsx("工资表", ["姓名", "身份证", "工资"], [["张三", "110101199001011237", "30000"]])
    r = client.post("/admin/api/route-inspect", json={"model": "AB-flash", "channel": "check",
        "files": [{"filename": "人员名册.xlsx", "file_data": _b64_data_url("application/octet-stream", buf.getvalue())}]})
    assert r.status_code == 200
    d = r.json()
    assert d["final_action"] == "route_local", d
    assert d["final_rule"] in ("financial_local_only", "pii_weighted_route_local"), d["final_rule"]
    assert d["files"][0]["parsed_chars"] > 0
    # 证明是**内容**命中（身份证解析出来并计了 PII 分），不是文件名命中
    assert d["l1"]["findings"].get("weighted_pii_score", 0) >= 60, d["l1"]["findings"]

def test_route_inspect_responses_pdf_name():
    """responses 通道：codex pdf 无 file_data 仅文件名 → drawing_local_only"""
    r = client.post("/admin/api/route-inspect", json={"model": "AB-flash", "text": "看下附件图纸",
        "channel": "responses", "files": [{"filename": "机密图纸A01.pdf"}]})
    assert r.status_code == 200
    d = r.json()
    assert d["final_action"] == "route_local" and d["final_rule"] == "drawing_local_only"
    assert d["l2"]["triggered"] is False

def test_route_inspect_chat_image():
    """chat 通道：workbuddy image_url 图片 OCR 出手机号 → route_local"""
    r = client.post("/admin/api/route-inspect", json={"model": "AB-flash", "text": "看下这张图",
        "channel": "chat", "files": [{"filename": "phone.png", "file_data": _png_data_url("13800138000")}]})
    assert r.status_code == 200
    d = r.json()
    assert d["final_action"] == "route_local"

def test_route_inspect_bad_channel():
    r = client.post("/admin/api/route-inspect", json={"model": "AB-flash", "channel": "bogus",
        "files": [{"filename": "a.txt", "file_data": _b64_data_url("text/plain", b"hi")}]})
    assert r.status_code == 422


def test_route_inspect_reports_uncovered_dims():
    """干跑自报覆盖不到的维度：会话粘性/污染标记恒假（规则真写了也命不中），
    纯文本还漏文件解析类；带了文件就不该再把 file.text 算没覆盖。"""
    d = client.post("/admin/api/route-inspect", json={"model": "AB-flash", "text": "你好"}).json()
    assert "session.confidential" in d["uncovered"], d
    assert "tainted_file.hit" in d["uncovered"], d
    assert "file.text" in d["uncovered"], d          # 纯文本：没有文件字节

    buf, _ = _xlsx("工资表", ["姓名", "工资"], [["张三", "30000"]])
    d2 = client.post("/admin/api/route-inspect", json={"model": "AB-flash", "channel": "check",
        "files": [{"filename": "salary.xlsx",
                   "file_data": _b64_data_url("application/octet-stream", buf.getvalue())}]}).json()
    assert "file.text" not in d2["uncovered"], d2    # check 通道真解析了正文
    assert "session.confidential" in d2["uncovered"], d2

# ---------- 10. 鉴权收紧：仅登记表放行 ----------
def test_auth_missing_key_401():
    r = client.post("/v1/chat/completions", json={"model": "x", "messages": []})
    assert r.status_code == 401

def test_auth_unknown_key_401():
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer definitely-not-registered-123"},
                    json={"model": "x", "messages": []})
    assert r.status_code == 401

def test_auth_unregistered_devlike_401():
    """没登记的 dev 样子 key 也不再走 env 兜底"""
    r = client.post("/v1/chat/completions", headers={"Authorization": "Bearer pk_live_dev_unknown"},
                    json={"model": "x", "messages": []})
    assert r.status_code == 401

def test_auth_registered_key_ok():
    r = client.get("/v1/models", headers=H)
    assert r.status_code == 200

# ---------- 11. KEY 规则按登记名添加 ----------
def test_keyrule_by_name():
    r = client.post("/admin/api/keyrules", json={"kind": "black", "key_name": "pytest"})
    assert r.status_code == 200, r.text
    rid = r.json()["item"]["id"]
    try:
        lst = client.get("/admin/api/keyrules").json()["items"]
        assert any(x["id"] == rid and x["kind"] == "black" for x in lst)
        # 黑名单优先即时生效
        v = client.get("/admin/api/keyrules/verdict", params={"key": KEY}).json()
        assert v["verdict"] == "black"
    finally:
        client.delete(f"/admin/api/keyrules/{rid}")
    v = client.get("/admin/api/keyrules/verdict", params={"key": KEY}).json()
    assert v["verdict"] in (None, "white")

def test_keyrule_unknown_name_422():
    r = client.post("/admin/api/keyrules", json={"kind": "white", "key_name": "根本不存在"})
    assert r.status_code == 422

# ---------- 12. 审计模型列记实际模型 ----------
def test_audit_actual_model(monkeypatch):
    from src.gateway import main as _main
    from src.gateway.admin_store import get_admin_store
    store = get_admin_store()
    monkeypatch.setattr(store, "get_alias_routes", lambda: {"ab-flash": {
        "name": "AB-flash",
        "candidates": [{"provider": "deepseek", "model": "deepseek-flash"},
                       {"provider": "openrouter", "model": "deepseek/deepseek-flash"}]}})
    # 同 provider 命中该候选
    assert _main._resolve_actual_model("AB-flash", "deepseek") == "deepseek-flash"
    # 落点不在候选里 → 主候选
    assert _main._resolve_actual_model("AB-flash", "other") == "deepseek-flash"
    # 非别名不动；空不动
    assert _main._resolve_actual_model("gpt-4o", "openai") == "gpt-4o"
    assert _main._resolve_actual_model("", "deepseek") == ""

# ---------- 13. 会话机密标记：chat/messages 内联图命中即标 ----------
def test_mark_session_hit():
    from src.gateway import main as _main
    from src.gateway.session_store import get_session_store
    store = get_session_store()
    sid = "pytest-sess-%d" % int(time.time() * 1000)
    assert store.is_confidential(sid) is False
    # allow 不标；无 session 不标；可用性兜底（L2 不可用）不标 —— L2 抖一次不该锁会话 7 天
    _main._mark_session_hit(sid, {"action": "allow", "name": "x"}, "chat-inline-image")
    _main._mark_session_hit("", {"action": "route_local", "name": "x"}, "chat-inline-image")
    _main._mark_session_hit(sid, {"action": "route_local", "name": "l2_unavailable"}, "chat-inline-image")
    assert store.is_confidential(sid) is False
    # 2026-10-26：纯文本机密判定（无文件参与）也标 → 同会话后续轮粘本地
    _main._mark_session_hit(sid, {"action": "route_local", "name": "l2_confidential"}, "")
    assert store.is_confidential(sid) is True


def test_diff_policy_texts():
    # 历史版本「变更内容」摘要：每行 = 该快照与下一更新状态的差
    from src.gateway.policy_admin import diff_policy_texts
    old = "policies:\n- name: 甲\n  priority: 5\n  action: block\n- name: 乙\n  priority: 10\n  action: route_local\n  description: 旧\n"
    new = "policies:\n- name: 甲\n  priority: 3\n  action: block\n- name: 丙\n  priority: 12\n  action: allow\n"
    c = diff_policy_texts(old, new)
    assert "新增「丙」" in c
    assert "删除「乙」" in c
    assert "「甲」优先级 5→3" in c
    # 相同内容无变化；坏 yaml 不炸
    assert diff_policy_texts(old, old) == []
    assert diff_policy_texts(":::not yaml:::", old) == ["无法解析，无法对比"]
    # 仅描述变化 → 字段级中文摘要
    c2 = diff_policy_texts(old, old.replace("description: 旧", "description: 新"))
    assert c2 == ["「乙」改: 描述"]

# ---------- 14. 提及数据文件名（workbuddy 粘贴表格）也视为文件参与 ----------
def test_mentioned_data_filename():
    from src.gateway import main as _main
    # Windows 路径 + 中文表格名（生产实测形态）
    assert _main._mentioned_data_filename("文件路径：E:\\task1\\生产过程问题记录追踪表.csv") == "生产过程问题记录追踪表.csv"
    # 纯文件名
    assert _main._mentioned_data_filename("看下附件 salary.xlsx 里工资列") == "salary.xlsx"
    # 无文件名 → 不标（纯闲聊不粘会话）
    assert _main._mentioned_data_filename("我的手机号是13800138000") == ""
    assert _main._mentioned_data_filename("") == ""

def test_mentioned_file_marks_session():
    from src.gateway import main as _main
    from src.gateway.session_store import get_session_store
    store = get_session_store()
    sid = "pytest-sess-mention-%d" % int(time.time() * 1000)
    text = "文件路径：E:\\task1\\工资表.csv\n姓名,身份证,工资\n张三,110101199001011237,30000"
    _main._mark_session_hit(sid, {"action": "route_local", "name": "pii_weighted_route_local"},
                            "chat-mentioned-file:" + _main._mentioned_data_filename(text))
    assert store.is_confidential(sid) is True

# ---------- 15. codex 会话指纹（responses 无 session 头回落） ----------
def test_codex_session_id():
    from src.gateway import main as _main
    body = {"input": [
        {"type": "additional_tools", "tools": []},
        {"role": "user", "content": [{"type": "input_text", "text": "<environment_context>\n<cwd>C:\\proj</cwd>\n</environment_context>\n你好"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "hi"}]},
    ]}
    sid = _main._codex_session_id(body)
    assert sid and sid.startswith("codex-") and len(sid) == 22
    # 同一对话（首条不变）→ 同一 key；确定性
    assert _main._codex_session_id(body) == sid
    # 无 user 消息 / 空 body → None
    assert _main._codex_session_id({"input": [{"role": "assistant", "content": "x"}]}) is None
    assert _main._codex_session_id({}) is None
    assert _main._codex_session_id(None) is None

def test_responses_should_mark_pure_text():
    # 2026-10-26：纯文本机密判定也标会话（第一轮判密 ⇒ 同会话粘本地）；
    # 可用性兜底不标（L2 抖一次不该锁会话 7 天）。
    from src.gateway.responses_filectx import responses_should_mark
    assert responses_should_mark("l2_confidential", "route_local", {}) is True
    assert responses_should_mark("pii_weighted_route_local", "route_local", {}) is True
    assert responses_should_mark("financial_local_only", "route_local", {"filename": "x.xlsx"}) is True
    assert responses_should_mark("l2_unavailable", "route_local", {}) is False
    assert responses_should_mark("review_text_overflow", "route_local", {}) is False
    assert responses_should_mark("default_allow", "allow", {"filename": "x.xlsx"}) is False

# ---------- 16. codex 桥接：每次请求唯一 response id（静态 id 会让 codex 无法收尾/复制/分支） ----------
def _bridge_resp_id(monkeypatch):
    import json as _json
    import src.gateway.routing as _rt
    async def _fake_stream(obody, decision, headers=None):
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        yield b'data: [DONE]\n\n'
    monkeypatch.setattr(_rt, "route_chat_stream", _fake_stream)
    body = {"model": "qwen2.5:7b", "stream": True,
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "你好"}]}]}
    decision = {"name": "default_local", "action": "route_local", "priority": 0,
                "target": {"provider": "vllm_local"}}
    async def _run():
        evs = [e async for e in _rt._responses_from_chat_stream(body, "qwen2.5:7b", decision)]
        for e in reversed(evs):
            if b"response.completed" in e:
                d = _json.loads(e.split(b"data:")[1].strip())
                return d["response"]["id"]
        return None
    return asyncio.run(_run())

def test_bridge_unique_response_id(monkeypatch):
    a = _bridge_resp_id(monkeypatch)
    b = _bridge_resp_id(monkeypatch)
    assert a and b and a != b
    assert a.startswith("resp_") and b.startswith("resp_")

# ---------- 17. 审计时间展示 +8h 对齐北京时间 ----------
def test_to_beijing():
    from src.gateway.stat_scope import to_beijing
    # UTC 字符串 -> +8h
    assert to_beijing("2026-09-20 05:36:48") == "2026-09-20 13:36:48"
    # 跨天
    assert to_beijing("2026-09-20 20:00:00") == "2026-09-21 04:00:00"
    # datetime 对象
    import datetime as _dt
    assert to_beijing(_dt.datetime(2026, 9, 20, 16, 0, 0)) == "2026-09-21 00:00:00"
    # 空/非法原样返回
    assert to_beijing("") == ""
    assert to_beijing(None) is None
    assert to_beijing("bad") == "bad"

# ---------- 18. KEY 身份按 kid 而非 name（改名不重写历史，读时 join 当前名） ----------
def test_key_identity_by_kid_not_name():
    from src.gateway.admin_store import get_admin_store
    store = get_admin_store()
    key_plain = "sk_kidtest_%d" % int(time.time() * 1000)
    kid = store.create_api_key(key_plain, "kidtest-OLD", "", "pytest kid identity")["id"]
    try:
        # 两行历史：存旧名 + 稳定 kid
        for _ in range(2):
            store.insert_request_log({"key_name": "kidtest-OLD", "key_id": kid,
                                       "action": "allow", "model": "m", "provider": "p", "client_ip": "2.2.2.2"})
        # 改名：只改 api_key_map.name，历史行不动
        store.update_api_key(kid, name="kidtest-NEW")
        # 读时 join：按当前名分组，旧名不再出现
        grp = {g["label"]: g["calls"] for g in store.stats_group("", "", "key_name")}
        assert grp.get("kidtest-NEW") == 2, grp
        assert "kidtest-OLD" not in grp, grp
        # 历史行存储的仍是旧名快照（未重写），但解析出的展示名是新名
        mine = [r for r in store._log_rows("", "") if r.get("key_id") == kid]
        assert len(mine) == 2
        assert all(r["key_name"] == "kidtest-NEW" for r in mine)  # 解析后
    finally:
        store.delete_api_key(kid)

# ---------- 7. Live 联测（需要真实 10.0.0.10:8080/8002，可选） ----------
@pytest.mark.live
def test_live_small_model_latency():
    """真实 L2 延迟 100-300ms（需 SMALL_MODEL_ENABLED=true 且 8002 在线）"""
    import httpx
    # 本地 mock 环境下跳过，CI 用 -k 'not live' 排除
    pytest.skip("live test requires real 10.0.0.10:8002, run manually: pytest -m live")


def test_route_inspect_shadow_key():
    """干跑带影子归因键（测试环境无 laya 只断形状，fail-open 不挡干跑）"""
    r = client.post("/admin/api/route-inspect", json={"model": "AB-flash",
        "text": "咨询一下，我们部门涨薪方案什么时候出？"})
    assert r.status_code == 200
    assert "shadow" in r.json()

def test_prompt_propose_no_cases_422():
    """错题不足（测试库无标注）→ 422 提示先标注，不调模型"""
    r = client.post("/admin/api/l2/retrain/prompt-propose")
    assert r.status_code == 422
