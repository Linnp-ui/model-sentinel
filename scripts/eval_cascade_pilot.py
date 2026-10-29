"""在 pilot 集上对比 L1 / L2 / 级联（口径对齐当前网关：灰区 risk>=20 无上界）

L2 用 laya（8003，非自回归判定）作为小模型代表；生产主判 qwen 见 l2_check.py。
用法: .venv/bin/python scripts/eval_cascade_pilot.py
输出: docs/thesis/cascade-baseline.json
"""
import json
import os
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from gateway.inspection import inspect_text  # noqa: E402
import laya_eval_common as L2  # noqa: E402

DATA = os.path.join(ROOT, "tests", "fixtures", "pilot_dataset.jsonl")
OUT = os.path.join(ROOT, "docs", "thesis", "cascade-baseline.json")
GRAY_FLOOR = 20


def metrics(rows, pred, lat):
    tp = fn = tn = fp = 0
    for r, p in zip(rows, pred):
        if r["expect"] == "confidential":
            tp += p
            fn += (not p)
        else:
            tn += (not p)
            fp += p
    s = sorted(lat)
    return {
        "recall": round(tp / max(1, tp + fn), 4),
        "fpr": round(fp / max(1, fp + tn), 4),
        "precision": round(tp / max(1, tp + fp), 4),
        "tp": tp, "fn": fn, "tn": tn, "fp": fp,
        "latency_ms": {"mean": round(statistics.mean(lat), 2),
                       "p50": round(s[len(s) // 2], 2),
                       "p95": round(s[int(len(s) * 0.95)], 2)},
    }


def main() -> None:
    rows = [json.loads(l) for l in open(DATA, encoding="utf-8") if l.strip()]
    risks, l1_lat, l2_p, l2_lat = [], [], [], []
    for r in rows:
        t0 = time.perf_counter()
        risk = inspect_text(r["text"]).get("risk_score", 0)
        l1_lat.append((time.perf_counter() - t0) * 1000)
        risks.append(risk)
        try:
            t0 = time.perf_counter()
            p = L2.classify_p(r["text"], timeout=30)
            l2_lat.append((time.perf_counter() - t0) * 1000)
        except Exception:
            p = 0.0
            l2_lat.append(0.0)
        l2_p.append(p)

    a = [risk >= 60 for risk in risks]
    b = [p >= L2.THRESH for p in l2_p]

    c, c_lat = [], []
    sticky = {}
    for r, risk, p, l1_ms, l2_ms in zip(rows, risks, l2_p, l1_lat, l2_lat):
        sid = r.get("session_id")
        if sid and sticky.get(sid):
            c.append(True)          # 会话粘性：短路，不再送 L2
            c_lat.append(l1_ms)
            continue
        hit = risk >= 60 or (risk >= GRAY_FLOOR and p >= L2.THRESH)
        c.append(hit)
        c_lat.append(l1_ms + (l2_ms if risk >= GRAY_FLOOR else 0))
        if sid and hit:
            sticky[sid] = True

    items = []
    for r, risk, p, a1, b1, c1 in zip(rows, risks, l2_p, a, b, c):
        items.append({"id": r["id"], "expect": r["expect"], "needs": r.get("needs"),
                      "risk": risk, "l2_p": round(p, 4),
                      "A": a1, "B": b1, "C": c1})
    res = {
        "n": len(rows),
        "items": items,
        "l2_backend": "laya 8003",
        "gray_floor": GRAY_FLOOR,
        "A_l1_only": metrics(rows, a, l1_lat),
        "B_l2_only": metrics(rows, b, l2_lat),
        "C_cascade": metrics(rows, c, c_lat),
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(res, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    for k in ("A_l1_only", "B_l2_only", "C_cascade"):
        m = res[k]
        print(f"{k:12s} recall={m['recall']:.1%}  FPR={m['fpr']:.1%}  "
              f"precision={m['precision']:.1%}  P95={m['latency_ms']['p95']}ms")
    print(f"写入 {OUT}")


if __name__ == "__main__":
    main()
