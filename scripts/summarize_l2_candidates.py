"""L2 候选模型选型对比汇总（论文表源）：读各评测 JSON，输出 Markdown 表。

行源：
- eval/l2-candidate-*.json（本实验新跑：laya base 零样本 / Jev-Style 零样本与微调）
- laya-version-compare.json["models"]["v34（现役）"]（现役 laya v34 微调）
- qwen-prompt-ab.json["base"]（生产 qwen 零样本，10-29/11-02 同口径复跑）

用法: .venv/bin/python scripts/summarize_l2_candidates.py [更多 l2-candidate-*.json …]
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EV = os.path.join(ROOT, "docs/thesis/eval")


def pct(x):
    return f"{x * 100:.1f}%" if x is not None else "—"


def row(name, model, l2, casc, real=None, probes=None):
    out = [name, model, pct(l2["recall"]), pct(l2["fpr"]), pct(casc["recall"]), pct(casc["fpr"])]
    if real is not None:
        out.append(pct(real["acc7"]) if "acc7" in real else "—")
    if probes is not None:
        out.append(pct(probes))
    return out


def main():
    cand_files = sys.argv[1:] or sorted(glob.glob(os.path.join(EV, "l2-candidate-*.json")))
    rows = []
    for f in cand_files:
        d = json.load(open(f, encoding="utf-8"))
        real = d.get("real_eval")
        probes = d.get("probes", {}).get("acc7")
        real7 = real["acc7"] if real and "acc7" in real else None
        rows.append(row(d["tag"], os.path.basename(d["model_dir"]) or d["tag"],
                        d["pilot_l2_only"], d["pilot_cascade"],
                        {"acc7": real7} if real7 is not None else None, probes))
    vc = json.load(open(os.path.join(ROOT, "docs/thesis/laya-version-compare.json")))
    v34 = vc["models"].get("v34（现役）") or next(iter(vc["models"].values()))
    rows.append(row("laya v34 微调（现役）", "laya-finetuned-confidential-v34",
                    v34["pilot_l2_only"], v34["pilot_cascade"],
                    {"acc7": v34["real_eval"]["acc7"]}, v34["probes"]["acc7"]))
    qa = json.load(open(os.path.join(ROOT, "docs/thesis/qwen-prompt-ab.json")))
    rows.append(row("qwen 4B 零样本（生产初态）", "Qwen3-4B-Instruct-2507",
                    qa["base"]["l2_only"], qa["base"]["cascade"], None, None))

    header = ["方案", "模型", "仅L2 召回", "仅L2 误报", "级联 召回", "级联 误报",
              "real_eval acc@0.7", "探针 acc@0.7"]
    print("| " + " | ".join(header) + " |")
    print("|" + "---|" * len(header))
    for r in rows:
        print("| " + " | ".join(r) + " |")
    print("\n口径：pilot n=184（标签 deny-overrides）；级联 C = A_l1 ∨ (risk≥20 ∧ p≥0.7) ∨ 会话粘性；"
          "real_eval n=103 真实留出（37 密/66 放行）；探针 8 例；阈值 0.7；"
          "题规格=双任务 noul（生产 QUESTIONS 逐字）。")


if __name__ == "__main__":
    main()
