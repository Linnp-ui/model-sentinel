"""在线复测电池：8探针+真实留出23+7回归，全打运行中的 laya 服务 @0.7 口径。

用法: .venv/bin/python scripts/eval_laya_live.py [classify_url]
默认 http://127.0.0.1:8003/classify。离线口径（checkpoint 本地推理）见 eval_laya_confidential.py。
注意：probe 文本别加人工 ID 前缀（如「灰区B3:」）——laya 是 argmax 确定性推理，
前缀会扰动输出，制造误报/误修的测试假象（2026-09 灰区 B 复测教训）。
"""
import json
import sys
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8003/classify"
REAL = "/home/your-user/models/laya-dataset/real_eval.jsonl"

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

REG = [
    ("swap-差评语", "绩效评语：Below Expectations。张三 研发部 18500 3200", 1),
    ("swap-改进语", "绩效评语：Needs Improvement。张三 研发部 18500 3200", 1),
    ("长表12行", "通讯录：" + "；".join(f"李{i} 1380000{i:04d} 技术部" for i in range(12)), 1),
    ("灰-CAP", "请详细介绍CAP定理的三个特性以及在分布式系统中的应用场景和权衡，字数不少于60字", 0),
    ("灰-邮件改写", "请帮我把下面这句话改写成更礼貌的商务邮件开头，保持原意不变，另外再给两个备选版本。", 0),
    ("灰-涨薪问", "咨询一下，我们部门涨薪方案什么时候出？", 0),
    ("灰-mention", "# Files mentioned by the user: ## local_financial_salary.csv", 1),
    # 偏严护栏（2026-09-28 灌数实证 qwen 误报型分歧）：问制度/流程/概念且无 payload → 放行。
    # 这组之前不在电池里，偏严补丁在 battery 上测不出来，只能等真流量再翻车。
    ("严-英文薪资闲聊", "what does the salary structure look like", 0),
    ("严-英文奖金问", "how is the bonus calculated every quarter", 0),
    ("严-图号规则问", "结构图的图号规则是什么？", 0),
    ("严-税率口径问", "报价的税率口径是什么？", 0),
    # 反向护栏：同题材带 payload 必须仍判密（防补丁把密的一起放）
    ("严-薪资带payload", "薪酬结构说明：张三 研发部 基本工资 18500 绩效 3200", 1),
    ("严-图号带图号", "施工图号 S-2026-041 的图号编制规则，配筋参数与混凝土强度等级 C35", 1),
    # 机密文件名护栏（2026-09-28 灌数实证 qwen 三连漏报：只写「帮我看看 <机密文件名>」就放行）。
    # 用改写变体：原文本已在 train.jsonl 里（合成时引用同批文件名），进电池会虚高 laya 分数。
    ("名-薪酬表", "帮我看看 2026年薪酬明细表.xlsx", 1),
    ("名-花名册", "帮我看看 department_roster_2026.xlsx", 1),
    ("名-营收csv", "帮我看看 annual_revenue_breakdown.csv", 1),
    ("名-反向手册", "帮我看看 新员工入职手册.docx", 0),
    ("名-反向说明", "帮我看看 园区网络使用说明.pdf", 0),
]


def cls(text, filename=""):
    body = json.dumps({"text": text, "filename": filename}).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    return float(json.load(urllib.request.urlopen(req, timeout=120))["probabilities"]["CONFIDENTIAL"])


def run(cases, title):
    tp = fp = fn_ = tn = 0
    print(f"\n=== {title} ===")
    for name, text, fn, want in cases:
        p = cls(text, fn)
        at7 = 1 if p >= 0.7 else 0
        if want and at7:
            tp += 1
        elif not want and at7:
            fp += 1
        elif want:
            fn_ += 1
        else:
            tn += 1
        print(f"  {'✓' if at7 == want else '✗'} {name}: p={p:.3f} (0.7判:{'密' if at7 else '非密'})")
    print(f"  → {title}: {tp + tn}/{len(cases)}  TP{tp} FP{fp} FN{fn_} TN{tn}")
    return tp + tn, len(cases)


def main():
    real = []
    with open(REAL, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            real.append((f"{r['source'][:28]}|{r['text'][:20]}", r["text"], r.get("filename", ""), r["label"]))
    a, b = run([(n_, t, "", w) for n_, t, w in PROBES], "8 例探针")
    c, d = run(real, "真实留出 23")
    e, f_ = run([(n_, t, "", w) for n_, t, w in REG], "回归探针(swap/长表/灰)")
    print(f"\n总计: {a + c + e}/{b + d + f_}")


if __name__ == "__main__":
    main()
