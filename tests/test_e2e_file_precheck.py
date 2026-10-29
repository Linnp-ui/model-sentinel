"""端到端矩阵：所有带文件的请求形态是否都被预检（2026-10-29 口径收口后）。

逐行走真实 /v1/* 端点（TestClient），断言判定动作+规则，分类三类：
  [预检]  内容字节过网关且被解析/OCR，内容命中才转内网（文件名中性，证明靠内容）
  [FC]    fail-closed：内容不可检/不支持 ⇒ 转内网，不静默放行
  [边界]  内容字节不过网关（纯 URL 引用 / file_id 引用 / 仅文件名），无从预检——
          这是协议边界不是漏检；文件名本身仍走 policy（file.filename 关键词）

responses(codex): R1 CSV  R2 xlsx  R3 PNG(input_file)  R4 手机号图(input_image)
                  R5 损坏PDF  R6 裸b64(FC)  R7 音频(FC)  R8 仅文件名(边界)  R9 file_id(边界)
                  R10 远端图URL(FC)  R11 远端file_data(FC)
chat(workbuddy):  C1 PNG(image_url)  C2 http URL(FC)  C3 音频(FC)
messages:         M1 PNG(base64)  M2 损坏PDF(document)  M3 document url(FC)  M4 未知source(FC)
files/check:      F1 xlsx  F2 良性txt

每行首条用户文本唯一：codex/chat 会话指纹=首条 user 消息哈希，撞会话会被
前序 route_local 标机密 ⇒ p12 抢在内容规则前，断言失真。
"""
import base64
import io

import openpyxl
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw, ImageFont

from src.gateway.main import app

client = TestClient(app)
KEY = "sk_shGD8A0lZpIJbcGavN8z6GBcSl-DGIsecJqgF4r0k_o"
H = {"Authorization": f"Bearer {KEY}"}
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
MODEL = "AB-flash"
# 内容命中类规则：财务表 p10 / 隐私加权 p20，谁先取决于 policy 顺序，两者都算「内容预检生效」
CONTENT_RULES = ("financial_local_only", "pii_weighted_route_local")
# fail-closed 类规则：空图 p30 / 未解析二进制 p13
FC_RULES = ("ocr_empty_image_route_local", "unparsed_binary_route_local")


def _last_audit(text_marker):
    """取 memory 审计库中最近一条、text_preview 含 text_marker 的 entries（单测进程内共享）。"""
    from src.gateway.audit_store import get_audit_store
    store = get_audit_store()
    with store._mem_lock:
        rows = list(store._mem)
    for r in reversed(rows):
        if text_marker and text_marker in str(r.get("text_preview", "")):
            return r
    return None



def _png_data_url(text):
    img = Image.new("RGB", (900, 160), "white")
    ImageDraw.Draw(img).text((20, 30), text, fill="black", font=ImageFont.truetype(FONT, 40))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _du(mime, raw: bytes) -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def _xlsx(sheet_name, headers, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _responses(blocks):
    return {"model": MODEL, "input": [{"role": "user", "content": blocks}]}


def _gw(r):
    assert r.status_code == 200, (r.status_code, r.text[:300])
    return r.json()["gateway"]


# ---------- A. /v1/responses（codex） ----------
def test_r1_responses_csv_content_prechecked():
    """[预检] data: CSV，文件名中性 ⇒ 只能靠解析出的表头/内容命中"""
    raw = "姓名,身份证,工资\n张三,110101199001011237,30000\n".encode("utf-8")
    gw = _gw(client.post("/v1/responses", headers=H, json=_responses([
        {"type": "input_text", "text": "e2e-R1 看下表格"},
        {"type": "input_file", "filename": "data.csv", "file_data": _du("text/csv", raw)}])))
    assert gw["action"] == "route_local", gw
    assert gw["policy_rule"] in CONTENT_RULES, gw
    # 审计拆分落键（2026-10-29）：findings=文本 L1 命中构成，file_findings=文件预检结果，
    # 展开区两段同显；PII 命中不再挤掉文件视图。
    a = _last_audit("e2e-R1")
    assert a, "audit entry for R1 missing"
    f = a.get("findings")
    ff = a.get("file_findings")
    assert isinstance(f, dict) and (f.get("risk_score") or f.get("keyword_hits")), a
    assert isinstance(ff, dict) and ff.get("char_count"), a


def test_r2_responses_xlsx_content_prechecked():
    """[预检] data: xlsx，文件名中性 ⇒ sheet 名/PII 只能靠全量解析命中"""
    raw = _xlsx("工资表", ["姓名", "身份证", "工资"], [["张三", "110101199001011237", "30000"]])
    gw = _gw(client.post("/v1/responses", headers=H, json=_responses([
        {"type": "input_text", "text": "e2e-R2 看下表格"},
        {"type": "input_file", "filename": "data.xlsx",
         "file_data": _du("application/octet-stream", raw)}])))
    assert gw["action"] == "route_local", gw
    assert gw["policy_rule"] in CONTENT_RULES, gw
    # 双视图同显的回归用例（2026-10-29 前：PII 命中 ⇒ 文件视图被挤掉，展开区只剩风险分解）：
    # findings 带 L1 命中构成（身份证 PII），file_findings 带表名，两段都要落审计。
    a = _last_audit("e2e-R2")
    assert a, "audit entry for R2 missing"
    f = a.get("findings")
    ff = a.get("file_findings")
    assert isinstance(f, dict) and (f.get("risk_score") or f.get("keyword_hits") or f.get("findings")), a
    assert isinstance(ff, dict) and "工资表" in (ff.get("sheet_names") or []), a


def test_r3_responses_png_input_file_ocr():
    """[预检] data: PNG 走 input_file，OCR 读出机密词 ⇒ drawing_local_only"""
    gw = _gw(client.post("/v1/responses", headers=H, json=_responses([
        {"type": "input_text", "text": "e2e-R3 看下附件"},
        {"type": "input_file", "filename": "scan.png", "file_data": _png_data_url("CONFIDENTIAL")}])))
    assert gw["action"] == "route_local", gw
    assert gw["policy_rule"] == "drawing_local_only", gw


def test_r4_responses_input_image_ocr_pii():
    """[预检] input_image 块，OCR 读出手机号 ⇒ pii_weighted_route_local"""
    gw = _gw(client.post("/v1/responses", headers=H, json=_responses([
        {"type": "input_text", "text": "e2e-R4 识别图中号码"},
        {"type": "input_image", "image_url": _png_data_url("13800138000")}])))
    assert gw["action"] == "route_local", gw
    assert gw["policy_rule"] == "pii_weighted_route_local", gw


def test_r5_responses_broken_pdf_fail_closed():
    """[预检+FC] 损坏 PDF 解析失败 ⇒ fail-closed，不许静默放行"""
    raw = b"%PDF-1.7\nbroken garbage\n%%EOF"
    gw = _gw(client.post("/v1/responses", headers=H, json=_responses([
        {"type": "input_text", "text": "e2e-R5 看下附件"},
        {"type": "input_file", "filename": "doc.pdf", "file_data": _du("application/pdf", raw)}])))
    assert gw["action"] == "route_local", gw
    assert gw["policy_rule"] in FC_RULES, gw
    a = _last_audit("e2e-R5")
    assert a, "audit entry for R5 missing"
    ff = a.get("file_findings")
    assert isinstance(ff, dict) and (ff.get("unparsed_binary") or ff.get("parse_failed") or ff.get("ocr_empty_and_image")), a


def test_r6_responses_raw_b64_fail_closed():
    """[FC] 裸 base64（无 data: 前缀）不可检 ⇒ fail-closed（2026-10-29 口径收口）

    文件名须唯一：responses 会把 input_file 文件名合成进审查文本，撞 R1 的
    data.csv（已标文件名污染）⇒ p5 抢在 p30 前，断言失真。"""
    raw = base64.b64encode("姓名,工资\n张三,30000".encode("utf-8")).decode()
    gw = _gw(client.post("/v1/responses", headers=H, json=_responses([
        {"type": "input_text", "text": "e2e-R6 看下附件"},
        {"type": "input_file", "filename": "raw_e2e.csv", "file_data": raw}])))
    assert gw["action"] == "route_local", gw
    assert gw["policy_rule"] == "ocr_empty_image_route_local", gw


def test_r7_responses_audio_fail_closed():
    """[FC] 音频不转写，有数据即 fail-closed"""
    gw = _gw(client.post("/v1/responses", headers=H, json=_responses([
        {"type": "input_text", "text": "e2e-R7 听下语音"},
        {"type": "input_audio", "input_audio": {"data": base64.b64encode(b"RIFFxxxx").decode()}}])))
    assert gw["action"] == "route_local", gw
    assert gw["policy_rule"] == "ocr_empty_image_route_local", gw


def test_r8_responses_filename_only_no_bytes():
    """[边界] 仅文件名、无内容字节 ⇒ 无从预检；文件名仍走 policy（中性名 ⇒ 放行）"""
    gw = _gw(client.post("/v1/responses", headers=H, json=_responses([
        {"type": "input_text", "text": "e2e-R8 看下附件"},
        {"type": "input_file", "filename": "public_stats.csv"}])))
    assert gw["action"] == "allow", gw
    # 无字节不可预检 ⇒ 审计 findings 应为空（无可显示的预检结果）
    a = _last_audit("e2e-R8")
    assert a, "audit entry for R8 missing"
    assert not a.get("findings") and not a.get("file_findings"), a


def test_r9_responses_file_id_no_bytes():
    """[边界] file_id 引用上游存储文件，内容不过网关 ⇒ 无从预检（文件名中性 ⇒ 放行）"""
    gw = _gw(client.post("/v1/responses", headers=H, json=_responses([
        {"type": "input_text", "text": "e2e-R9 看下附件"},
        {"type": "input_file", "file_id": "file_e2e_r9"}])))
    assert gw["action"] == "allow", gw
    # file_id 引用不过网关 ⇒ 无内容可预检 ⇒ 审计 findings 为空
    a = _last_audit("e2e-R9")
    assert a, "audit entry for R9 missing"
    assert not a.get("findings") and not a.get("file_findings"), a


def test_r10_responses_remote_image_url_fail_closed():
    """[FC] input_image 是远端 URL，字节不过网关 ⇒ 内容不可检 ⇒ fail-closed 转内网"""
    gw = _gw(client.post("/v1/responses", headers=H, json=_responses([
        {"type": "input_text", "text": "e2e-R10 看这张图"},
        {"type": "input_image", "image_url": "https://example.com/a.png"}])))
    assert gw["action"] == "route_local", gw
    assert gw["policy_rule"] == "remote_attachment_route_local", gw
    a = _last_audit("e2e-R10")
    assert a and isinstance(a.get("file_findings"), dict) and a["file_findings"].get("remote_attachment"), a


def test_r11_responses_remote_file_data_url_fail_closed():
    """[FC] input_file.file_data 是 http(s) URL ⇒ 远端附件 fail-closed（与裸 b64 的空图口径区分）

    文件名须唯一：撞 R1 的 data.csv（已标文件名污染）⇒ p5 抢在 p14 前，断言失真。"""
    gw = _gw(client.post("/v1/responses", headers=H, json=_responses([
        {"type": "input_text", "text": "e2e-R11 看下附件"},
        {"type": "input_file", "filename": "remote_r11.csv", "file_data": "https://example.com/a.csv"}])))
    assert gw["action"] == "route_local", gw
    assert gw["policy_rule"] == "remote_attachment_route_local", gw
    a = _last_audit("e2e-R11")
    assert a and isinstance(a.get("file_findings"), dict) and a["file_findings"].get("remote_attachment"), a


# ---------- B. /v1/chat/completions（workbuddy） ----------
def test_c1_chat_image_url_ocr():
    """[预检] image_url data: 图，OCR 读出机密词 ⇒ drawing_local_only"""
    gw = _gw(client.post("/v1/chat/completions", headers=H, json={
        "model": MODEL, "messages": [{"role": "user", "content": [
            {"type": "text", "text": "e2e-C1 识别这张图"},
            {"type": "image_url", "image_url": {"url": _png_data_url("CONFIDENTIAL")}}]}]}))
    assert gw["action"] == "route_local", gw
    assert gw["policy_rule"] == "drawing_local_only", gw
    # 内联图片预检 findings 落审计展开区（is_image / char_count，独立 file_findings 键）
    a = _last_audit("e2e-C1")
    assert a and isinstance(a.get("file_findings"), dict) and a["file_findings"].get("is_image"), a


def test_c2_chat_remote_url_fail_closed():
    """[FC] image_url 是远端 URL，字节不过网关 ⇒ 内容不可检 ⇒ fail-closed 转内网（非放行）"""
    gw = _gw(client.post("/v1/chat/completions", headers=H, json={
        "model": MODEL, "messages": [{"role": "user", "content": [
            {"type": "text", "text": "e2e-C2 看这张图"},
            {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}]}))
    assert gw["action"] == "route_local", gw
    assert gw["policy_rule"] == "remote_attachment_route_local", gw
    # 远端附件 fail-closed 标记落审计展开区
    a = _last_audit("e2e-C2")
    assert a and isinstance(a.get("file_findings"), dict) and a["file_findings"].get("remote_attachment"), a


def test_c3_chat_audio_fail_closed():
    """[FC] chat 音频块有数据即 fail-closed"""
    gw = _gw(client.post("/v1/chat/completions", headers=H, json={
        "model": MODEL, "messages": [{"role": "user", "content": [
            {"type": "text", "text": "e2e-C3 听下语音"},
            {"type": "input_audio", "input_audio": {"data": base64.b64encode(b"RIFFxxxx").decode()}}]}]}))
    assert gw["action"] == "route_local", gw
    assert gw["policy_rule"] == "ocr_empty_image_route_local", gw


# ---------- C. /v1/messages（anthropic） ----------
def _messages(content_blocks):
    return {"model": MODEL, "max_tokens": 16, "messages": [{"role": "user", "content": content_blocks}]}


def test_m1_messages_image_b64_ocr():
    """[预检] image 块 base64，OCR 读出机密词 ⇒ drawing_local_only"""
    gw = _gw(client.post("/v1/messages", headers=H, json=_messages([
        {"type": "text", "text": "e2e-M1 识别这张图"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": _png_data_url("CONFIDENTIAL").split(",", 1)[1]}}])))
    assert gw["action"] == "route_local", gw
    assert gw["policy_rule"] == "drawing_local_only", gw


def test_m2_messages_document_broken_pdf_fail_closed():
    """[预检+FC] document 块损坏 PDF ⇒ fail-closed"""
    raw = b"%PDF-1.7\ne2e-M2 broken\n%%EOF"
    gw = _gw(client.post("/v1/messages", headers=H, json=_messages([
        {"type": "text", "text": "e2e-M2 看下附件"},
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                        "data": base64.b64encode(raw).decode()}}])))
    assert gw["action"] == "route_local", gw
    assert gw["policy_rule"] in FC_RULES, gw


def test_m3_messages_document_remote_url_fail_closed():
    """[FC] document source.type=url，字节不过网关 ⇒ 内容不可检 ⇒ fail-closed 转内网"""
    gw = _gw(client.post("/v1/messages", headers=H, json=_messages([
        {"type": "text", "text": "e2e-M3 看下附件"},
        {"type": "document", "source": {"type": "url", "url": "https://example.com/a.pdf"}}])))
    assert gw["action"] == "route_local", gw
    assert gw["policy_rule"] == "remote_attachment_route_local", gw
    a = _last_audit("e2e-M3")
    assert a and isinstance(a.get("file_findings"), dict) and a["file_findings"].get("remote_attachment"), a


def test_m4_messages_unknown_source_fail_closed():
    """[FC] 未知 source 类型 ⇒ fail-closed（对齐口径，不静默放行）"""
    gw = _gw(client.post("/v1/messages", headers=H, json=_messages([
        {"type": "text", "text": "e2e-M4 看下附件"},
        {"type": "image", "source": {"type": "blob", "data": "AAAA"}}])))
    assert gw["action"] == "route_local", gw
    assert gw["policy_rule"] == "ocr_empty_image_route_local", gw


# ---------- D. /v1/files/check（专用预检端点，恒检） ----------
def test_f1_files_check_xlsx_prechecked():
    """[预检] 上传 xlsx 恒预检：sheet 名命中 + findings 落返回体"""
    raw = _xlsx("工资表", ["姓名", "身份证", "工资"], [["李四", "110101199001011238", "25000"]])
    r = client.post("/v1/files/check", headers=H,
                    files={"file": ("e2e_f1.xlsx", raw, "application/octet-stream")})
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d["gateway"]["action"] == "route_local", d
    assert d["findings"], "预检 findings 必须落返回体"
    assert "工资表" in (d["findings"] or {}).get("sheet_names", []), d["findings"]


def test_f2_files_check_benign_txt_prechecked():
    """[预检] 良性 txt 也恒预检：放行且 findings/preview 非空（证明解析跑过）"""
    r = client.post("/v1/files/check", headers=H,
                    files={"file": ("e2e_f2.txt", b"hello world e2e-f2", "text/plain")})
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d["gateway"]["action"] == "allow", d
    assert d["findings"] and d["preview"].strip(), d
