from __future__ import annotations
import re
import math
import base64
import binascii
import unicodedata
from typing import Dict, List, Tuple

PATTERNS = {
    "idcard": re.compile(r"\d{17}[\dXx]"),
    "phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),  # 大陆手机号 11 位（分隔符已在 digit 层剥离）
    "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA )?PRIVATE KEY-----"),
    "bankcard": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
    "gh_token": re.compile(r"ghp_[A-Za-z0-9]{36,}"),
    "ant_api_key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "deepseek_key": re.compile(r"sk-[0-9a-fA-F]{32}"),  # 2026-10-19 红队：sk-32hex 曾只靠 policy 原文裸正则，零宽化即绕过
}

# PII severity weights - higher means more sensitive
PII_WEIGHTS = {
    "private_key": 100,   # 私钥/证书 - 最高
    "aws_key": 90,        # 云访问密钥
    "ant_api_key": 90,    # Anthropic/类 API
    "gh_token": 85,       # GitHub PAT
    "deepseek_key": 90,   # DeepSeek/类 sk- 密钥（2026-10-19 红队补：此前无 PII 权重，零宽化绕过 policy 裸正则后直出外网）
    "idcard": 70,         # 身份证 - L3
    "phone": 60,          # 手机号单命中即转本地（与银行卡同档；2026-09-20 补：此前无此 pattern，裸号曾原文出境）
    "bankcard": 60,       # 银行卡 (Luhn valid)
}

KEYWORD_WEIGHTS = {
    # 30档：强机密标记（两命中即 ≥60 转本地）
    "机密": 30, "保密": 30, "绝密": 30, "不得外传": 30,
    # 25档：图纸/身份/资金凭证类
    "图纸": 25, "施工图": 25, "身份证": 25, "标书": 25, "银行流水": 25, "内部资料": 25,
    # 20档：财薪/合同/投标/人事类（单命中即进灰区强制 L2；与 AI_GATEWAY_L2_GRAY_FLOOR=20 对齐）
    "工资": 20, "薪资": 20, "奖金": 20, "财务": 20, "营收": 20, "利润": 20,
    "成本": 20, "预算": 20, "决算": 20, "报价": 20, "单价": 20, "总价": 20,
    "合同": 20, "投标": 20, "中标": 20, "底价": 20, "税率": 20,
    "银行卡": 20, "密码": 20, "手机号": 20, "住址": 20, "图号": 20,
    "人事档案": 20, "工艺参数": 20, "会议纪要": 20,
    # 2026-09-23 补（160 条安全矩阵漏网分析）：薪酬口语/英文财薪/分红/图纸口语/密钥残缺/
    # 电话/通讯录指代。英文词仅小写形态（匹配为原文子串，不 lower）；全 20 档只进灰不直拦。
    "涨薪": 20, "年终": 20, "绩效": 20, "分红": 20,
    "salary": 20, "bonus": 20, "revenue": 20, "package": 20, "offer": 20, "headcount": 20,
    "配筋": 20, "开挖": 20, "切削": 20, "HRB": 20,
    "通讯录": 20, "花名册": 20, "电话": 20, "别外传": 20,
    "密钥": 20, "token": 20, "password": 20, "AKIA": 20,
}

# 2026-10-08 补（160 条矩阵 7 条 risk=0 漏网分析）：单关键词打不中的"口语化/暗语"
# 形状。每条规则一个 20 档 = 恰好落进灰区下界，交给 L2 判密，网关门逻辑不变。
# 计数按规则去重（同名规则多次命中只算一次）。
RISK_RULES = {
    # 暗语金额指代："那个数你懂的,38后面加个k" / "图上那个数,C后面35" / "会上定的那个月数,2加13"
    "implied_amount": re.compile(
        r"(?:那个|这份|那份|这个)[^,，。;；]{0,4}(?:数|金额|工资|月薪|薪资)"
        r"|[A-Za-z0-9]{1,4}\s*后面\s*[加接][^,，。;；]{0,3}[A-Za-z0-9kK万千]"
    ),
    # 口语金额："涨到38000了" / "加到50k"（要求 4+ 位数，避免命中"上涨5%"这类公开报道）
    "raise_amount": re.compile(r"(?:涨到|涨至|加到|涨了|拿到|到手)[^,，。;；]{0,6}\d{4,}"),
    # 拼音/拆字薪资："我 g 资 daoshou 12k"
    "salary_pinyin": re.compile(
        r"(?:到手|daoshou|gongzi|xinzi|gong\s?zi|xin\s?zi)\s*[:：]?\s*[\d一二三四五六七八九十]"
    ),
    # 高管行程："老板周三下午3点见红杉的人,行程没变吧"（必须与职务同现，避开"行程单私发我"）
    # 间隔允许跨逗号，故用 [\s\S] 而非排除标点的字符类
    "exec_schedule": re.compile(
        r"(?:老板|领导|高管|总裁|总经理|副总)[\s\S]{0,24}(?:行程|日程|在哪|几点|见谁)"
        r"|(?:行程|日程)[\s\S]{0,10}(?:老板|领导|高管|总裁|总经理|副总)"
    ),
}

def luhn_valid(card: str) -> bool:
    s = 0
    alt = False
    for ch in reversed(card):
        if not ch.isdigit():
            continue
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        s += d
        alt = not alt
    return s % 10 == 0

_IDCARD_W = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_IDCARD_CK = "10X98765432"
_IDCARD_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def _idcard_date_ok(s: str) -> bool:
    """Positions 6..14 (YYYYMMDD of birth) must form a real calendar date."""
    y, mo, d = int(s[6:10]), int(s[10:12]), int(s[12:14])
    if not (1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
        return False
    dim = _IDCARD_DAYS[mo - 1]
    if mo == 2 and (y % 4 == 0 and y % 100 != 0 or y % 400 == 0):
        dim = 29
    return d <= dim


def idcard_valid(card: str) -> bool:
    """Mainland-China 18-digit ID: real birth date + GB 11643 mod 11-2 checksum.

    A bare 17-digit-plus-check-char match is NOT enough: random 18-digit runs
    (millisecond timestamps / order ids / encoded blobs) almost never carry both
    a calendar-valid birth date and a correct check digit, so requiring both
    removes the 「隐私加权转内网」false positive that mis-routed benign
    Codex/tool payloads to the local model -- WITHOUT digit-boundaries (which
    would create false negatives once separators are stripped). Genuine IDs, even
    ones split by separators / zero-width (reassembled before this check), still pass.
    """
    s = (card or "").strip().upper()
    if len(s) != 18 or not s[:17].isdigit() or s[17] not in "0123456789X":
        return False
    if not _idcard_date_ok(s):
        return False
    total = sum(int(s[i]) * _IDCARD_W[i] for i in range(17))
    return _IDCARD_CK[total % 11] == s[17]


# --- P0-1/P0-2 fix: normalize text before PII regex matching ---
# Two pre-processing passes:
#   1) NFKC: full-width digits (\uff11-\uff19) -> ASCII, ligatures, etc.
#   2) Zero-width chars + invisible formatting (U+200B-200F, U+202A-202E, U+FEFF, U+2060-2064) -> removed
# Original text is preserved for keyword matching (Chinese keywords match raw bytes).

_INVISIBLE_RE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5]"
)


def _normalize_for_pii(text: str) -> str:
    """Return a PII-friendly copy: NFKC + strip invisible/formatting chars.
    Used for regex matching of PII patterns (idcard/phone/email/AWS key etc.),
    and as the base of the keyword compact copy (zero-width/full-width/space-split
    all resolve after this pass). policy.py 的 text 视图也复用本函数。
    """
    if not text:
        return ""
    n = unicodedata.normalize("NFKC", text)
    n = _INVISIBLE_RE.sub("", n)
    return n


# PII patterns are too lax against common PII-separator insertion:
# "110101-1990-0101-1234" / "110101 1990 0101 1234" should still match.
# Strip the most common PII separators (space/hyphen/dot/underscore/slash/comma/pipe/fullwidth)
# from the normalized copy before running idcard/phone regex. Other PII (email, keys) keep raw.
_PII_SEP_RE = re.compile(r"[\s\u3000\-_.,/|]+")


def _strip_pii_separators(text: str) -> str:
    """Collapse common separators so a split idcard/phone becomes continuous digits.
    Only for digit-cluster patterns (idcard/bankcard). Applied AFTER NFKC+invisible strip.
    """
    return _PII_SEP_RE.sub("", text)


# 关键词专用紧凑副本：NFKC+零宽剥离之后的 pii_text 再去掉全部空白（含 NFKC 归一后的
# 全角空格）。2026-10-19 红队：「工\u200b资」「ｓａｌａｒｙ」「财 务 报 表」原文匹配全 miss
# ⇒ risk 0 ⇒ 不进灰区直出外网。跨词边界拼接的误报只会多进灰区（误报可接受，漏报不可）。
_KW_WS_RE = re.compile(r"\s+")


# --- Red-team corpus fix (2026-09-08): encoding-obfuscated PII ---
# P0-1 closed *unicode* tricks (NFKC/separators/zero-width). Base64/hex-encoded
# PII sailed through: NFKC cannot decode it and the L2 small model sees gibberish.
# Sniff candidate blobs, decode, re-run the SAME PII patterns on the decoded layer.
# Guards: min length, strict utf-8, blob-count cap. Decoded hits tagged via=base64/hex.
# Known overlap: a digit-only hex blob ALSO matches the raw idcard regex
# (hex("110101..") is 36 digits) -> may double-count; harmless for >=1/action asserts.
_B64_RUN_RE = re.compile(r"[A-Za-z0-9+/=\s]{24,}")
_HEX_RUN_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32,}(?![0-9a-fA-F])")
_DECODE_MAX_BLOBS = 8
# F1(2026-10-14)：原始 break 条件是 `len(layers) >= 8`——只要候选**全部解码失败**
# （等长非 4 倍数 base64 串 / 奇数长 hex 串），layers 永不增长、break 永不触发，
# finditer 遍历全部分-match 且每次进 Python 层 re.sub/fullmatch/b64decode：
# 512KB 对抗文本 ≈ 2 万次 × ~10µs ≈ 200ms，5MB ≈ 2s，全在事件循环。改成
# 「尝试次数」硬顶，与成功与否解耦；良性请求候选数远小于 256。
_DECODE_MAX_ATTEMPTS = 256
_DECODE_MIN_B64_LEN = 16  # real guard is utf-8+PII match (phone removed)
_DECODE_MIN_HEX_LEN = 20
# 每 pattern 每层最多处理的 match 数：bankcard/idcard 每个候选都要 luhn_valid/
# idcard_valid Python 校验，18 位数字串重复排列可产出百万候选（PII 权重在
# 60 即满，权重新增不改变判决）⇒ 良性远小于 256，对抗线性可算。
_PATTERN_MATCH_CAP = 256


def _try_b64_decode(blob: str):
    s = re.sub(r"\s+", "", blob)
    if len(s) < _DECODE_MIN_B64_LEN or len(s) % 4 != 0:
        return None
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", s):
        return None
    try:
        raw = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _try_hex_decode(blob: str):
    s = blob.strip()
    if len(s) < _DECODE_MIN_HEX_LEN or len(s) % 2 != 0:
        return None
    try:
        raw = bytes.fromhex(s)
    except ValueError:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _b64_candidates(run: str):
    """Split a b64/whitespace run into decode candidates."""
    # Whole-run despace covers multiline splits ("MTEx\nMDAx"); per-token
    # covers English-merged runs ("is MTEx... check" -> tokens).
    cands = []
    despace = re.sub(r"\s+", "", run)
    if despace:
        cands.append(despace)
    for tok in re.split(r"\s+", run):
        stripped = re.sub(r"^[^A-Za-z0-9+/]+|[^A-Za-z0-9+/=]+$", "", tok)
        if stripped and stripped != despace:
            cands.append(stripped)
    return cands


def _decode_layers(pii_text: str):
    """Return [(decoded_text, via)] for embedded base64/hex blobs."""
    layers = []
    if not pii_text:
        return layers
    attempts = 0
    for m in _B64_RUN_RE.finditer(pii_text):
        if len(layers) >= _DECODE_MAX_BLOBS or attempts >= _DECODE_MAX_ATTEMPTS:
            break
        for cand in _b64_candidates(m.group(0)):
            if len(layers) >= _DECODE_MAX_BLOBS or attempts >= _DECODE_MAX_ATTEMPTS:
                break
            attempts += 1
            dec = _try_b64_decode(cand)
            if dec is not None and all(dec != l for l, _ in layers):
                layers.append((dec, "base64"))
    if len(layers) < _DECODE_MAX_BLOBS:
        for m in _HEX_RUN_RE.finditer(pii_text):
            if len(layers) >= _DECODE_MAX_BLOBS or attempts >= _DECODE_MAX_ATTEMPTS:
                break
            attempts += 1
            dec = _try_hex_decode(m.group(0))
            if dec is not None and all(dec != l for l, _ in layers):
                layers.append((dec, "hex"))
    return layers


def inspect_text(text: str) -> Dict:
    findings: List[Dict] = []
    pii_count = 0
    risk_score = 0
    weighted_count = 0
    by_type: Dict[str, int] = {}
    # P0-1/P0-2: use normalized+separator-stripped copy for digit-cluster PII.
    # Other patterns (email/keys) match against the NFKC-normalized copy.
    pii_text = _normalize_for_pii(text or "")
    digit_text = _strip_pii_separators(pii_text)
    def _emit(name, val, start, via):
        nonlocal pii_count, weighted_count
        w = PII_WEIGHTS.get(name, 0)
        findings.append({
            "type": name,
            "value_preview": val[:6] + "***",
            "start": start,
            "end": start + len(val),
            "weight": w,
            "via": via,
        })
        pii_count += 1
        weighted_count += w
        by_type[name] = by_type.get(name, 0) + 1

    def _scan_layer(layer_text, via):
        layer_digits = _strip_pii_separators(layer_text)
        seen = set()
        for name, pat in PATTERNS.items():
            # 去分隔符副本抓「空格/横线拆开」的号码；原文副本抓「与相邻数字列粘连」的号码
            # （旧实现只扫去分隔符副本：表格里行号+身份证会并成 19 位串，校验位失败漏报）
            texts = ((layer_text, layer_digits) if name in ("idcard", "bankcard", "phone")
                     else (layer_text,))
            n = 0
            for match_text in texts:
                for m in pat.finditer(match_text):
                    if n >= _PATTERN_MATCH_CAP:
                        break
                    val = m.group(0)
                    key = (name, val)
                    if key in seen:
                        continue
                    if name == "bankcard" and not luhn_valid(val):
                        continue
                    if name == "idcard" and not idcard_valid(val):
                        continue
                    n += 1
                    seen.add(key)
                    _emit(name, val, m.start(), via)

    _scan_layer(pii_text, "raw")
    decoded = _decode_layers(pii_text)
    for dec_text, via in decoded:
        _scan_layer(dec_text, via)

    keyword_hits: List[str] = []
    encoded_kw_hits: List[str] = []   # 只在解码层出现、原文不可见的命中
    keyword_score = 0
    # 2026-10-19 红队：关键词层此前只跑原文，零宽/全角/空格拆分关键词（工\u200b资、
    # ｓａｌａｒｙ、财 务 报 表）risk 归 0 不进灰区直出外网。补紧凑副本（NFKC+零宽剥离+
    # 去全部空白）双跑；原文保留作兜底（NFKC 对 CJK 是 no-op，ASCII 全角→半角只增命中）。
    # 2026-10-19 M2：解码层（base64/hex）此前只重扫 PII，整段无 PII 的机密文本编码后
    # risk 归 0 直出外网 —— 关键词/RISK_RULES 对每个解码层同样双跑，同名跨层去重；
    # 解码层独有的命中单列 encoded_keyword_hits（L2 看编码串判不了，由调用侧定夺）。
    def _scan_keywords(layer_text: str, encoded: bool = False) -> None:
        nonlocal keyword_score

        def _hit(name: str, w: int) -> None:
            nonlocal keyword_score
            if name in keyword_hits:
                return
            keyword_hits.append(name)
            keyword_score += w
            if encoded:
                encoded_kw_hits.append(name)

        norm = _normalize_for_pii(layer_text or "")
        compact = _KW_WS_RE.sub("", norm)
        for kw, w in KEYWORD_WEIGHTS.items():
            if kw in layer_text or kw in compact:
                _hit(kw, w)
        # 规则跑归一化副本（零宽/全角绕过）+ 紧凑副本（空格拆分），与关键词层同一口径
        for name, pat in RISK_RULES.items():
            if pat.search(norm) or pat.search(compact):
                _hit(name, 20)

    _scan_keywords(text or "")
    for dec_text, _via in decoded:
        _scan_keywords(dec_text, encoded=True)

    risk_score = min(200, weighted_count + keyword_score)

    return {
        "findings": findings,
        "pii_count": pii_count,
        "weighted_pii_score": weighted_count,
        "keyword_score": keyword_score,
        "keyword_hits": keyword_hits,
        "encoded_keyword_hits": encoded_kw_hits,
        "by_type": by_type,
        "risk_score": risk_score,
    }
