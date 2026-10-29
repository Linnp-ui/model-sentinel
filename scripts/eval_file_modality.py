#!/usr/bin/env python3
"""文件通道与多模态样本评测：xlsx / PDF（文本层 + 扫描件 OCR）/ 图片 OCR / 内联图片 chat。

用法: .venv/bin/python scripts/eval_file_modality.py
输出: docs/thesis/eval/file-modality-results.json

隔离环境与 tests/conftest.py 一致：内存审计后端、L2 关闭、外网路由走产品 mock。
期望标签：敏感样本应 route_local/block（不出境），良性样本应 allow。
"""
import base64
import io
import json
import os
import statistics
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)
sys.path.insert(0, REPO)

os.environ.setdefault("AI_GATEWAY_DEV_API_KEY",
                      "sk_shGD8A0lZpIJbcGavN8z6GBcSl-DGIsecJqgF4r0k_o")
os.environ.setdefault("OLLAMA_MOCK", "true")
os.environ.setdefault("SMALL_MODEL_ENABLED", "false")
os.environ.setdefault("AI_GATEWAY_ADMIN_IP_ALLOWLIST", "127.0.0.1,::1,testclient")
for _v in ("ADMIN_DB_URL", "DATABASE_URL", "AUDIT_MYSQL_URL", "AUDIT_DATABASE_URL", "REDIS_URL"):
    os.environ[_v] = ""
_ov = tempfile.mktemp(prefix="gw-eval-l2ov-", suffix=".yaml")
open(_ov, "w").close()
os.environ["AI_GATEWAY_L2_OVERRIDES_PATH"] = _ov
os.environ["AI_GATEWAY_SUGGEST_RULE_SEEN_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="gw-eval-seen-"), "rule_first_seen.json")

import src.gateway.routing as _rt  # noqa: E402
_rt._mock_enabled = lambda *a, **k: True

from fastapi.testclient import TestClient  # noqa: E402
from src.gateway.admin_store import get_admin_store  # noqa: E402
from src.gateway.main import app  # noqa: E402

KEY = "sk_shGD8A0lZpIJbcGavN8z6GBcSl-DGIsecJqgF4r0k_o"
H = {"Authorization": f"Bearer {KEY}"}
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def xlsx_bytes(sheet, headers, rows):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def pdf_text_bytes(text):
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 120), text, fontsize=16, fontname="helv")
    out = doc.tobytes()
    doc.close()
    return out


def _png(text, size=(900, 160), px=40):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", size, "white")
    if text:
        font = ImageFont.truetype(FONT, px)
        ImageDraw.Draw(img).text((20, 30), text, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def pdf_scanned_bytes(text):
    import pymupdf
    png = _png(text, size=(1200, 220), px=44)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(pymupdf.Rect(40, 80, 555, 175), stream=png)
    out = doc.tobytes()
    doc.close()
    return out


def data_url(png):
    return "data:image/png;base64," + base64.b64encode(png).decode()


def chat_payload(data_urls, prompt="看下这张图"):
    content = [{"type": "text", "text": prompt}]
    for u in data_urls if isinstance(data_urls, list) else [data_urls]:
        if u:
            content.append({"type": "image_url", "image_url": {"url": u}})
    return {"model": "AB-flash", "messages": [{"role": "user", "content": content}]}


def durl(raw, mime):
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


def responses_payload(text, files):
    content = [{"type": "input_text", "text": text}]
    for fn, raw, mime in files:
        blk = {"type": "input_file", "filename": fn}
        if raw is not None:
            blk["file_data"] = durl(raw, mime)
        content.append(blk)
    return {"model": "AB-flash", "input": [{"role": "user", "content": content}]}


# id, 类别, 说明, 期望("local"=不出境 / "allow"), 请求方式
XLSX = [
    ("xlsx_sheet", "工作表名命中", "工资表2024", ["姓名", "身份证", "工资"], [["张三", "110101199001011237", "30000"]], "salary.xlsx", "local"),
    ("xlsx_filename", "文件名命中", "Sheet1", ["姓名", "工资"], [["李四", "28000"]], "工资表.xlsx", "local"),
    ("xlsx_sheet_budget", "工作表名命中（预算）", "预算表", ["项目", "金额"], [["A", "1"]], "data.xlsx", "local"),
    ("xlsx_filename_cost", "文件名命中（cost）", "Sheet1", ["Product", "Price"], [["A", "100"]], "cost_report.xlsx", "local"),
    ("xlsx_headers", "表头命中", "Sheet1", ["姓名", "身份证号", "工资"], [["王五", "110101199001011237", "42000"]], "data.xlsx", "local"),
    ("xlsx_pii_content", "表头无关，正文身份证号", "Sheet1", ["编号", "证件", "备注"], [["1", "110101199001011237", "x"]], "data.xlsx", "local"),
    ("xlsx_benign_price", "良性（价格表）", "Sheet1", ["Product", "Price"], [["A", "100"]], "price.xlsx", "allow"),
    ("xlsx_benign_normal", "良性（普通表）", "Sheet1", ["Name", "Age"], [["Alice", "30"]], "normal.xlsx", "allow"),
]

PDF = [
    ("pdf_text_conf", "文本层机密", "CONFIDENTIAL SALARY REPORT 30000", "local"),
    ("pdf_text_blueprint", "文本层图纸词", "BLUEPRINT A01 REV C", "local"),
    ("pdf_text_pii", "文本层身份证号", "ID 110101199001011237 NAME ZHANG", "local"),
    ("pdf_scanned_conf", "扫描件（无文本层，走 OCR）", "CONFIDENTIAL SALARY 30000", "local"),
    ("pdf_benign", "良性文本", "WEATHER STATION DATA 2024 PUBLIC", "allow"),
    ("pdf_corrupt", "损坏 PDF（fail-closed）", None, "local"),
]

IMAGE = [
    ("img_phone", "图片 OCR：手机号", "13800138000", "local"),
    ("img_conf", "图片 OCR：机密标记", "CONFIDENTIAL", "local"),
    ("img_pii", "图片 OCR：身份证号", "110101199001011237", "local"),
    ("img_benign", "图片 OCR：良性文字", "HELLO WORLD 2024", "allow"),
    ("img_blank", "空白图片（fail-closed）", None, "local"),
]

CODEX = [
    ("codex_pdf_data", "input_file PDF 带字节（文本层）", "分析附件",
     [("salary_2024.pdf", pdf_text_bytes("CONFIDENTIAL SALARY 30000"), "application/pdf")], "local"),
    ("codex_pdf_name_only", "input_file 仅文件名（财务词）", "分析附件",
     [("工资表.pdf", None, None)], "local"),
    ("codex_xlsx_data", "input_file xlsx 带字节（身份证）", "分析附件",
     [("salary.xlsx", xlsx_bytes("工资表2024", ["姓名", "身份证", "工资"],
                                 [["张三", "110101199001011237", "30000"]]), XLSX_MIME)], "local"),
    ("codex_csv_data", "input_file CSV 带字节（表头）", "分析附件",
     [("report.csv", "姓名,工资\n张三,30000\n".encode(), "text/csv")], "local"),
    ("codex_csv_benign", "input_file CSV 良性", "分析附件",
     [("public_stats.csv", "Product,Price\nA,100\n".encode(), "text/csv")], "allow"),
    ("codex_name_benign", "input_file 仅文件名（良性）", "分析附件",
     [("public_stats.xlsx", None, None)], "allow"),
    ("codex_drawing_name", "input_file 仅文件名（图纸词）", "分析附件",
     [("施工图_A01.pdf", None, None)], "local"),
    ("codex_broken_data", "input_file 损坏字节（fail-closed）", "分析附件",
     [("broken.pdf", b"%PDF-1.7\nbroken garbage\n%%EOF", "application/pdf")], "local"),
]

WORKBUDDY = [
    ("wb_paste_id", "粘贴文本 + 文件名（身份证）", "附件 员工表.xlsx：张三 110101199001011237，帮我核对", "local"),
    ("wb_taint_follow", "文件名污染（上文同文件，无敏感词）", "员工表.xlsx 的第二行是谁？", "local"),
    ("wb_paste_finance", "粘贴文本（财务报表）", "这是 salary.xlsx 里的财务报表，帮我分析", "local"),
    ("wb_paste_benign", "粘贴文本良性", "附件 normal.xlsx：Product A 100，汇总一下", "allow"),
]

CHAT = [
    ("chat_img_phone", "内联图片：手机号", "13800138000", "local"),
    ("chat_img_conf", "内联图片：机密标记", "CONFIDENTIAL", "local"),
    ("chat_img_pii", "内联图片：身份证号", "110101199001011237", "local"),
    ("chat_img_benign", "内联图片：良性文字", "HELLO WORLD 2024", "allow"),
    ("chat_img_blank", "内联空白图（fail-closed）", None, "local"),
]


def verdict(gw_action):
    return "allow" if gw_action == "allow" else "local"


def main():
    store = get_admin_store()
    try:
        _, found, _ = store.find_key_entry(KEY)
        if not found:
            store.create_api_key(KEY, "eval", "", "file/modality eval")
    except Exception:
        pass

    cases = []
    with TestClient(app) as client:
        for cid, desc, sheet, headers, rows, fn, exp in XLSX:
            data = xlsx_bytes(sheet, headers, rows)
            t0 = time.perf_counter()
            r = client.post("/v1/files/check", headers=H,
                            files={"file": (fn, data, XLSX_MIME)})
            ms = (time.perf_counter() - t0) * 1000
            cases.append(dict(id=cid, kind="xlsx", desc=desc, expected=exp,
                              status=r.status_code, gw=r.json().get("gateway", {}),
                              ms=round(ms, 3), file=fn))

        for cid, desc, text, exp in PDF:
            if text is None:
                data = b"%PDF-1.7\nbroken garbage\n%%EOF"
            elif cid == "pdf_scanned_conf":
                data = pdf_scanned_bytes(text)
            else:
                data = pdf_text_bytes(text)
            t0 = time.perf_counter()
            r = client.post("/v1/files/check", headers=H,
                            files={"file": (cid + ".pdf", data, "application/pdf")})
            ms = (time.perf_counter() - t0) * 1000
            cases.append(dict(id=cid, kind="pdf", desc=desc, expected=exp,
                              status=r.status_code, gw=r.json().get("gateway", {}),
                              ms=round(ms, 3), file=cid + ".pdf"))

        for cid, desc, text, exp in IMAGE:
            png = _png(text)
            t0 = time.perf_counter()
            r = client.post("/v1/files/check", headers=H,
                            files={"file": (cid + ".png", png, "image/png")})
            ms = (time.perf_counter() - t0) * 1000
            cases.append(dict(id=cid, kind="image", desc=desc, expected=exp,
                              status=r.status_code, gw=r.json().get("gateway", {}),
                              ms=round(ms, 3), file=cid + ".png"))

        for cid, desc, text, exp in CHAT:
            png = _png(text)
            t0 = time.perf_counter()
            r = client.post("/v1/chat/completions", headers=H,
                            json=chat_payload(data_url(png)))
            ms = (time.perf_counter() - t0) * 1000
            cases.append(dict(id=cid, kind="chat_image", desc=desc, expected=exp,
                              status=r.status_code, gw=r.json().get("gateway", {}),
                              ms=round(ms, 3), file=cid + ".png"))

        for cid, desc, text, files, exp in CODEX:
            t0 = time.perf_counter()
            r = client.post("/v1/responses", headers=H,
                            json=responses_payload(f"{text}（{cid}）", files))
            ms = (time.perf_counter() - t0) * 1000
            cases.append(dict(id=cid, kind="codex", desc=desc, expected=exp,
                              status=r.status_code, gw=r.json().get("gateway", {}),
                              ms=round(ms, 3), file=files[0][0]))

        for cid, desc, text, exp in WORKBUDDY:
            t0 = time.perf_counter()
            r = client.post("/v1/chat/completions", headers=H,
                            json=chat_payload(None, prompt=text))
            ms = (time.perf_counter() - t0) * 1000
            cases.append(dict(id=cid, kind="workbuddy", desc=desc, expected=exp,
                              status=r.status_code, gw=r.json().get("gateway", {}),
                              ms=round(ms, 3), file=""))

        multi = [
            ("wb_multi_image", "多图内联（手机号 + 良性）",
             [_png("13800138000"), _png("HELLO WORLD 2024")], "local"),
            ("wb_multi_benign", "多图内联（均良性）",
             [_png("HELLO WORLD 2024"), _png("WEATHER DATA 2024")], "allow"),
        ]
        for cid, desc, pngs, exp in multi:
            t0 = time.perf_counter()
            r = client.post("/v1/chat/completions", headers=H,
                            json=chat_payload([data_url(x) for x in pngs]))
            ms = (time.perf_counter() - t0) * 1000
            cases.append(dict(id=cid, kind="workbuddy", desc=desc, expected=exp,
                              status=r.status_code, gw=r.json().get("gateway", {}),
                              ms=round(ms, 3), file=cid + ".png"))

    for c in cases:
        c["actual"] = verdict(c["gw"].get("action", ""))
        c["rule"] = c["gw"].get("policy_rule") or c["gw"].get("rule") or ""
        c["ok"] = c["actual"] == c["expected"]

    def summary(kind=None):
        sel = [c for c in cases if kind is None or c["kind"] == kind]
        pos = [c for c in sel if c["expected"] == "local"]
        neg = [c for c in sel if c["expected"] == "allow"]
        tp = sum(1 for c in pos if c["actual"] == "local")
        fp = sum(1 for c in neg if c["actual"] == "local")
        return {
            "n": len(sel),
            "correct": sum(1 for c in sel if c["ok"]),
            "recall": round(tp / len(pos), 4) if pos else None,
            "fpr": round(fp / len(neg), 4) if neg else None,
            "latency_ms_median": round(statistics.median([c["ms"] for c in sel]), 2),
        }

    # 长文本探针（>100KB）：敏感词置于尾部，验证审查不因长度截断而漏检
    with TestClient(app) as client:
        long_probe = []
        for label, txt, exp in (
            ("long_sensitive", "常规业务内容 " * 20000 + " 员工身份证 110101199001011237", "local"),
            ("long_benign", "常规业务内容 " * 20000 + " 天气预报", "allow"),
        ):
            t0 = time.perf_counter()
            r = client.post("/v1/chat/completions", headers=H,
                            json=chat_payload(None, prompt=txt))
            ms = (time.perf_counter() - t0) * 1000
            gw = r.json().get("gateway", {})
            long_probe.append({"id": label, "chars": len(txt), "expected": exp,
                               "actual": verdict(gw.get("action", "")),
                               "rule": gw.get("policy_rule") or gw.get("rule") or "",
                               "ms": round(ms, 2)})

    # OCR 抽取抽查：真实解析结果里是否出现植入串（空白图期望为空）
    from src.gateway.file_inspector import inspect_file
    ocr_probe = []
    for text in ("13800138000", "110101199001011237", "CONFIDENTIAL",
                 "HELLO WORLD 2024", None):
        info = inspect_file(_png(text), "probe.png")
        got = (info.get("text") or "").strip()
        ocr_probe.append({"planted": text, "extracted": got[:80],
                          "ok": (text.lower() in got.lower()) if text else not got})
    scanned = inspect_file(pdf_scanned_bytes("CONFIDENTIAL SALARY 30000"), "probe.pdf")
    got = (scanned.get("text") or "").strip()
    ocr_probe.append({"planted": "CONFIDENTIAL SALARY 30000 (scanned pdf)",
                      "extracted": got[:80], "ok": "confidential" in got.lower()})

    result = {
        "cases": cases,
        "summary_all": summary(),
        "summary_by_kind": {k: summary(k) for k in ("xlsx", "pdf", "image", "chat_image", "codex", "workbuddy")},
        "ocr_probe": ocr_probe,
        "ocr_probe_ok": sum(1 for p in ocr_probe if p["ok"]),
        "ocr_probe_n": len(ocr_probe),
        "long_text_probe": long_probe,
    }
    os.makedirs(os.path.join(REPO, "docs/thesis/eval"), exist_ok=True)
    out = os.path.join(REPO, "docs/thesis/eval/file-modality-results.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"{'id':22} {'kind':11} {'exp':6} {'got':6} {'rule':16} {'ms':>8}")
    for c in cases:
        print(f"{c['id']:22} {c['kind']:11} {c['expected']:6} {c['actual']:6} "
              f"{c['rule'][:16]:16} {c['ms']:8.1f}")
    print(json.dumps({k: result[k] for k in ("summary_all", "summary_by_kind")},
                     ensure_ascii=False, indent=2))
    print("OCR probe:", result["ocr_probe_ok"], "/", result["ocr_probe_n"])
    print("long text:", [(p["id"], p["chars"], p["actual"], p["rule"]) for p in long_probe])
    print("wrote", out)


if __name__ == "__main__":
    main()
