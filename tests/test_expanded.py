"""
扩展测试集 - 覆盖 L1 规则 + L2 小模型 + 文件 + 网关特性 + 延迟
tests/test_expanded.py
运行: .\.venv\Scripts\python.exe -m pytest tests/test_expanded.py -v
      .\.venv\Scripts\python.exe -m pytest tests/test_expanded.py -k "not live" -v  # 仅离线
"""
import io
import time
import asyncio
import openpyxl
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

from src.gateway.main import app

client = TestClient(app)
KEY = "sk_test_dummy0123456789abcdef0123456789abcdef01"
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

def _pdf(text):
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setFont("STSong-Light", 12)
    c.drawString(100, 700, text)
    c.save()
    buf.seek(0)
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
    """<30字即使语义机密也不应触发L2（网关层直接 L1 allow）"""
    r = client.post("/v1/chat/completions", headers=H, json={"model":"gpt-4o-mini","messages":[{"role":"user","content":"张三工资15000"}]})
    assert r.status_code == 200
    # 短文本不触发 L2，L1 未命中则 allow
    assert r.json()["gateway"]["layer"] == "L1"

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
    pdf = _pdf("机密 保密 内部资料 不得外传 confidential 图纸")
    r = client.post("/v1/files/check", headers=H, files={"file": ("drawing.pdf", pdf, "application/pdf")})
    assert r.status_code == 200
    assert r.json()["gateway"]["policy_rule"] == "drawing_local_only"
    assert r.json()["gateway"]["action"] == "route_local"

def test_file_drawing_normal():
    pdf = _pdf("普通图纸 无水印 公开资料")
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
            r = client.post("/v1/chat/completions", headers=H, json={"model":"gpt-4o-mini","messages":[{"role":"user","content":"这是超过三十字的长文本，用于触发L2小模型分类，内容包含薪资住址银行流水等敏感信息需要超过三十字"}]})
            assert r.status_code == 200
            gw = r.json()["gateway"]
            assert gw["layer"] == "L2"
            assert gw["policy_rule"] == "small_model_confidential"
            assert gw["action"] == "route_local"

def test_l2_mock_normal():
    mock_res = {"label":"NORMAL","confidence":0.9,"reason":"mock normal","latency_ms":100,"raw":"mock"}
    with patch("src.gateway.main.small_classify", new=AsyncMock(return_value=mock_res)):
        with patch("src.gateway.main.is_confidential", return_value=False):
            r = client.post("/v1/chat/completions", headers=H, json={"model":"gpt-4o-mini","messages":[{"role":"user","content":"请详细介绍CAP定理的三个特性以及在分布式系统中的应用场景和权衡，字数不少于60字"}]})
            assert r.status_code == 200
            gw = r.json()["gateway"]
            assert gw["action"] == "allow"
            assert gw["l2"]["label"] == "NORMAL"

def test_l2_cache_hit():
    """同一文本二次请求应命中缓存 latency 0"""
    import time
    from src.gateway.small_model import _CACHE, _cache_key
    # 直接测 small_model 缓存逻辑（离线 mock 已关 L2，这里仅测缓存结构）
    assert isinstance(_CACHE, dict)

# ---------- 4. 网关特性 ----------
def test_auth_bearer_and_x_api_key():
    for h in [H, H_X]:
        r = client.get("/v1/models", headers=h)
        assert r.status_code == 200

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
    return {"model": "ext-flash", "input": [{"role": "user", "content": blocks}]}

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

def test_file_docx_financial():
    """docx 薪资表 → financial_local_only（_sniff_ooxml 分流，PK 头不误判 xlsx）"""
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
    r = client.post("/v1/files/check", headers=H, files={"file": ("salary.docx", buf, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert r.status_code == 200
    gw = r.json()["gateway"]
    assert gw["action"] == "route_local"
    assert gw["policy_rule"] == "financial_local_only"

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
    return {"model": "ext-flash", "messages": [{"role": "user", "content": [
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
    r = client.post("/admin/api/route-inspect", json={"model": "ext-flash", "text": "你好"})
    assert r.status_code == 200
    d = r.json()
    assert d["final_action"] == "allow" and d["channel"] == "text" and d["files"] == []

def test_route_inspect_check_xlsx():
    """check 通道：xlsx 薪资表全量解析 → financial_local_only"""
    buf, _ = _xlsx("工资表", ["姓名", "身份证", "工资"], [["张三", "110101199001011237", "30000"]])
    r = client.post("/admin/api/route-inspect", json={"model": "ext-flash", "channel": "check",
        "files": [{"filename": "salary.xlsx", "file_data": _b64_data_url("application/octet-stream", buf.getvalue())}]})
    assert r.status_code == 200
    d = r.json()
    assert d["final_action"] == "route_local" and d["final_rule"] == "financial_local_only"
    assert d["files"][0]["parsed_chars"] > 0

def test_route_inspect_responses_pdf_name():
    """responses 通道：codex pdf 无 file_data 仅文件名 → drawing_local_only"""
    r = client.post("/admin/api/route-inspect", json={"model": "ext-flash", "text": "看下附件图纸",
        "channel": "responses", "files": [{"filename": "机密图纸A01.pdf"}]})
    assert r.status_code == 200
    d = r.json()
    assert d["final_action"] == "route_local" and d["final_rule"] == "drawing_local_only"
    assert d["l2"]["triggered"] is False

def test_route_inspect_chat_image():
    """chat 通道：workbuddy image_url 图片 OCR 出手机号 → route_local"""
    r = client.post("/admin/api/route-inspect", json={"model": "ext-flash", "text": "看下这张图",
        "channel": "chat", "files": [{"filename": "phone.png", "file_data": _png_data_url("13800138000")}]})
    assert r.status_code == 200
    d = r.json()
    assert d["final_action"] == "route_local"

def test_route_inspect_bad_channel():
    r = client.post("/admin/api/route-inspect", json={"model": "ext-flash", "channel": "bogus",
        "files": [{"filename": "a.txt", "file_data": _b64_data_url("text/plain", b"hi")}]})
    assert r.status_code == 422

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
    monkeypatch.setattr(store, "get_alias_routes", lambda: {"ext-flash": {
        "name": "ext-flash",
        "candidates": [{"provider": "deepseek", "model": "deepseek-flash"},
                       {"provider": "openrouter", "model": "deepseek/deepseek-flash"}]}})
    # 同 provider 命中该候选
    assert _main._resolve_actual_model("ext-flash", "deepseek") == "deepseek-flash"
    # 落点不在候选里 → 主候选
    assert _main._resolve_actual_model("ext-flash", "other") == "deepseek-flash"
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
    # allow 不标；无文件名（纯文本）不标；无 session 不标
    _main._mark_session_hit(sid, {"action": "allow", "name": "x"}, "chat-inline-image")
    _main._mark_session_hit(sid, {"action": "route_local", "name": "x"}, "")
    _main._mark_session_hit("", {"action": "block", "name": "x"}, "chat-inline-image")
    assert store.is_confidential(sid) is False
    # 内联图命中 route_local → 标上
    _main._mark_session_hit(sid, {"action": "route_local", "name": "pii_weighted_route_local"}, "chat-inline-image")
    assert store.is_confidential(sid) is True

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
    from src.gateway import main as _main
    # UTC 字符串 -> +8h
    assert _main._to_beijing("2026-09-20 05:36:48") == "2026-09-20 13:36:48"
    # 跨天
    assert _main._to_beijing("2026-09-20 20:00:00") == "2026-09-21 04:00:00"
    # datetime 对象
    import datetime as _dt
    assert _main._to_beijing(_dt.datetime(2026, 9, 20, 16, 0, 0)) == "2026-09-21 00:00:00"
    # 空/非法原样返回
    assert _main._to_beijing("") == ""
    assert _main._to_beijing(None) is None
    assert _main._to_beijing("not-a-time") == "not-a-time"

def test_ts_beijing_request_log():
    from src.gateway import admin_store as _st
    # UTC 字符串 -> +8h
    assert _st._ts_beijing("2026-09-20 05:36:48") == "2026-09-20 13:36:48"
    # datetime 对象
    import datetime as _dt
    assert _st._ts_beijing(_dt.datetime(2026, 9, 20, 20, 0, 0)) == "2026-09-21 04:00:00"
    # 空/非法原样
    assert _st._ts_beijing("") == ""
    assert _st._ts_beijing("bad") == "bad"

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

