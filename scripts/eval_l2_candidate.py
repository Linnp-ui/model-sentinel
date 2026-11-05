"""L2 候选模型选型对比评测（零样本 / 微调后，统一口径）。

用法（同一口径：Q2 双任务 noul 题、render 空字段不渲染、阈值 0.7、级联公式同
eval_laya_version_compare.py）：

  # Jev-Style-2B-Decision-v3（零样本 = 官方 T=0.8279，自动读 readout_config.json；
  # 微调版把拟合温度写回输出目录的 readout_config.json，同样直接跑）
  CUDA_VISIBLE_DEVICES=6 ~/venvs/jevstyle/bin/python scripts/eval_l2_candidate.py \
      --backend jevstyle --model-dir ~/models/jev-style-2b --tag jevstyle-2b-zeroshot

  # laya base multilingual 零样本（生产服务同框：max_len 2048）
  CUDA_VISIBLE_DEVICES=7 ~/venvs/laya/bin/python scripts/eval_l2_candidate.py \
      --backend layabase --model-dir ~/models/laya/multilingual --tag laya-base-zeroshot

输出: docs/thesis/eval/l2-candidate-<tag>.json
延迟为进程内单流口径（不含 HTTP 开销），与 cascade-baseline.json 的服务端口径不同源，
只作相对比较。
"""
import argparse
import datetime
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from l2_candidate_common import (Q2, ROOT, conf_pr, count_cases, load_pilot,  # noqa: E402
                                 load_probes, load_real_eval, pilot_metrics,
                                 render_state)

THRESH = 0.7


def _q_jev(q):
    """服务端 QUESTIONS 的 'type' 键 → Jev-Style 运行时的 't' 键。"""
    return {"t": q["type"], "ins": q["instructions"], "crit": q["criteria"]}


def predict_jevstyle(model_dir):
    sys.path.insert(0, model_dir)
    from jev_style_decision import JevStyleDecision
    eng = JevStyleDecision(model_dir, device="cuda", dtype="float32")
    qs = [_q_jev(Q2["is_confidential"]), _q_jev(Q2["is_injection"])]
    def predict(state):
        return float(eng.score_many(state, qs)[0]["probabilities"]["true"])
    return predict


def predict_layabase(model_dir):
    import laya
    a = laya.load(model_dir)
    a.cfg["max_len"] = 2048  # 生产服务同框（1200 字中文预览 > 默认 state 预算）
    def predict(state):
        return float(a.predict(state, Q2)["answers"]["is_confidential"]["noul"])
    return predict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", required=True, choices=["jevstyle", "layabase"])
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--note", default="")
    args = ap.parse_args()
    model_dir = os.path.expanduser(args.model_dir)

    t0 = time.time()
    predict = (predict_jevstyle if args.backend == "jevstyle" else predict_layabase)(model_dir)
    load_s = time.time() - t0

    rows, cbase = load_pilot()
    real = load_real_eval()
    probes = load_probes()

    p_real, lat = {}, []
    for name, state, _w in real:
        t0 = time.perf_counter()
        p_real[name] = predict(state)
        lat.append((time.perf_counter() - t0) * 1000)
    p_probe = {name: predict(state) for name, state, _w in probes}
    p_pilot = {}
    for r in rows:
        t0 = time.perf_counter()
        p_pilot[r["id"]] = predict(render_state(r["text"], ""))
        lat.append((time.perf_counter() - t0) * 1000)

    l2only, casc = pilot_metrics(rows, cbase, p_pilot)
    lat_s = sorted(lat)
    out = {
        "date": datetime.date.today().isoformat(),
        "tag": args.tag, "note": args.note,
        "backend": args.backend, "model_dir": model_dir,
        "threshold": THRESH, "load_s": round(load_s, 1),
        "probes": {k: count_cases(p_probe, probes)[k] for k in ("n", "acc7", "tp", "fp", "fn", "tn")},
        "real_eval": count_cases(p_real, real),
        "pilot_l2_only": l2only,
        "pilot_cascade": casc,
        "pilot_per_item": {r["id"]: round(p_pilot[r["id"]], 4) for r in rows},
        "latency_ms_inproc": {"n": len(lat),
                              "mean": round(statistics.mean(lat), 2),
                              "p50": round(lat_s[len(lat_s) // 2], 2),
                              "p95": round(lat_s[int(len(lat_s) * 0.95)], 2)},
    }
    ev = os.path.join(ROOT, "docs/thesis/eval")
    os.makedirs(ev, exist_ok=True)
    OUT = os.path.join(ev, f"l2-candidate-{args.tag}.json")
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    rl, pr = out["real_eval"], out["probes"]
    print(f"{args.tag:28s} 探针 acc@0.7={pr['acc7']:.0%} | real_eval acc@0.7={rl['acc7']:.1%} "
          f"({rl['tp']}T/{rl['fn']}FN/{rl['fp']}FP/{rl['tn']}TN) | "
          f"pilot 仅L2 {l2only['recall']:.1%}/{l2only['fpr']:.1%} | "
          f"级联 {casc['recall']:.1%}/{casc['fpr']:.1%} | "
          f"P95(inproc)={out['latency_ms_inproc']['p95']}ms → {OUT}")


if __name__ == "__main__":
    main()
