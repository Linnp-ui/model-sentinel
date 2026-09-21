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
    import io
    # 最小PDF含“机密”（必须嵌 CJK 字体，否则中文写不进文本层，抽出来是空）
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.setFont("STSong-Light", 12)
    c.drawString(100,700,"机密 设计图纸 图号 A01")
    c.save()
    buf.seek(0)
    r = client.post("/v1/files/check", headers=H, files={"file": ("drawing_A01.pdf", buf, "application/pdf")})
    assert r.status_code == 200
    assert r.json()["gateway"]["action"] == "route_local"
