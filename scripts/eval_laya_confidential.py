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

Q = {
    "is_confidential": {
        "type": "noul",
        "instructions": (
            "Is this content enterprise-confidential (engineering drawings, financial data like "
            "salaries/costs/revenue, personal sensitive data like ID/phone/bank card, or business "
            "secrets like contracts/bids/internal docs)? A table with employee names and salary "
            "amounts is confidential."
        ),
        "criteria": {"true": "contains confidential content", "false": "public or everyday content"},
    },
}

PROBES = [
    ("工资表(中文)", "文件名: 2026年3月工资表.xlsx\n表头: 姓名,部门,基本工资,奖金\n内容预览:\n张三 研发部 18500 3200\n李四 市场部 12800 2100", 1),
    ("工资表(英文)", "File: 2026-03-payroll.xlsx\nHeaders: name,department,base_salary,bonus\nContent:\nZhang San R&D 18500 3200\nLi Si Marketing 12800 2100", 1),
    ("施工图(中文)", "施工图号 S-2026-041，三层框架结构图，梁柱配筋参数与混凝土强度等级 C35，基础开挖深度 3.2 米", 1),
    ("身份证号(中文)", "客户资料：姓名王五，身份证号 110101199003071234，手机号 13812345678，住址北京市朝阳区", 1),
    ("报价单(中文)", "报价单：设备采购合同，总价 1,280,000 元，税率13%，含安装费，付款周期 30 天", 1),
    ("技术文档(中文)", "FastAPI 依赖注入说明：Depends 可复用鉴权逻辑，中间件处理横切关注点，路由函数保持纯粹", 0),
    ("技术讨论(中文)", "MySQL 索引优化：联合索引遵循最左前缀原则，覆盖索引避免回表，explain 看 type 和 rows", 0),
    ("闲聊(中文)", "中午吃什么？楼下新开了家面馆，听说牛肉面不错，要不要一起去尝尝", 0),
]


def render(text, filename):
    return f"文件名: {filename}\n\n内容预览:\n{text}" if filename else f"内容预览:\n{text}"


def eval_agent(agent, cases, label):
    print(f"\n=== {label} ===")
    correct7, correct5, tp, fp, fn, tn = 0, 0, 0, 0, 0, 0
    for name, state, want in cases:
        res = agent.predict(state, Q)
        p = float(res["answers"]["is_confidential"]["noul"])
        at7 = 1 if p >= 0.7 else 0
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
    a2 = laya.load("/home/your-user/models/laya-finetuned-confidential-v4")
    a2.cfg["max_len"] = 2048

    z7, z5 = eval_agent(a0, probe_cases, "零样本 · 8 例探针")
    f7, f5 = eval_agent(a1, probe_cases, "微调v1 · 8 例探针")
    g7, g5 = eval_agent(a2, probe_cases, "微调v4 · 8 例探针")
    zr7, zr5 = eval_agent(a0, real_cases, "零样本 · 真实留出 23")
    fr7, fr5 = eval_agent(a1, real_cases, "微调v1 · 真实留出 23")
    gr7, gr5 = eval_agent(a2, real_cases, "微调v4 · 真实留出 23")
    print(f"\n汇总: acc@0.7 探针 {z7:.0%}→{f7:.0%}→{g7:.0%} | 真实 {zr7:.0%}→{fr7:.0%}→{gr7:.0%} ; acc@0.5 真实 {zr5:.0%}→{fr5:.0%}→{gr5:.0%}")


if __name__ == "__main__":
    main()
