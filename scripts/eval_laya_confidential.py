"""评估 Laya 零样本 vs 微调：8 例探针 + 真实留出集（real_eval.jsonl）

用法: CUDA_VISIBLE_DEVICES=6 ~/venvs/laya/bin/python scripts/eval_laya_confidential.py
"""
import json
import os
import sys

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "6")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("USE_TF", "0")

import laya

from laya_eval_common import PROBES, Q, THRESH


def render(text, filename):
    return f"文件名: {filename}\n\n内容预览:\n{text}" if filename else f"内容预览:\n{text}"


def eval_agent(agent, cases, label):
    print(f"\n=== {label} ===")
    correct7, correct5, tp, fp, fn, tn = 0, 0, 0, 0, 0, 0
    for name, state, want in cases:
        res = agent.predict(state, Q)
        p = float(res["answers"]["is_confidential"]["noul"])
        at7 = 1 if p >= THRESH else 0
        at5 = 1 if p >= 0.5 else 0
        correct7 += at7 == want
        correct5 += at5 == want
        if want and at7: tp += 1
        elif not want and at7: fp += 1
        elif want: fn += 1
        else: tn += 1
        mark = "✓" if at7 == want else "✗"
        print(f"  {mark} {name}: p_conf={p:.3f} (0.7判:{'密' if at7 else '非密'})")
    n = len(cases)
    print(f"  acc@0.7={correct7}/{n}  acc@0.5={correct5}/{n}  TP={tp} FP={fp} FN={fn} TN={tn}")
    return correct7 / n, correct5 / n


def _live_ckpt():
    """现役影子 checkpoint：var/laya-l2.env 的 LAYA_MODEL_PATH（promote 写的唯一真源）。"""
    env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "var", "laya-l2.env")
    with open(env, encoding="utf-8") as f:
        for line in f:
            if line.startswith("LAYA_MODEL_PATH="):
                return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("读不到 var/laya-l2.env 的 LAYA_MODEL_PATH，显式传 checkpoint 路径")


def main():
    real = []
    path = os.path.expanduser("~/models/laya-dataset/real_eval.jsonl")
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            real.append((f"{r['source'][:28]}|{r['text'][:20]}", render(r["text"], r.get("filename", "")), r["label"]))
    probe_cases = [(n, render(t, ""), w) for n, t, w in PROBES]
    real_cases = real  # 已渲染

    a0 = laya.load("/home/your-user/models/laya/multilingual")
    a0.cfg["max_len"] = 2048
    a1 = laya.load("/home/your-user/models/laya-finetuned-confidential")
    a1.cfg["max_len"] = 2048
    # 候选 checkpoint：argv[1] 优先，否则读现役影子 var/laya-l2.env（原来硬编码 v4，
    # 每版晋升后都得手改一次，忘了就拿旧版当"微调"基线评）
    ckpt = sys.argv[1] if len(sys.argv) > 1 else _live_ckpt()
    a2 = laya.load(ckpt)
    a2.cfg["max_len"] = 2048
    tag = "微调" + os.path.basename(ckpt).replace("laya-finetuned-confidential", "") or "v?"

    z7, z5 = eval_agent(a0, probe_cases, "零样本 · 8 例探针")
    f7, f5 = eval_agent(a1, probe_cases, "微调v1 · 8 例探针")
    g7, g5 = eval_agent(a2, probe_cases, f"{tag} · 8 例探针")
    zr7, zr5 = eval_agent(a0, real_cases, f"零样本 · 真实留出 {len(real_cases)}")
    fr7, fr5 = eval_agent(a1, real_cases, f"微调v1 · 真实留出 {len(real_cases)}")
    gr7, gr5 = eval_agent(a2, real_cases, f"{tag} · 真实留出 {len(real_cases)}")
    print(f"\n汇总: acc@0.7 探针 {z7:.0%}→{f7:.0%}→{g7:.0%} | 真实 {zr7:.0%}→{fr7:.0%}→{gr7:.0%} ; acc@0.5 真实 {zr5:.0%}→{fr5:.0%}→{gr5:.0%}")


if __name__ == "__main__":
    main()
