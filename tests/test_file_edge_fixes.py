"""文件通道四处边界回归（2026-10-28 文件/多模态评测暴露）。

1. PII 数字串与相邻数字列粘连（表格行号 + 身份证）仍应计分；
2. 内联文本（codex input_file csv/txt）应解析而非 fail-closed；
3. 内联损坏 PDF（parse_failed）应 fail-closed；
4. 内联图片 OCR 文本应进入 file.text，命中"drawing_local_only"。
"""
import base64
import io

from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from src.gateway.file_inspector import inspect_inline_bytes
from src.gateway.inspection import inspect_text
from src.gateway.main import app

client = TestClient(app)
KEY = "sk_shGD8A0lZpIJbcGavN8z6GBcSl-DGIsecJqgF4r0k_o"
H = {"Authorization": f"Bearer {KEY}"}
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"


def test_id_adjacent_digit_columns_still_scored():
    r = inspect_text("1 110101199001011237 x")
    assert r["weighted_pii_score"] >= 70, r


def test_inline_textual_csv_parsed_not_fail_closed():
    r = inspect_inline_bytes(b"Product,Price\nA,100\n")
    assert r["unreadable"] is False, r
    assert "Product" in r["text"]


def test_inline_broken_pdf_fail_closed():
    r = inspect_inline_bytes(b"%PDF-1.7\nbroken garbage\n%%EOF")
    assert r["unreadable"] is True, r


def test_responses_raw_b64_file_data_fail_closed():
    """file_data 不带 data: 前缀（裸 base64）必须 fail-closed，不得静默跳过
    （对齐解码失败/未知 source 口径，回归 2026-10-29 口径不一致）"""
    r = client.post("/v1/responses", headers=H, json={
        "model": "AB-flash",
        "input": [{"role": "user", "content": [
            # 首条用户文本与文件名均须全套件唯一：codex 会话指纹=首条 user 消息哈希
            # （撞会话 ⇒ p12 抢先）；responses 会把 input_file 文件名合成进审查文本
            # （撞已标污染的文件名 ⇒ p5 抢先，test_e2e_file_precheck R1 会用 data.csv）
            {"type": "input_text", "text": "看下附件（口径回归-唯一首问）"},
            {"type": "input_file", "filename": "raw_edge.csv",
             "file_data": base64.b64encode("姓名,工资\n张三,30000".encode("utf-8")).decode()}]}]})
    assert r.status_code == 200
    gw = r.json()["gateway"]
    assert gw["action"] == "route_local", gw
    assert gw["policy_rule"] == "ocr_empty_image_route_local", gw


def _png_data_url(text):
    img = Image.new("RGB", (900, 160), "white")
    ImageDraw.Draw(img).text((20, 30), text, fill="black", font=ImageFont.truetype(FONT, 40))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_chat_inline_confidential_hits_file_rule():
    r = client.post("/v1/chat/completions", headers=H, json={
        "model": "AB-flash",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "识别这张图的标记（回归用例）"},
            {"type": "image_url", "image_url": {"url": _png_data_url("CONFIDENTIAL")}}]}]})
    assert r.status_code == 200
    gw = r.json()["gateway"]
    assert gw["action"] == "route_local"
    assert gw["policy_rule"] == "drawing_local_only", gw
