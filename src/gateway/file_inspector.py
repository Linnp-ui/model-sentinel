"""
File Inspector v2 — 复杂文档增强版
src/gateway/file_inspector.py:1
- PDF: pypdf 全量 + OCR 兜底 + 水印
- XLSX: 多Sheet + 合并单元格展开 + Markdown 表格 + 全量表头
- DOCX/PPT/TXT: 基础支持
- CAD: ezdxf 文本提取占位，失败则靠文件名
返回: {text: 全量文本, markdown: 表格Markdown, findings, sheet_names, headers, chunks}
"""
from __future__ import annotations
import base64
import io
import os
import re
from typing import Dict, Any, List, Optional, Tuple


# --- P0-3 fix: magic-byte sniff (don't trust filename ext alone) ---
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"%PDF-", "pdf"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
    (b"AC10", "dwg"),
    # PK\x03\x04（zip 容器）在 _sniff_magic 里单独处理：xlsx/docx/pptx 同为 PK 头，
    # 必须看 zip 内部目录才能分清，否则「真 docx 改名 .csv」会被判成 xlsx 再解析失败。
)


def _sniff_ooxml(data: bytes) -> str:
    """PK 头 → ooxml 子类型。认不出 / 坏 zip 时回 'xlsx'（保持旧语义，交给解析失败兜底）。"""
    try:
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()[:500]
        for n in names:
            if n.startswith("word/"):
                return "docx"
            if n.startswith("ppt/"):
                return "pptx"
            if n.startswith("xl/"):
                return "xlsx"
    except Exception:
        pass
    return "xlsx"


def _sniff_magic(data: bytes) -> Optional[str]:
    """Return canonical ext from first bytes, or None if unknown.
    Used to override user-supplied filename when they conflict (e.g. salary.txt
    is actually a PNG with sensitive text)."""
    if not data:
        return None
    head = data[:16]
    for magic, ext in _MAGIC:
        if head.startswith(magic):
            return ext
    if head.startswith(b"PK\x03\x04"):
        # xlsx/docx/pptx 同为 PK 头 —— 必须看 zip 内部目录细分
        return _sniff_ooxml(data)
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if len(data) >= 12 and data[4:8] == b'ftyp':
        brand = data[8:12]
        if brand in (b'heic', b'heix', b'hevc', b'hevx', b'heim', b'heis',
                       b'hevm', b'hevs', b'mif1', b'msf1'):
            return 'heic'
        if brand in (b'avif', b'avis'):
            return 'avif'
    return None


# --- P0-3b: 真格式优先 + 解析失败 fail-closed ---
# 后缀族 / magic 族。族不一致 ⇒ 视为「扩展名伪装」，按真格式解析。
# 注意 tsv 不在 sheet 族：它是纯文本，走通用文本分支 + inspect_text
# （划进 sheet 族会被 openpyxl 解析失败误伤成 parse_failed）。
_EXT_FAMILY = {
    "pdf": "pdf",
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image", "bmp": "image",
    "tiff": "image", "tif": "image", "webp": "image", "heic": "image", "heif": "image",
    "avif": "image",
    "xlsx": "sheet", "xls": "sheet", "csv": "sheet",
    "docx": "doc", "doc": "doc", "pptx": "doc", "ppt": "doc",
    "dwg": "cad", "dxf": "cad", "rvt": "cad", "skp": "cad",
}

_MAGIC_FAMILY = {
    "png": "image", "jpg": "image", "gif": "image", "bmp": "image", "tiff": "image",
    "webp": "image", "heic": "image", "avif": "image",
    "pdf": "pdf",
    "xlsx": "sheet", "docx": "doc", "pptx": "doc",
    "dwg": "cad",
}

_PARSE_ERROR_MARKS = ("[pdf_parse_error:", "[excel_parse_error:", "[docx_parse_error:")

# 二进制定性阈值：UTF-8 解不出来的字节占比超过它 ⇒ 不当文本读
_BINARY_RATIO_MAX = 0.05


def _looks_textual(raw: bytes) -> bool:
    """这段字节能不能安全当文本读。

    含 NUL 直接否；否则按 UTF-8 解码丢弃率判断（留 5% 余量给混合编码日志）。
    旧实现 decode(errors="ignore") 无条件当文本读 ⇒ 未知二进制被 dump 成乱码、
    抽不出关键词 ⇒ 静默 allow。
    """
    if not raw:
        return True
    if b"\x00" in raw[:4096]:
        return False
    try:
        raw.decode("utf-8")
        return True
    except UnicodeDecodeError:
        dropped = len(raw) - len(raw.decode("utf-8", errors="ignore").encode("utf-8"))
        return (dropped / len(raw)) <= _BINARY_RATIO_MAX


FINANCIAL_SHEET_KEYWORDS = ["工资","薪资","财务","预算","决算","成本","营收","利润","资产负债","费用","报销","报价","合同","采购"]
FINANCIAL_HEADER_KEYWORDS = ["姓名","身份证","工资","薪资","银行账号","银行卡","金额","成本","利润","营收","费用","部门","工号","单价","总价","税率"]
DRAWING_KEYWORDS = ["机密","保密","内部资料","不得外传","设计图纸","施工图","结构图","图号","confidential","secret","blueprint","drawing","CAD","BIM"]
SECRET_PATTERNS = [r"AKIA[0-9A-Z]{16}", r"-----BEGIN (?:RSA )?PRIVATE KEY-----"]

def _contains_any(text: str, keywords: List[str]) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in keywords)

def _to_markdown_table(headers: List[str], rows: List[List[Any]]) -> str:
    if not headers:
        return ""
    md = "| " + " | ".join(headers) + " |\n"
    md += "| " + " | ".join(["---"]*len(headers)) + " |\n"
    for r in rows[:15]:  # 每表限15行防超长
        vals = [str(v) if v is not None else "" for v in r]
        # 补齐列数
        while len(vals) < len(headers):
            vals.append("")
        md += "| " + " | ".join(vals[:len(headers)]) + " |\n"
    return md

def inspect_pdf(data: bytes, filename: str) -> Dict[str, Any]:
    text = ""
    pages = 0
    has_ocr = False
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = len(reader.pages)
        # 全量提取（限50页防大文件）
        for idx, p in enumerate(reader.pages[:50]):
            try:
                t = p.extract_text() or ""
                text += t + f"\n\n[PAGE {idx+1}]\n"
            except Exception:
                continue
        # 若提取文本过短 (<200字) 可能是扫描件，尝试 OCR
        if len(text.strip()) < 200:
            ocr_text = _ocr_pdf_fallback(data)
            if ocr_text:
                text += "\n[OCR]\n" + ocr_text
                has_ocr = True
    except Exception as e:
        text = f"[pdf_parse_error: {e}]"
        ocr_text = _ocr_pdf_fallback(data)
        if ocr_text:
            text += "\n[OCR]\n" + ocr_text
            has_ocr = True

    findings = {
        "text_preview": text[:500],
        "markdown_preview": text[:500],
        "has_drawing_keyword": _contains_any(text, DRAWING_KEYWORDS),
        "has_financial_keyword": _contains_any(text, FINANCIAL_SHEET_KEYWORDS + FINANCIAL_HEADER_KEYWORDS),
        "has_watermark": _contains_any(text, ["机密","保密","Watermark"]),
        "page_count": pages,
        "has_ocr": has_ocr,
        "char_count": len(text),
    }
    # 分块供 L2
    chunks = _chunk_text(text, 1800)
    return {"text": text, "markdown": text, "findings": findings, "chunks": chunks}

# OCR 输入归一化：tesseract 耗时约随像素数线性涨，手机 12MP 照片先压到长边 2000px
# （截图文字精度几乎无损，速度数倍提升）。跨图并行已在 main._inline_media_scan
# 用 asyncio.gather+线程池做；生产是 pytesseract（子进程，真并行）。
_OCR_MAX_DIM = int(os.getenv('AI_GATEWAY_OCR_MAX_DIM', '2000'))


def _fit_ocr_size(img):
    """长边压到 _OCR_MAX_DIM 以内；小图原样返回。"""
    try:
        w, h = img.size
        if max(w, h) > _OCR_MAX_DIM:
            img = img.copy()
            img.thumbnail((_OCR_MAX_DIM, _OCR_MAX_DIM))
    except Exception:
        pass
    return img


def _ocr_pil_image(img) -> tuple:
    """引擎链（paddle 中文优先 → pytesseract chi_sim+eng 兜底）。返回 (text, err_str)。"""
    text = ""
    err = None
    try:
        from paddleocr import PaddleOCR
        global _ocr_engine
        if "_ocr_engine" not in globals():
            _ocr_engine = PaddleOCR(lang="ch", use_angle_cls=False, show_log=False)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        res = _ocr_engine.ocr(buf.getvalue(), cls=False)
        if res and res[0]:
            text = " ".join([line[1][0] for line in res[0]])
    except Exception:
        try:
            import pytesseract
            text = pytesseract.image_to_string(img, lang="chi_sim+eng", config="--psm 6")
        except Exception as e:
            err = f"ocr_lib_missing: {type(e).__name__}"
    return text, err


def _ocr_pixmap(pix) -> tuple:
    """OCR a single pymupdf Pixmap. Returns (text, err_str)."""
    from PIL import Image
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    return _ocr_pil_image(_fit_ocr_size(img))


def _ocr_pdf_fallback(data: bytes) -> str:
    """OCR fallback: scanned PDF with no extractable text.
    Priority: paddleocr (Chinese accuracy) > pytesseract + tesseract-ocr (chi_sim+eng).
    Limit: up to 5 pages, 150 DPI, <=8000 chars.
    """
    import time as _t
    from .metrics import get_metrics
    t0 = _t.time()
    pages_ocr = 0
    chars = 0
    last_err = None
    try:
        try:
            import pymupdf
        except ImportError:
            import fitz as pymupdf
        doc = pymupdf.open(stream=data, filetype="pdf")
        ocr_text = ""
        max_pages = min(5, len(doc))
        for i in range(max_pages):
            try:
                pix = doc[i].get_pixmap(dpi=150)
            except Exception:
                continue
            page_text, err = _ocr_pixmap(pix)
            if err:
                last_err = err
            if page_text:
                pages_ocr += 1
                ocr_text += page_text + f"\n[OCR_PAGE {i+1}]\n"
                chars += len(page_text)
            if chars >= 8000:
                break
        latency_ms = (_t.time() - t0) * 1000
        m = get_metrics()
        m.inc_ocr_pages(pages_ocr)
        m.observe_ocr_latency(latency_ms)
        if last_err:
            m.inc_ocr_errors()
        return ocr_text[:8000]
    except Exception:
        try:
            get_metrics().inc_ocr_errors()
        except Exception:
            pass
        return ""


# Size cap: anything above this gets no OCR (anti-bomb).
_IMAGE_OCR_MAX_BYTES = 20 * 1024 * 1024


def _open_heif_image(data: bytes):
    """pillow-heif 解 HEIC/AVIF → 纯 PIL Image(RGB)；失败/没装库返回 None。

    必须压平成纯 PIL Image：Image.open 直接返回的是 HeifImageFile，
    pytesseract.prepare() 会 TypeError 拒收（生产实测）。"""
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.load()
        rgb = img.convert("RGB")
        if type(rgb) is not Image.Image:
            rgb = Image.frombytes("RGB", rgb.size, rgb.tobytes())
        return rgb
    except Exception:
        return None


def _ocr_heif_bytes(data: bytes) -> str:
    """OCR 一张 HEIC/AVIF。引擎缺失/解码失败返回 ""（上层 fail-closed）。"""
    import time as _t
    from .metrics import get_metrics
    if len(data) > _IMAGE_OCR_MAX_BYTES:
        try:
            get_metrics().inc_ocr_errors()
            get_metrics().observe_ocr_latency(0)
        except Exception:
            pass
        return ""
    t0 = _t.time()
    err = None
    pages_ocr = 0
    try:
        img = _open_heif_image(data)
        if img is None:
            err = "heif_decode_failed"
            return ""
        text, perr = _ocr_pil_image(_fit_ocr_size(img))
        if perr:
            err = perr
        if text:
            pages_ocr = 1
        return text[:8000]
    except Exception:
        try:
            get_metrics().inc_ocr_errors()
        except Exception:
            pass
        return ""
    finally:
        try:
            m = get_metrics()
            m.inc_ocr_pages(pages_ocr)
            m.observe_ocr_latency((_t.time() - t0) * 1000)
            if err:
                m.inc_ocr_errors()
        except Exception:
            pass


_HEIF_EXTS = ("heic", "heif", "avif")


def _ocr_image_bytes(data: bytes, ext: str) -> str:
    """OCR a single image (png/jpg/jpeg/tiff/bmp/webp/heic/heif/avif).
    Uses pymupdf to decode + render. Same engine chain as PDF OCR.
    Size cap: 20MB; corrupted/missing engine -> fail-open empty string.
    """
    import time as _t
    from .metrics import get_metrics
    if len(data) > _IMAGE_OCR_MAX_BYTES:
        try:
            get_metrics().inc_ocr_errors()
            get_metrics().observe_ocr_latency(0)
        except Exception:
            pass
        return ""
    if (ext or "").lower() in _HEIF_EXTS or _sniff_magic(data) in ("heic", "avif"):
        return _ocr_heif_bytes(data)
    t0 = _t.time()
    err = None
    pages_ocr = 0
    doc = None
    try:
        try:
            import pymupdf
        except ImportError:
            import fitz as pymupdf
        doc = pymupdf.open(stream=data, filetype=ext if ext else "png")
        if len(doc) == 0:
            err = "image_empty"
            return ""
        try:
            pix = doc[0].get_pixmap(dpi=150)
        except Exception as e:
            err = f"pixmap_failed: {type(e).__name__}"
            return ""
        text, perr = _ocr_pixmap(pix)
        if perr:
            err = perr
        if text:
            pages_ocr = 1
        return text[:8000]
    except Exception:
        try:
            get_metrics().inc_ocr_errors()
        except Exception:
            pass
        return ""
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
        try:
            m = get_metrics()
            m.inc_ocr_pages(pages_ocr)
            m.observe_ocr_latency((_t.time() - t0) * 1000)
            if err:
                m.inc_ocr_errors()
        except Exception:
            pass


def inspect_image(data: bytes, filename: str) -> Dict[str, Any]:
    """Inspect a raster image: run OCR, return unified schema.
    Used by inspect_file() for png/jpg/jpeg/tiff/bmp/webp extensions.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
    text = _ocr_image_bytes(data, ext)
    # P0-3: when OCR returns nothing for an image, fail-CLOSED. Either:
    #   - OCR engine missing (paddleocr/pytesseract not installed)
    #   - Image is corrupted / not really an image
    #   - Image is HEIC/AVIF/etc not in our supported set
    #   - OCR mis-read all chars
    # In all cases policy.yaml will route_local via ocr_empty_image_route_local.
    ocr_empty = (text == "")
    findings = {
        "text_preview": text[:500],
        "has_drawing_keyword": _contains_any(text, DRAWING_KEYWORDS),
        "has_financial_keyword": _contains_any(text, FINANCIAL_SHEET_KEYWORDS + FINANCIAL_HEADER_KEYWORDS),
        "char_count": len(text),
        "is_image": True,
        "has_ocr": True,  # image branch always involves OCR (best-effort)
        "ocr_empty_and_image": ocr_empty,
    }
    return {
        "text": text,
        "markdown": text,
        "findings": findings,
        "chunks": _chunk_text(text, 1800),
    }


def inspect_excel(data: bytes, filename: str) -> Dict[str, Any]:
    sheet_names: List[str] = []
    all_headers: List[str] = []
    sample_text = ""
    markdown_all = ""
    tables: List[Dict] = []
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=False, data_only=True)
        sheet_names = wb.sheetnames
        for ws in wb.worksheets:
            # 展开合并单元格：用左上角值填充
            merged = {}
            for m in ws.merged_cells.ranges:
                min_col, min_row, max_col, max_row = m.bounds
                val = ws.cell(row=min_row, column=min_col).value
                for r in range(min_row, max_row+1):
                    for c in range(min_col, max_col+1):
                        merged[(r,c)] = val
            def cell_val(r,c):
                if (r,c) in merged:
                    return merged[(r,c)]
                return ws.cell(row=r, column=c).value

            # 读取维度
            max_row = ws.max_row or 0
            max_col = ws.max_column or 0
            if max_row == 0 or max_col == 0:
                continue
            # 限制每表 100行 20列防大文件
            max_row = min(max_row, 100)
            max_col = min(max_col, 20)

            # 智能表头探测：跳过合并标题行，取首个含2+列且含财务关键词或典型表头行
            headers = []
            header_row = 1
            for r in range(1, min(6, max_row+1)):
                row_vals = [cell_val(r,c) for c in range(1, max_col+1)]
                row_vals = [str(v).strip() if v is not None else "" for v in row_vals]
                non_empty = [v for v in row_vals if v]
                if not non_empty:
                    continue
                # 跳过合并标题：所有非空值相同且含“表/汇总”
                if len(set(non_empty)) == 1 and any(k in non_empty[0] for k in ["表","汇总","统计"]):
                    continue
                # 典型表头：至少2列且含财务关键词，或至少3列
                if _contains_any(" ".join(row_vals), FINANCIAL_HEADER_KEYWORDS) or len(non_empty) >= 3:
                    headers = [v for v in row_vals if v]
                    header_row = r
                    break
                if r == 1 and len(non_empty) >= 2:
                    headers = [v for v in row_vals if v]
                    header_row = r
                    break
            if not headers:
                headers = [f"列{i+1}" for i in range(max_col)]
                header_row = 1
            all_headers.extend(headers)

            # 行数据（从表头下一行开始）
            rows = []
            for r in range(header_row+1, min(max_row+1, 80)):
                vals = [cell_val(r,c) for c in range(1, len(headers)+1)]
                if any(v not in (None,"") for v in vals):
                    rows.append(vals)
                    sample_text += " ".join([str(v) for v in vals if v not in (None,"")]) + "\n"

            # Markdown
            md = f"### Sheet: {ws.title}\n" + _to_markdown_table(headers, rows)
            markdown_all += md + "\n"
            tables.append({"sheet": ws.title, "headers": headers, "rows": len(rows)})

            # 公式探测（若 data_only=False 时可另读）
            # 已用 data_only=True 取值，若需公式可再 load data_only=False 对比

        wb.close()
    except Exception as e:
        sample_text = f"[excel_parse_error: {e}]"
        markdown_all = sample_text

    # CSV 回退（P0-3b：只有「真能当文本读」才回退。旧实现把二进制原始字节也
    # decode 成乱码文本 ⇒ 抽不出关键词 ⇒ allow；现在改留 [excel_parse_error]，
    # 让 inspect_file 出口的 parse_failed 兜底）
    if not sheet_names and filename.lower().endswith(".csv"):
        blob = data[:20000]
        if _looks_textual(blob):
            try:
                sample_text = blob.decode("utf-8", errors="ignore")
                lines = sample_text.splitlines()
                headers = [h.strip() for h in lines[0].split(",")] if lines else []
                sheet_names = [filename]
                all_headers = headers
                markdown_all = sample_text[:5000]
            except Exception:
                pass
        else:
            sample_text = f"[excel_parse_error: binary payload claimed as csv ({len(blob)}B)]"
            markdown_all = sample_text

    # 多Sheet 文本聚合
    full_text = f"Sheets: {', '.join(sheet_names)}\nHeaders: {', '.join(all_headers)}\n" + sample_text[:15000]
    findings = {
        "sheet_names": sheet_names,
        "headers": all_headers,
        "tables": tables,
        "sample_preview": sample_text[:500],
        "markdown_preview": markdown_all[:500],
        "has_financial_sheet": _contains_any(" ".join(sheet_names), FINANCIAL_SHEET_KEYWORDS),
        "has_financial_header": _contains_any(" ".join(all_headers), FINANCIAL_HEADER_KEYWORDS),
        "idcard_count": len(re.findall(r"\d{17}[\dXx]", sample_text)),
        "phone_count": len(re.findall(r"1[3-9]\d{9}", sample_text)),
        "char_count": len(sample_text),
        "sheet_count": len(sheet_names),
    }
    chunks = _chunk_text(full_text + "\n" + markdown_all, 1800)
    return {"text": full_text, "markdown": markdown_all, "findings": findings, "sheet_names": sheet_names, "headers": all_headers, "chunks": chunks}

def inspect_docx(data: bytes, filename: str) -> Dict[str, Any]:
    text = ""
    try:
        import docx
        doc = docx.Document(io.BytesIO(data))
        for p in doc.paragraphs[:200]:
            text += p.text + "\n"
        for t in doc.tables[:5]:
            for row in t.rows[:20]:
                text += " | ".join([c.text for c in row.cells]) + "\n"
    except Exception as e:
        text = f"[docx_parse_error: {e}]"
    findings = {"text_preview": text[:500], "has_drawing_keyword": _contains_any(text, DRAWING_KEYWORDS), "char_count": len(text)}
    return {"text": text, "markdown": text, "findings": findings, "chunks": _chunk_text(text, 1800)}

def _chunk_text(text: str, size: int = 1800) -> List[str]:
    if not text:
        return []
    chunks = []
    for i in range(0, len(text), size):
        chunks.append(text[i:i+size])
        if len(chunks) >= 10:  # 限10块防超长
            break
    return chunks

def inspect_file(data: bytes, filename: str) -> Dict[str, Any]:
    """按**真格式**（magic 字节）而非声明后缀分派，解析失败一律 fail-closed 留痕。

    P0-3b（2026-09-17）：旧实现**先按后缀分派**，magic 嗅探放在 if 链**末尾**且
    只覆盖 txt/csv/log/md/空后缀（csv 分支还被 excel 分支先截走 = 死代码）。于是
    薪资 PDF / 密钥截图改名 .csv/.docx/.xlsx 时，富格式分支解析失败只留一行
    [*_parse_error]，decide() 抽不到任何关键词 ⇒ **allow 零检查出境**。现在：
      1. 先嗅探 magic，与声明后缀的「族」比对，族不一致就按真格式解析并盖
         ext_mismatch/magic_ext/claimed_ext 痕迹；
      2. 任何分支解析失败 ⇒ findings.parse_failed（不再静默）；
      3. 未知二进制不许 dump 成文本 ⇒ findings.unparsed_binary；
      4. 2/3 由 policy.yaml 的 unparsed_binary_route_local(priority 13) 接住。
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    file_type = ext

    magic_ext = _sniff_magic(data)
    magic_family = _MAGIC_FAMILY.get(magic_ext) if magic_ext else None
    claimed_family = _EXT_FAMILY.get(ext, "text")
    ext_mismatch = magic_family is not None and magic_family != claimed_family
    family = magic_family if ext_mismatch else claimed_family
    tags: Dict[str, Any] = {}
    if ext_mismatch:
        tags = {"ext_mismatch": True, "magic_ext": magic_ext, "claimed_ext": ext}

    def _out(r: Dict[str, Any]) -> Dict[str, Any]:
        """统一出口：补空通道 + 探测解析失败 + 盖上伪装痕迹。"""
        out: Dict[str, Any] = {"ext": ext, "file_type": file_type}
        out.update(r)
        out.setdefault("sheet_names", [])
        out.setdefault("headers", [])
        out.setdefault("chunks", [])
        f = dict(out.get("findings") or {})
        txt = out.get("text") or ""
        if any(m in txt for m in _PARSE_ERROR_MARKS):
            f["parse_failed"] = True  # 解析失败不许静默：由 policy p13 接住
        f.update(tags)
        out["findings"] = f
        return out

    if family == "cad":
        text = ""
        parse_failed = False
        try:
            import ezdxf
            doc = ezdxf.read(io.BytesIO(data))
            msp = doc.modelspace()
            for e in msp[:100]:
                if e.dxftype() in ("TEXT", "MTEXT"):
                    text += str(e.dxf.text) + "\n"
        except Exception:
            parse_failed = True
            text = f"[cad] filename: {filename}"
        findings = {"is_cad": True, "has_drawing_keyword": True, "text_preview": text[:500]}
        if parse_failed:
            if ext_mismatch:
                # 真图纸被改成别的后缀（如 AC1032 头落地 .txt）：内容无从抽取 ⇒ fail-closed
                findings["unparsed_binary"] = True
            elif magic_ext is None and not _looks_textual(data[:50000]):
                # 声明 .dwg/.dxf，但内容既不是文本（真 DXF 是 ASCII）、也认不出 magic
                # ⇒ 可疑二进制 ⇒ fail-closed。判据用「是否文本」而不是「ezdxf 是否可用」：
                # 本机没装 ezdxf 时真 DXF 也会 parse_failed=True，但它是文本，不该因此改判。
                findings["parse_failed"] = True
        return _out({"text": text, "markdown": text, "findings": findings,
                     "chunks": _chunk_text(text, 1800)})

    if family == "pdf":
        return _out(inspect_pdf(data, filename))

    if family == "image":
        return _out(inspect_image(data, filename))

    if family == "sheet":
        return _out(inspect_excel(data, filename))

    if family == "doc":
        return _out(inspect_docx(data, filename))

    # 通用文本：先判「是否真能当文本读」，未知二进制一律不 dump
    raw = data[:50000]
    if not _looks_textual(raw):
        return _out({
            "text": "", "markdown": "",
            "findings": {"unparsed_binary": True, "char_count": 0, "text_preview": "",
                         "binary_bytes": len(raw), "magic_ext": magic_ext},
            "chunks": [],
        })
    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        text = ""
    return _out({
        "text": text, "markdown": text,
        "findings": {"text_preview": text[:500], "char_count": len(text)},
        "chunks": _chunk_text(text, 1800),
    })


# --- 方案A: 内联媒体 (chat/responses/messages 里嵌的 base64 图/文档) ---
# 三个入口只抽文本给 policy 时，图片/附件二进制原本被跳过 (只留 URL 前缀/文件名)。
# 这里把 data: URL / base64 source 解码后复用 inspect_file() (magic 嗅探 + OCR +
# ocr_empty fail-closed)，抽出的文本并入 policy 扫描串。外链 http(s) 不下载。
_INLINE_MEDIA_MAX_ITEMS = int(os.getenv('AI_GATEWAY_INLINE_MEDIA_MAX_ITEMS', '5'))
_INLINE_MEDIA_MAX_BYTES = int(os.getenv('AI_GATEWAY_INLINE_MEDIA_MAX_BYTES', str(_IMAGE_OCR_MAX_BYTES)))


def decode_inline_data_url(url: str) -> Optional[bytes]:
    """Decode a data: URL to bytes. None = 不是 data: / 解码失败 / 超 cap。"""
    if not isinstance(url, str) or not url.startswith('data:'):
        return None
    try:
        header, _, b64 = url.partition(',')
        if ';base64' not in header or not b64:
            return None
        raw = base64.b64decode(b64.strip(), validate=True)
    except Exception:
        return None
    if not raw or len(raw) > _INLINE_MEDIA_MAX_BYTES:
        return None
    return raw


def decode_inline_b64(data: str) -> Optional[bytes]:
    """Decode 裸 base64 (Anthropic source.data)。None = 失败/超 cap。"""
    if not isinstance(data, str) or not data:
        return None
    try:
        raw = base64.b64decode(data.strip(), validate=True)
    except Exception:
        return None
    if not raw or len(raw) > _INLINE_MEDIA_MAX_BYTES:
        return None
    return raw


def inspect_inline_bytes(data: bytes) -> Dict[str, Any]:
    """检查一块内联二进制。返回 {'text': str, 'unreadable': bool}。

    unreadable=True (fail-closed) 的情况：magic 未知 / gif 等不支持格式 /
    解析抛错 / 抽出文本为空。调用方应把 unreadable 合并进 findings
    (ocr_empty_and_image)，让 policy.yaml p12 接住。
    """
    try:
        magic = _sniff_magic(data)
        if magic in ('png', 'jpg', 'bmp', 'heic', 'avif'):
            r = inspect_file(data, 'inline.' + magic)
        elif magic == 'pdf':
            r = inspect_file(data, 'inline.pdf')
        elif magic == 'xlsx':
            r = inspect_file(data, 'inline.xlsx')
        elif magic == 'docx':
            r = inspect_file(data, 'inline.docx')
        else:
            return {'text': '', 'unreadable': True}
        text = r.get('text') or ''
        if text.strip() == '' or r.get('findings', {}).get('ocr_empty_and_image'):
            return {'text': text, 'unreadable': True}
        return {'text': text, 'unreadable': False}
    except Exception:
        return {'text': '', 'unreadable': True}


def _content_parts(content: Any) -> list:
    if isinstance(content, str) or content is None:
        return []
    if isinstance(content, list):
        return [p for p in content if isinstance(p, dict)]
    return []


def collect_inline_blobs_chat(messages: Any) -> Tuple[list, bool]:
    """chat messages (pydantic 或 dict) → ([bytes], overflow_or_bad)。"""
    blobs: list = []
    bad = False
    total = 0
    items = messages if isinstance(messages, list) else []
    for m in items:
        c = getattr(m, 'content', None) if not isinstance(m, dict) else m.get('content')
        for p in _content_parts(c):
            t = p.get('type', '')
            url = None
            if t == 'image_url':
                iu = p.get('image_url', '')
                url = iu.get('url', '') if isinstance(iu, dict) else iu
            elif t in ('image', 'input_image'):
                url = p.get('image_url') or p.get('url')
            if isinstance(url, str) and url.startswith('data:'):
                total += 1
                raw = decode_inline_data_url(url)
                if raw is None:
                    bad = True
                elif len(blobs) < _INLINE_MEDIA_MAX_ITEMS:
                    blobs.append(raw)
            if t in ('input_audio', 'audio'):
                au = p.get('input_audio')
                adata = (au.get('data') if isinstance(au, dict) else None) or p.get('data')
                if isinstance(adata, str) and adata.strip():
                    bad = True  # 音频不转写：有数据即 fail-closed
    if total > _INLINE_MEDIA_MAX_ITEMS:
        bad = True  # 超 cap 的部分没检查 → fail-closed
    return blobs, bad


def collect_inline_blobs_anthropic(body: Any) -> Tuple[list, bool]:
    """Anthropic messages body → ([bytes], overflow_or_bad)。"""
    blobs: list = []
    bad = False
    total = 0
    msgs = (body.get('messages', []) or []) if isinstance(body, dict) else []
    for m in msgs:
        c = m.get('content') if isinstance(m, dict) else None
        for b in _content_parts(c):
            if b.get('type') not in ('image', 'document', 'input_image'):
                continue
            src = b.get('source') or {}
            if not isinstance(src, dict):
                continue
            if src.get('type') == 'base64' and isinstance(src.get('data'), str):
                total += 1
                raw = decode_inline_b64(src.get('data'))
                if raw is None:
                    bad = True
                elif len(blobs) < _INLINE_MEDIA_MAX_ITEMS:
                    blobs.append(raw)
            elif src.get('type') not in ('url', 'file', 'text'):
                bad = True  # 未知 source 类型 → fail-closed
    if total > _INLINE_MEDIA_MAX_ITEMS:
        bad = True
    return blobs, bad


def collect_inline_blobs_responses(body: Any) -> Tuple[list, bool]:
    """OpenAI Responses body → ([bytes], overflow_or_bad)。"""
    blobs: list = []
    bad = False
    total = 0

    def _block(b: Any) -> None:
        nonlocal total, bad
        if not isinstance(b, dict):
            return
        t = b.get('type', '')
        if t in ('input_image', 'image', 'image_url'):
            v = b.get('image_url', '')
            if isinstance(v, str) and v.startswith('data:'):
                total += 1
                raw = decode_inline_data_url(v)
                if raw is None:
                    bad = True
                elif len(blobs) < _INLINE_MEDIA_MAX_ITEMS:
                    blobs.append(raw)
        elif t in ('input_file', 'file'):
            fd = b.get('file_data')
            if isinstance(fd, str) and fd.startswith('data:'):
                total += 1
                raw = decode_inline_data_url(fd)
                if raw is None:
                    bad = True
                elif len(blobs) < _INLINE_MEDIA_MAX_ITEMS:
                    blobs.append(raw)
        elif t == 'input_audio':
            au = b.get('input_audio')
            adata = (au.get('data') if isinstance(au, dict) else None) or b.get('data')
            if isinstance(adata, str) and adata.strip():
                bad = True  # 音频不转写：有数据即 fail-closed

    inp = body.get('input') if isinstance(body, dict) else None
    if isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            c = item.get('content')
            if isinstance(c, list):
                for b in c:
                    _block(b)
            else:
                _block(item)
    if total > _INLINE_MEDIA_MAX_ITEMS:
        bad = True
    return blobs, bad
