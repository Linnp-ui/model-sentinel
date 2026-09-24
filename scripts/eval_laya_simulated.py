"""Laya 模拟评测集（与训练集不同种子/句式，含边缘易混案例）：零样本 vs 微调

用法: CUDA_VISIBLE_DEVICES=6 ~/venvs/laya/bin/python scripts/eval_laya_simulated.py
输出: 分类别准确率 + 阈值0.7 的 FP/FN + 概率分离度
"""
import json
import os
import random
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

# 与训练脚本不同的名字/句式池（防模板记忆）
SURN = list("刘张陈杨黄吴周徐孙胡朱高林何郭罗郑梁谢宋唐许韩冯邓曹彭")
GIV = ["志强", "秀兰", "建国", "淑珍", "国庆", "雅静", "文斌", "慧敏", "立军", "晓彤", "俊杰", "雪莉", "永福", "佳琪", "海涛", "梦瑶"]
DEPT = ["工程部", "销售部", "采购部", "研发一部", "质量部", "制造部", "研发二部", "人力部"]
CITY = ["南京市江宁区", "苏州市工业园区", "武汉市东湖高新区", "重庆市渝北区", "宁波市鄞州区"]
STRT = ["科创路 12 号 5 幢 1103", "滨河西路 66 号 2 单元 801", "高新区软件园 A 区 902"]
BANK = ["中国银行", "邮储银行", "中信银行", "光大银行"]

rng = random.Random(998877)  # 与训练 seed 20260922 不同


def nm():
    return rng.choice(SURN) + rng.choice(GIV)

def money(lo, hi):
    return f"{rng.randint(lo, hi):,}"

def phone():
    return "1" + rng.choice("35789") + "".join(rng.choice("0123456789") for _ in range(9))

def idc():
    b = rng.choice(["320115", "320505", "420102", "500112", "330212"]) + \
        f"{rng.randint(1970, 1999)}{rng.randint(1, 12):02d}{rng.randint(1, 28):02d}" + \
        "".join(rng.choice("0123456789") for _ in range(3))
    w = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    return b + "0123456789X"[(11 - sum(int(c) * x for c, x in zip(b, w))) % 11]

def card():
    return "".join(rng.choice("0123456789") for _ in range(rng.choice([16, 19])))

# ---------------- 正样本（与训练不同句式）
def s_salary():
    n = rng.randint(4, 7)
    rows = "\n".join(
        f"{nm()}｜{rng.choice(DEPT)}｜{money(9000, 46000)}｜{money(800, 12000)}｜{money(1000, 9000)}"
        for _ in range(n))
    return f"2026 年 {rng.randint(1, 8)} 月绩效奖金分配表（含基本工资、绩效系数 {rng.choice(['1.0','1.2','0.9','1.5'])}）：\n{rows}"

def s_fin():
    return rng.choice([
        f"部门费用台账（{rng.randint(1,8)} 月）：差旅 {money(20000,180000)} 元、招待 {money(5000,60000)} 元、设备维护 {money(8000,90000)} 元，超预算 {rng.randint(3,18)}%，需说明原因。",
        f"年度决算对比：营业收入 {money(30000000,90000000)} 元，营业成本 {money(20000000,70000000)} 元，净利率 {rng.randint(4,25)}%，与预算偏差 {rng.randint(2,12)}%。",
        f"资金计划：下月需付供应商货款 {money(500000,8000000)} 元（{rng.choice(DEPT)} 采购），工资发放 {money(300000,4000000)} 元，税费 {money(100000,1500000)} 元。",
    ])

def s_quote():
    items = "、".join(
        f"{rng.choice(['伺服电机','减速器','传感器','控制柜','焊接机器人','气动元件'])} {rng.randint(1,6)} 台 {money(2000,150000)}/台"
        for _ in range(rng.randint(2, 4)))
    return rng.choice([
        f"投标文件技术商务部分：总报价 {money(800000, 6000000)} 元，构成明细：{items}。投标有效期 90 天，价格构成属商业秘密。",
        f"询价回复单：{items}，合计 {money(100000, 3000000)} 元（不含税），交货期 {rng.randint(15,60)} 天。此报价仅对 {nm()} 经理提供的需求有效。",
    ])

def s_pii():
    return rng.choice([
        f"背景调查表：候选人 {nm()}，身份证 {idc()}，电话 {phone()}，家庭住址 {rng.choice(CITY)}{rng.choice(STRT)}，前雇主联系人 {nm()}。",
        f"报销登记：{nm()} 提交 {money(500, 9000)} 元报销，银行卡 {card()}（{rng.choice(BANK)}），手机号 {phone()}，发票 3 张。",
        f"家访记录：{nm()}（{nm()} 之父），住址 {rng.choice(CITY)}{rng.choice(STRT)}，联系电话 {phone()}，家庭年收入约 {money(50000, 200000)} 元。",
    ])

def s_contract():
    return rng.choice([
        f"保密协议（NDA）编号 {rng.choice('BX')}-{rng.randint(2025,2026)}-{rng.randint(10,99)}：双方就 {rng.choice(['联合研发','技术转让','供应链合作'])} 项目交换的技术资料互为保密信息，保密期 5 年，违约金 {money(500000,5000000)} 元。",
        f"内部会议纪要：{rng.choice(DEPT)} {rng.randint(1,12)} 月 {rng.randint(1,28)} 日例会，确定 {rng.choice(['年度预算','薪酬调整','客户报价策略','产线改造方案'])} 方案，涉及金额 {money(100000,9000000)} 元，参会人员 {nm()}、{nm()}、{nm()}。本纪要内部留存。",
    ])

def s_draw():
    return rng.choice([
        f"结构布置图（图号 {rng.choice('S/J')}-{rng.randint(2024,2026)}-{rng.randint(10,99)}）：主梁截面 300×600，柱距 {rng.choice(['7.2','8.1','9.0'])} m，钢筋 {rng.choice(['HRB400','HRB500'])}，抗震等级 {rng.choice(['二级','三级'])}。",
        f"工艺纪律文件：关键工序 {rng.randint(1,9)}-{rng.randint(10,99)} 参数——焊接电流 {rng.randint(80,220)} A、焊接速度 {rng.randint(30,120)} mm/min、保压时间 {rng.randint(3,30)} s，参数变更需工艺处批准，属工艺秘密。",
        f"试验报告（内部）：批次 {rng.randint(1000,9999)} 盐雾试验 {rng.choice(['48','96','240'])} h 后 {rng.choice(['不合格','临界'])}，失效模式分析数据附后，报告编号 {rng.choice('SY')}-{rng.randint(100,999)}。",
    ])

# ---------------- 负样本
def s_tech():
    t, d = rng.choice([
        ("Go 并发模型", "goroutine 由运行时调度器 GMP 管理，channel 用于通信而非共享内存，select 多路复用注意 default 分支的饥饿问题，context 传递取消信号"),
        ("Java 垃圾回收", "G1 把堆划成 Region 并按回收价值排序，ZGC 用染色指针实现并发重定位，停顿控制在毫秒级，调优先看 GC 日志的晋升速率"),
        ("React 渲染原理", "虚拟 DOM diff 按同层比较与 key 复用，useMemo 缓存计算结果但别滥用，并发模式下 useTransition 把非紧急更新让路给紧急交互"),
        ("Kubernetes 存储", "PV 是集群资源、PVC 是用户申请，StorageClass 动态供给，ReadWriteOnce 多副本要配合 sidecar 或用 ReadOnlyMany，CSI 驱动负责对接后端"),
        ("Nginx 反向代理", "upstream 权重与 keepalive 连接池，proxy_next_upstream 控制故障切换，限流用 limit_req 按 IP+URI，注意 gzip 与缓冲对 SSE 的影响"),
        ("SQL 调优", "执行计划看索引扫描与回表，改写存在子查询为 join，大事务拆小，热点行更新用队列削峰，统计信息过期会导致选错计划"),
        ("Linux 性能分析", "perf 采样看火焰图，ftrace 追系统调用延迟，io_uring 异步 IO 适合高并发盘读写，NUMA 绑核对内存密集型负载影响明显"),
        ("Git 工作流", "主干开发配短周期合并，rebase 保持线性历史但别动公共分支，cherry-pick 修紧急补丁，分支保护规则挡住强推"),
    ])
    return f"技术笔记 · {t}：{d}。整理自团队分享，供参考。"

def s_meet():
    return rng.choice([
        f"项目周会：{rng.choice(['数据平台','官网改版','App 性能','客服系统'])} 进度 {rng.randint(40,90)}%，本周完成 {rng.choice(['接口联调','压测','灰度发布','需求评审'])}，风险是 {rng.choice(['第三方排期','测试环境','人员请假'])}，已同步负责人。",
        f"代码评审纪要：本次改动涉及 {rng.choice(['缓存','消息队列','鉴权','日志'])} 模块，结论是 {rng.choice(['可以合并','补两个单测后合并','拆分后再审'])}，命名与错误码按规范统一。",
        f"运维复盘：昨晚的告警根因是 {rng.choice(['证书过期','磁盘写满','配置漂移','依赖超时'])}，已加监控与自愈脚本，改进项 {rng.randint(2,5)} 条全部指派。",
    ])

def s_chat():
    return rng.choice([
        f"这周末打算去 {rng.choice(['江边','山里','古镇','植物园'])} 转转，带本书发发呆，有人拼车吗，周六早上走",
        "新买的咖啡机到了，第一杯手冲翻车了，粉太细全堵了，求推荐入门研磨度参数，预算内的磨豆机也求推荐",
        f"最近在追一部 {rng.choice(['科幻','悬疑','历史'])} 剧，节奏很慢但后劲大，看到一半被结局预告吓到了，看过的大佬来聊聊",
        "跑步三个月了，配速从七分半降到六分一，下周准备跑第一个五公里，膝盖有点紧，要不要先做个拉伸计划",
    ])

def s_public():
    return rng.choice([
        f"公司公告（公开披露）：本公司 {rng.randint(2024,2026)} 年第 {rng.randint(1,4)} 季度营业收入 {money(100000000,900000000)} 元，同比增长 {rng.randint(3,28)}%，详见交易所指定网站披露文件。",
        f"产品发布会预告：{rng.choice(['新款笔记本','折叠屏手机','智能手表','无人机'])} 新品发布会定于 {rng.randint(9,12)} 月 {rng.randint(1,28)} 日举行，届时公布售价与配置，媒体报名见官网。",
        f"开源项目更新：v{rng.randint(1,5)}.{rng.randint(0,9)}.{rng.randint(0,20)} 修复 {rng.randint(2,8)} 个内存泄漏，新增插件 API 与 CLI 子命令，采用 Apache-2.0 协议，欢迎贡献。",
        f"行业快讯：据公开市场数据，{rng.choice(['光伏组件','动力电池','工业机器人','服务器'])} 行业 {rng.randint(1,8)} 月出货量 {money(80000,900000)} 台，环比 {rng.choice(['增长','持平'])} {rng.randint(2,15)}%。",
    ])

def s_edge_pub():
    # 易混边缘：数字很多但内容公开
    return rng.choice([
        f"上市公司年报摘要（公开）：营业收入 {money(500000000,8000000000)} 元，净利润 {money(20000000,600000000)} 元，研发投入 {money(10000000,300000000)} 元，已在上交所官网披露。",
        f"公开产品价格表（官网下载）：标准版 {money(1000,9999)}/年，专业版 {money(5000,49999)}/年，企业版另议；教育与非营利组织 {rng.choice(['五折','六折','七折'])}。",
        f"公开招标中标公告（政府网站）：{rng.choice(['办公设备采购','道路养护服务','监控系统建设','机房空调采购'])} 项目中标人 {rng.choice(['华','中','东','科'])}{rng.choice(['达','信','腾','远'])}科技有限公司，中标金额 {money(200000,8000000)} 元。",
        f"招聘启事（公开）：{rng.choice(['后端工程师','算法工程师','测试工程师','产品经理'])} 若干名，薪资 {money(15,45)}K-{money(46,90)}K·15 薪，要求 {rng.randint(3,8)} 年以上经验，简历投递至官网招聘页。",
        f"高校公开讲座：{rng.choice(['量子计算','脑机接口','大模型安全','新能源材料'])} 专题报告，主讲 {nm()} 教授，地点 {rng.choice(['一号报告厅','学术中心','大礼堂'])}，免费开放。",
    ])

GEN_POS = [("薪资绩效", s_salary), ("财务台账", s_fin), ("报价投标", s_quote),
           ("个人信息", s_pii), ("合同纪要", s_contract), ("图纸工艺", s_draw)]
GEN_NEG = [("技术笔记", s_tech), ("例会纪要", s_meet), ("日常闲聊", s_chat),
           ("公开内容", s_public), ("边缘-公开数字", s_edge_pub)]
N_PER = 24  # 每类


def build():
    cases = []
    for name, fn in GEN_POS:
        for _ in range(N_PER):
            cases.append((name, fn(), 1))
    for name, fn in GEN_NEG:
        for _ in range(N_PER):
            cases.append((name, fn(), 0))
    rng.shuffle(cases)
    return cases


def render(text):
    return f"内容预览:\n{text}"


def run(agent, cases):
    rows = {}
    for name, text, want in cases:
        p = float(agent.predict(render(text), Q)["answers"]["is_confidential"]["noul"])
        rows.setdefault(name, []).append((p, want))
    return rows


def summarize(label, rows):
    allp, allw = [], []
    print(f"\n=== {label} ===")
    print(f"{'类别':<12} {'n':>3} {'acc@0.7':>8} {'acc@0.5':>8} {'FP':>3} {'FN':>3} {'p中位(正)':>10} {'p中位(负)':>10}")
    tot7 = tot5 = n = 0
    for name, lst in rows.items():
        c7 = c5 = fp = fn_ = 0
        pp, pn = [], []
        for p, want in lst:
            allp.append(p); allw.append(want)
            n += 1
            n7, n5 = p >= 0.7, p >= 0.5
            c7 += n7 == want; c5 += n5 == want
            tot7 += n7 == want; tot5 += n5 == want
            if want and not n7: fn_ += 1
            if not want and n7: fp += 1
            (pp if want else pn).append(p)
        mp = (lambda v: f"{sorted(v)[len(v)//2]:.3f}" if v else "  -  ")
        print(f"{name:<12} {len(lst):>3} {c7/len(lst):>8.0%} {c5/len(lst):>8.0%} {fp:>3} {fn_:>3} {mp(pp):>10} {mp(pn):>10}")
    ps = sorted(allp)
    pos = [p for p, w in zip(allp, allw) if w]
    neg = [p for p, w in zip(allp, allw) if not w]
    brier = sum((p - w) ** 2 for p, w in zip(allp, allw)) / len(allp)
    print(f"{'合计':<12} {n:>3} {tot7/n:>8.0%} {tot5/n:>8.0%}")
    print(f"  概率分离: 正样本 p 中位 {sorted(pos)[len(pos)//2]:.3f} (p10={sorted(pos)[len(pos)//10]:.3f}) "
          f"| 负样本 p 中位 {sorted(neg)[len(neg)//2]:.3f} (p90={sorted(neg)[len(neg)*9//10]:.3f}) | Brier {brier:.4f}")


def main():
    cases = build()
    print(f"模拟评测集: {len(cases)} 条 (正 {sum(c[2] for c in cases)} / 负 {sum(1-c[2] for c in cases)}), seed 998877")
    a0 = laya.load("/home/your-user/models/laya/multilingual"); a0.cfg["max_len"] = 2048
    a1 = laya.load("/home/your-user/models/laya-finetuned-confidential"); a1.cfg["max_len"] = 2048
    summarize("零样本 (multilingual)", run(a0, cases))
    summarize("微调 v1 (confidential-zh)", run(a1, cases))
    if len(sys.argv) > 1:
        a2 = laya.load(sys.argv[1]); a2.cfg["max_len"] = 2048
        import os as _os
        summarize("微调 %s" % _os.path.basename(sys.argv[1]), run(a2, cases))


if __name__ == "__main__":
    main()
