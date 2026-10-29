"""L1 消融实验：逐项关闭 L1 组件，观察 recall/FPR 变化

组件（全部真实存在于 inspection.py）：
  正则 PATTERNS / 校验位 luhn_valid+idcard_valid / 关键词 KEYWORD_WEIGHTS /
  暗语规则 RISK_RULES / PII 加权 PII_WEIGHTS

用法: .venv/bin/python scripts/ablate_l1.py
输出: docs/thesis/ablation-l1.json
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import gateway.inspection as I  # noqa: E402

DATA = os.path.join(ROOT, "tests", "fixtures", "pilot_dataset.jsonl")
OUT = os.path.join(ROOT, "docs", "thesis", "ablation-l1.json")
THRESH = 60


def score(rows):
    tp = fn = tn = fp = 0
    for r in rows:
        hit = I.inspect_text(r["text"])["risk_score"] >= THRESH
        if r["expect"] == "confidential":
            tp += hit
            fn += (not hit)
        else:
            tn += (not hit)
            fp += hit
    return {"recall": round(tp / max(1, tp + fn), 4),
            "fpr": round(fp / max(1, fp + tn), 4),
            "tp": tp, "fn": fn, "tn": tn, "fp": fp}


def run(rows, name, patch):
    saved = {k: getattr(I, k) for k in patch}
    try:
        for k, v in patch.items():
            setattr(I, k, v)
        return name, score(rows)
    finally:
        for k, v in saved.items():
            setattr(I, k, v)


def main() -> None:
    rows = [json.loads(l) for l in open(DATA, encoding="utf-8") if l.strip()]
    configs = [
        ("Full", {}),
        ("-regular", {"PATTERNS": {}}),
        ("-checksum", {"luhn_valid": lambda s: True, "idcard_valid": lambda s: True}),
        ("-keyword", {"KEYWORD_WEIGHTS": {}}),
        ("-darkrule", {"RISK_RULES": {}}),
        ("-piiweight", {"PII_WEIGHTS": {}}),
    ]
    out = {}
    for name, patch in configs:
        n, m = run(rows, name, patch)
        out[n] = m
        print(f"{n:12s} recall={m['recall']:.1%}  FPR={m['fpr']:.1%}  "
              f"(TP {m['tp']} FN {m['fn']} TN {m['tn']} FP {m['fp']})")

    base = out["Full"]
    print("\n相对 Full 的变化：")
    for n, m in out.items():
        if n == "Full":
            continue
        print(f"  {n:12s} ΔRecall={100*(m['recall']-base['recall']):+.1f}pp  "
              f"ΔFPR={100*(m['fpr']-base['fpr']):+.1f}pp")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"n": len(rows), "threshold": THRESH, "results": out},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"写入 {OUT}")


if __name__ == "__main__":
    main()
