"""Presidio 基线评测（对照本系统 L1/L2）

在隔离环境运行，勿装进生产 .venv：
  python -m venv /tmp/opencode/presidio-venv
  /tmp/opencode/presidio-venv/bin/pip install presidio-analyzer \
      https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl

用法:
  /tmp/opencode/presidio-venv/bin/python scripts/eval_presidio.py --mode default
  /tmp/opencode/presidio-venv/bin/python scripts/eval_presidio.py --mode cn      # + 中文 ID/手机 规则
输出: 控制台指标 + docs/thesis/presidio-baseline.json
"""
import argparse
import json
import os
import statistics
import time

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "tests", "fixtures", "pilot_dataset.jsonl")
OUT_DIR = os.path.join(ROOT, "docs", "thesis")
# 只把 PII 实体视为"判密"，排除 DATE_TIME/URL 等噪声实体（否则误报失真）
PII_ENTITIES = {
    "CREDIT_CARD", "EMAIL_ADDRESS", "PHONE_NUMBER", "CN_PHONE", "CN_ID",
    "IBAN_CODE", "IP_ADDRESS", "US_SSN", "US_PASSPORT", "US_DRIVER_LICENSE",
    "MEDICAL_LICENSE", "CRYPTO", "PERSON", "NRP",
}


def build(mode: str) -> AnalyzerEngine:
    prov = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
    })
    eng = AnalyzerEngine(nlp_engine=prov.create_engine(),
                         supported_languages=["en"], default_score_threshold=0.5)
    if mode == "cn":
        eng.registry.add_recognizer(PatternRecognizer(
            supported_entity="CN_ID",
            patterns=[Pattern("cn_id18", r"\d{17}[\dXx]", 0.6)]))
        eng.registry.add_recognizer(PatternRecognizer(
            supported_entity="CN_PHONE",
            patterns=[Pattern("cn_phone", r"(?<!\d)1[3-9]\d{9}(?!\d)", 0.6)]))
    return eng


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["default", "cn"], default="default")
    ap.add_argument("--data", default=DATA)
    args = ap.parse_args()

    eng = build(args.mode)
    rows = [json.loads(l) for l in open(args.data, encoding="utf-8") if l.strip()]

    cat, lat = {}, []
    for r in rows:
        t0 = time.perf_counter()
        res = eng.analyze(text=r["text"], language="en")
        lat.append((time.perf_counter() - t0) * 1000)
        hit = any(x.entity_type in PII_ENTITIES and x.score >= 0.5 for x in res)
        c = cat.setdefault(r["category"], [0, 0, 0, 0])
        if r["expect"] == "confidential":
            c[0] += hit
            c[1] += 1
        else:
            c[2] += (not hit)
            c[3] += 1

    tp = sum(c[0] for c in cat.values())
    fn = sum(c[1] - c[0] for c in cat.values())
    tn = sum(c[2] for c in cat.values())
    fp = sum(c[3] - c[2] for c in cat.values())
    recall = tp / max(1, tp + fn)
    fpr = fp / max(1, fp + tn)

    def pct(n):
        return round(100 * n / max(1, len(lat)), 1)

    lat_sorted = sorted(lat)
    summary = {
        "mode": args.mode,
        "n": len(rows),
        "confidential": tp + fn,
        "pass": tn + fp,
        "tp": tp, "fn": fn, "tn": tn, "fp": fp,
        "recall": round(recall, 4),
        "fpr": round(fpr, 4),
        "precision": round(tp / max(1, tp + fp), 4),
        "latency_ms": {
            "mean": round(statistics.mean(lat), 2),
            "p50": round(lat_sorted[len(lat_sorted) // 2], 2),
            "p95": round(lat_sorted[int(len(lat_sorted) * 0.95)], 2),
        },
        "by_category": {k: {"hit": v[0], "conf": v[1],
                            "clean": v[2], "pass": v[3]} for k, v in sorted(cat.items())},
    }
    out = os.path.join(OUT_DIR, f"presidio-baseline-{args.mode}.json")
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(summary, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"Presidio[{args.mode}] n={len(rows)}  机密={tp+fn} 放行={tn+fp}")
    print(f"  recall={recall:.1%}  FPR={fpr:.1%}  precision={tp/max(1,tp+fp):.1%}")
    print(f"  延迟 mean={summary['latency_ms']['mean']}ms  "
          f"p50={summary['latency_ms']['p50']}ms  p95={summary['latency_ms']['p95']}ms")
    for k, v in sorted(cat.items()):
        print(f"  {k:14s} 命中 {v[0]}/{v[1]}  放行干净 {v[2]}/{v[3]}")
    print(f"写入 {out}")


if __name__ == "__main__":
    main()
