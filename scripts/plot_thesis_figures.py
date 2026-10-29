"""生成第 6 章的三张实验图（数据取自 docs/thesis/*.json，不手填数字）

用法（隔离环境，勿装进生产 .venv）：
  /tmp/opencode/presidio-venv/bin/python scripts/plot_thesis_figures.py
输出：
  docs/thesis/figures/fig6-1-latency.png    图 6-1 各方案 P95 延迟
  docs/thesis/figures/fig6-2-metrics.png    图 6-2 各方案召回/误报
  docs/thesis/figures/fig6-3-ablation.png   图 6-3 L1 消融
"""
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, "docs", "thesis")
OUT = os.path.join(D, "figures")
os.makedirs(OUT, exist_ok=True)

# 中文字体：系统 WenQuanYi Zen Hei
_font = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
if os.path.exists(_font):
    font_manager.fontManager.addfont(_font)
    plt.rcParams["font.sans-serif"] = ["WenQuanYi Zen Hei"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams["figure.dpi"] = 150

CASCADE = json.load(open(os.path.join(D, "cascade-baseline.json"), encoding="utf-8"))
P_DEF = json.load(open(os.path.join(D, "presidio-baseline-default.json"), encoding="utf-8"))
P_CN = json.load(open(os.path.join(D, "presidio-baseline-cn.json"), encoding="utf-8"))
ABL = json.load(open(os.path.join(D, "ablation-l1.json"), encoding="utf-8"))["results"]

A, B, C = CASCADE["A_l1_only"], CASCADE["B_l2_only"], CASCADE["C_cascade"]
METHODS = ["仅 L1", "仅 L2", "级联", "Presidio默认", "Presidio中文"]
RECALL = [A["recall"], B["recall"], C["recall"], P_DEF["recall"], P_CN["recall"]]
FPR = [A["fpr"], B["fpr"], C["fpr"], P_DEF["fpr"], P_CN["fpr"]]
P95 = [A["latency_ms"]["p95"], B["latency_ms"]["p95"], C["latency_ms"]["p95"],
       P_DEF["latency_ms"]["p95"], P_CN["latency_ms"]["p95"]]


def save(fig, name):
    p = os.path.join(OUT, name)
    fig.tight_layout()
    fig.savefig(p, bbox_inches="tight")
    plt.close(fig)
    assert os.path.getsize(p) > 5000, f"图过小，疑似空白: {p}"
    print("saved", p, os.path.getsize(p), "bytes")


def bar_labels(ax, bars, fmt="{:.2f}"):
    for b in bars:
        ax.annotate(fmt.format(b.get_height()),
                    (b.get_x() + b.get_width() / 2, b.get_height()),
                    ha="center", va="bottom", fontsize=8)


# ---- 图 6-1 P95 延迟（对数轴，否则 0.26ms 与 20ms 无法同图可读）
fig, ax = plt.subplots(figsize=(7, 3.6))
bars = ax.bar(METHODS, P95, color=["#4C78A8"] * 3 + ["#9E9E9E"] * 2)
ax.set_yscale("log")
ax.set_ylabel("P95 延迟 (ms，对数轴)")
ax.set_title("各方案 P95 延迟对比（n=184）")
bar_labels(ax, bars, "{:.2f}")
ax.grid(axis="y", alpha=0.3)
save(fig, "fig6-1-latency.png")

# ---- 图 6-2 召回 / 误报
fig, ax = plt.subplots(figsize=(7, 3.6))
x = range(len(METHODS))
w = 0.38
b1 = ax.bar([i - w / 2 for i in x], [v * 100 for v in RECALL], w, label="召回率")
b2 = ax.bar([i + w / 2 for i in x], [v * 100 for v in FPR], w, label="误报率")
ax.set_xticks(list(x))
ax.set_xticklabels(METHODS)
ax.set_ylabel("百分比 (%)")
ax.set_ylim(0, 115)
ax.set_title("各方案机密识别召回率与误报率（n=184）")
bar_labels(ax, b1, "{:.1f}")
bar_labels(ax, b2, "{:.1f}")
ax.legend()
ax.grid(axis="y", alpha=0.3)
save(fig, "fig6-2-metrics.png")

# ---- 图 6-3 消融
NAMES = {"Full": "全量", "-regular": "-正则", "-checksum": "-校验位",
         "-keyword": "-关键词", "-darkrule": "-暗语规则", "-piiweight": "-PII加权"}
keys = list(NAMES)
fig, ax = plt.subplots(figsize=(8, 3.8))
x = range(len(keys))
w = 0.38
b1 = ax.bar([i - w / 2 for i in x], [ABL[k]["recall"] * 100 for k in keys], w, label="召回率")
b2 = ax.bar([i + w / 2 for i in x], [ABL[k]["fpr"] * 100 for k in keys], w, label="误报率")
ax.set_xticks(list(x))
ax.set_xticklabels([NAMES[k] for k in keys])
ax.set_ylabel("百分比 (%)")
ax.set_ylim(0, 45)
ax.set_title("L1 消融实验：各组件对召回与误报的影响（n=184）")
bar_labels(ax, b1, "{:.1f}")
bar_labels(ax, b2, "{:.2f}")
ax.legend()
ax.grid(axis="y", alpha=0.3)
save(fig, "fig6-3-ablation.png")
