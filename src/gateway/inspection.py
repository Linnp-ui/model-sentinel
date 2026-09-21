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
}

# PII severity weights - higher means more sensitive
PII_WEIGHTS = {
    "private_key": 100,   # 私钥/证书 - 最高
    "aws_key": 90,        # 云访问密钥
    "ant_api_key": 90,    # Anthropic/类 API
    "gh_token": 85,       # GitHub PAT
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
}

RISK_THRESHOLD_ROUTE_LOCAL = 60  # >=60 -> route_local
RISK_THRESHOLD_BLOCK = 150        # >=150 -> block (override to block)


def shannon_entropy(s: str) -> float:
    if not s:
        return 0
    from collections import Counter
    c = Counter(s)
    l = len(s)
    return -sum((v/l) * math.log2(v/l) for v in c.values())


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
    removes the pii_weighted_route_local false positive that mis-routed benign
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
    Used only for regex matching of PII patterns (idcard/phone/email/AWS key etc.).
    Keyword scan still runs against the original text.
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
_DECODE_MIN_B64_LEN = 16  # real guard is utf-8+PII match (phone removed)
_DECODE_MIN_HEX_LEN = 20


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
    for m in _B64_RUN_RE.finditer(pii_text):
        if len(layers) >= _DECODE_MAX_BLOBS:
            break
        for cand in _b64_candidates(m.group(0)):
            if len(layers) >= _DECODE_MAX_BLOBS:
                break
            dec = _try_b64_decode(cand)
            if dec is not None and all(dec != l for l, _ in layers):
                layers.append((dec, "base64"))
    if len(layers) < _DECODE_MAX_BLOBS:
        for m in _HEX_RUN_RE.finditer(pii_text):
            if len(layers) >= _DECODE_MAX_BLOBS:
                break
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
        entropy = shannon_entropy(val) if name in ("aws_key", "ant_api_key", "gh_token") else 0
        w = PII_WEIGHTS.get(name, 0)
        findings.append({
            "type": name,
            "value_preview": val[:6] + "***",
            "start": start,
            "end": start + len(val),
            "entropy": round(entropy, 2),
            "severity": "L3" if w >= 70 else ("L2" if w >= 40 else "L1"),
            "weight": w,
            "via": via,
        })
        pii_count += 1
        weighted_count += w
        by_type[name] = by_type.get(name, 0) + 1

    def _scan_layer(layer_text, via):
        layer_digits = _strip_pii_separators(layer_text)
        for name, pat in PATTERNS.items():
            match_text = layer_digits if name in ("idcard", "bankcard", "phone") else layer_text
            if name == "email" and "@" not in match_text:
                continue  # P0: 无 @ 不可能命中，跳过回溯风险
            for m in pat.finditer(match_text):
                val = m.group(0)
                if name == "bankcard" and not luhn_valid(val):
                    continue
                if name == "idcard" and not idcard_valid(val):
                    continue
                _emit(name, val, m.start(), via)

    _scan_layer(pii_text, "raw")
    for dec_text, via in _decode_layers(pii_text):
        _scan_layer(dec_text, via)

    keyword_hits: List[str] = []
    keyword_score = 0
    for kw, w in KEYWORD_WEIGHTS.items():
        if kw in (text or ""):
            keyword_hits.append(kw)
            keyword_score += w

    risk_score = min(200, weighted_count + keyword_score)

    severity = "LOW"
    if risk_score >= RISK_THRESHOLD_BLOCK:
        severity = "CRITICAL"
    elif risk_score >= RISK_THRESHOLD_ROUTE_LOCAL:
        severity = "HIGH"
    elif risk_score >= 30:
        severity = "MEDIUM"

    return {
        "findings": findings,
        "pii_count": pii_count,
        "weighted_pii_score": weighted_count,
        "keyword_score": keyword_score,
        "keyword_hits": keyword_hits,
        "by_type": by_type,
        "risk_score": risk_score,
        "severity": severity,
    }
