"""L2 判定阈值扫描（论文 表 6-12：0.7 选型依据）。

纯 CPU 重算，不跑模型：读现役 v34 的逐条 p 值
（docs/thesis/eval/l2-candidate-laya-v34-referee.json，2026-11-05 实跑落盘），
在 T ∈ 0.5–0.9 上重算 仅L2 / 级联（含会话粘性，口径同 pilot_metrics）/
real_eval acc。干净 161 口径的 23 条剔除按 build_laya_dataset
check_train_pilot_overlap 同规则现算（双向包含、最短 8 字）。

用法: .venv/bin/python scripts/sweep_l2_threshold.py
输出: docs/thesis/eval/l2-threshold-sweep.json + stdout Markdown 表
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import l2_candidate_common as C  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(ROOT, "docs/thesis/eval", "l2-candidate-laya-v34-referee.json")
TRAIN = os.path.expanduser("~/models/laya-dataset/train.jsonl")
TS = [0.5, 0.6, 0.7, 0.8, 0.9]


def overlap_ids():
    """同 check_train_pilot_overlap：pilot 与训练集文字双向包含（最短 8 字）。"""
    tset = [json.loads(l)["text"].strip()
            for l in open(TRAIN, encoding="utf-8") if l.strip()]
    out = set()
    for l in open(os.path.join(ROOT, "tests/fixtures/pilot_dataset.jsonl"),
                  encoding="utf-8"):
        if not l.strip():
            continue
        r = json.loads(l)
        pt = r["text"].strip()
        if len(pt) >= 8 and any(pt in t or t in pt for t in tset):
            out.add(r["id"])
    return out


def run_one(ref, rows, cbase, real, overlap, T):
    C.THRESH = T
    p_pilot = ref["pilot_per_item"]
    l2only, casc = C.pilot_metrics(rows, cbase, p_pilot)
    clean_ids = {r["id"] for r in rows} - overlap
    clean_rows = [r for r in rows if r["id"] in clean_ids]
    l2only_c, casc_c = C.pilot_metrics(clean_rows, cbase, p_pilot)
    real_m = C.count_cases(ref["real_eval"]["p"], real)
    return {
        "T": T,
        "clean161_l2_only": l2only_c, "clean161_cascade": casc_c,
        "full184_l2_only": l2only, "full184_cascade": casc,
        "real_eval": {k: real_m[k] for k in ("n", "acc7", "tp", "fp", "fn", "tn")},
    }


def main():
    ref = json.load(open(REF, encoding="utf-8"))
    rows, cbase = C.load_pilot()
    real = C.load_real_eval()
    overlap = overlap_ids()
    assert len(overlap) == 23, f"干净 161 剔除数应为 23，实得 {len(overlap)}（训练集或 pilot 变了？）"
    out = {"source": os.path.relpath(REF, ROOT), "thresholds": TS,
           "overlap_ids": sorted(overlap), "sweep": []}
    for T in TS:
        m = run_one(ref, rows, cbase, real, overlap, T)
        out["sweep"].append(m)
        print(f"T={T:.1f}  clean161 仅L2 {m['clean161_l2_only']['recall']:.1%}/"
              f"{m['clean161_l2_only']['fpr']:.1%}  "
              f"级联 {m['clean161_cascade']['recall']:.1%}/"
              f"{m['clean161_cascade']['fpr']:.1%}  "
              f"real_eval acc {m['real_eval']['acc7']:.1%} "
              f"(全184 仅L2 {m['full184_l2_only']['recall']:.1%}/"
              f"{m['full184_l2_only']['fpr']:.1%})")
    dst = os.path.join(ROOT, "docs/thesis/eval", "l2-threshold-sweep.json")
    json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"→ {dst}")


if __name__ == "__main__":
    main()
