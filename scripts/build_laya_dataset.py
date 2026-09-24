"""构建 Laya L2 微调数据集（审计库真实样本 + 模板槽位合成增广）

用法: python scripts/build_laya_dataset.py
输出: ~/models/laya-dataset/{train.jsonl, real_eval.jsonl, stats.json}
  - train.jsonl   = 真实(审计去重)训练集 + 合成增广
  - real_eval.jsonl = 真实留出集（评估微调增益用，不进训练）
  - 数据含公司机密内容，**留在本机 models 目录，不进仓库**

标签口径（与网关判定链一致，deny-overrides）：
  正(1) = l2_confidential / pii_* / financial_local_only / block_secrets / drawing_local_only
  负(0) = default_allow
  排除  = session_route_local / AB文件 / first_chunk_fallback 等歧义规则
"""
import hashlib
import json
import os
import random
import re
import statistics
from collections import Counter

import pymysql

OUT_DIR = os.path.expanduser("~/models/laya-dataset")
AUDIT_URL = dict(host="127.0.0.1", user="root", password="changeme",
                 database="gateway_audit", charset="utf8mb4")
POS_RULES = {"l2_confidential", "pii_weighted_route_local", "financial_local_only",
             "block_secrets", "pii_critical_block", "drawing_local_only"}
NEG_RULES = {"default_allow"}
MIN_CHARS = 30  # 真实样本长度下限（2026-09-23 起 L2 门取消字数触发，短文本覆盖靠合成模板）
EVAL_FRAC = 0.32  # 真实样本留出比例（73 → 23 评估 / 50 训练）

SEED = 20260922
random.seed(SEED)

# ---------------------------------------------------------------- 真实样本
def extract_real():
    conn = pymysql.connect(**AUDIT_URL)
    cur = conn.cursor()
    cur.execute("SELECT rule, text_preview, filename, ts FROM audit_logs "
                "WHERE text_preview IS NOT NULL AND CHAR_LENGTH(text_preview) >= %s", (MIN_CHARS,))
    seen = {}
    for rule, text, fn, ts in cur.fetchall():
        norm = re.sub(r"\s+", " ", text).strip()
        label = 1 if rule in POS_RULES else (0 if rule in NEG_RULES else None)
        if label is None:
            continue
        h = hashlib.sha1(norm.encode()).hexdigest()
        if h in seen:
            if label == 1 and seen[h]["label"] == 0:
                seen[h]["label"], seen[h]["source"] = 1, f"audit:{rule}"  # 冲突取密
            continue
        seen[h] = {"text": norm, "label": label, "source": f"audit:{rule}",
                   "filename": fn or "", "ts": str(ts)}
    conn.close()
    items = sorted(seen.values(), key=lambda v: hashlib.sha1(v["text"].encode()).hexdigest())
    n_eval = max(1, int(len(items) * EVAL_FRAC))
    return items[n_eval:], items[:n_eval]  # 确定性切分


# ---------------------------------------------------------------- 合成增广
SURNAMES = list("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐薛雷贺倪汤")
GIVEN = ["伟", "芳", "娜", "敏", "静", "磊", "军", "洋", "勇", "艳", "杰", "涛", "明", "超", "秀英", "霞", "平", "刚", "桂英", "华", "建", "国", "文", "辉", "志强", "海燕", "晓东", "丽华", "建军", "雪梅", "子涵", "雨欣", "浩然", "紫萱", "俊杰", "婷婷", "建国", "美玲", "志强", "春燕"]
DEPTS = ["研发部", "市场部", "财务部", "人事部", "采购部", "生产部", "质量部", "设计部", "项目部", "运维部", "法务部", "行政部"]
CITIES = ["北京市朝阳区", "上海市浦东新区", "深圳市南山区", "杭州市西湖区", "成都市武侯区", "武汉市洪山区", "西安市雁塔区", "广州市天河区"]
STREETS = ["建国路 88 号院 3 栋 502", "科技园区 B 座 12 层", "长江路 126 号 6 单元 901", "高新大道 9 号 2 幢 803", "望京SOHO T1 1801"]

def rnd_name():
    return random.choice(SURNAMES) + random.choice(GIVEN)

def rnd_phone():
    return "1" + random.choice("35789") + "".join(random.choice("0123456789") for _ in range(9))

def rnd_id_card():
    # GB 11643 mod 11-2，与 inspection.idcard_valid 同公式（之前版本校验位全错，0/20 有效）
    region = random.choice(["110101", "310115", "440305", "330106", "510107", "420111"])
    ymd = f"{random.randint(1965, 2000)}{random.randint(1,12):02d}{random.randint(1,28):02d}"
    seq = "".join(random.choice("0123456789") for _ in range(3))
    body = region + ymd + seq
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    chk = "10X98765432"[sum(int(c) * w for c, w in zip(body, weights)) % 11]
    return body + chk

def rnd_bank_card():
    # Luhn 校验位，与 inspection.luhn_valid 对齐
    n = random.choice([16, 19])
    digits = [random.choice("0123456789") for _ in range(n - 1)]
    s = 0
    for i, c in enumerate(reversed(digits)):
        d = int(c)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        s += d
    return "".join(digits) + str((10 - s % 10) % 10)

def rnd_money(lo=8000, hi=50000):
    v = random.randint(lo, hi)
    return f"{v:,}" if random.random() < 0.5 else str(v)

def rnd_drawing_no():
    return f"{random.choice(['S', 'J', 'G'])}-{random.randint(2023, 2026)}-{random.randint(1, 999):03d}"

# 正样本模板：每模板 N 条
def synth_positives(n_per=220):
    out = []

    def payroll():
        rows = []
        for _ in range(random.randint(3, 8)):
            rows.append(f"{rnd_name()} {random.choice(DEPTS)} {rnd_money()} {rnd_money(500,8000)} {rnd_money(600,2500)}")
        month = f"2026年{random.randint(1,8)}月"
        return (f"文件名: {month}工资表.xlsx\n表头: 姓名,部门,基本工资,奖金,社保扣款\n"
                f"内容预览:\n" + "\n".join(rows))

    def financial():
        m = f"2026年{random.randint(1,8)}月"
        kind = random.choice([
            f"{m}成本核算：原材料 {rnd_money(200000,900000)} 元，人工 {rnd_money(150000,600000)} 元，制造费用 {rnd_money(50000,300000)} 元",
            f"{m}营收 {rnd_money(5000000,80000000)} 元，营业利润 {rnd_money(300000,4000000)} 元，毛利率 {random.randint(12,42)}%",
            f"年度预算：研发费用 {rnd_money(1000000,9000000)} 元，市场推广 {rnd_money(500000,4000000)} 元，年终奖池 {rnd_money(2000000,12000000)} 元",
            f"资产负债表（{m}）：总资产 {rnd_money(10000000,300000000)} 元，负债 {rnd_money(3000000,120000000)} 元，所有者权益 {rnd_money(5000000,180000000)} 元",
        ])
        return f"内容预览:\n{kind}。以上数据仅限内部使用，不得对外披露。"

    def quote():
        items = "\n".join(
            f"{random.choice(['数控加工设备','液压系统','检测仪器','工业传感器','传动组件'])} ×{random.randint(1,5)}  单价 {rnd_money(3000,200000)}  小计 {rnd_money(3000,900000)}"
            for _ in range(random.randint(2, 5)))
        total = rnd_money(50000, 1500000)
        return (f"报价单（项目编号 {random.choice('ABC')}{random.randint(1000,9999)}）\n{items}\n"
                f"总价（含税）{total} 元，税率 {random.choice(['13','6','9'])}%，付款周期 {random.choice(['30','45','60'])} 天。本报价自发出之日起 30 天内有效，价格含保密义务。")

    def pii():
        name = rnd_name()
        return random.choice([
            f"客户资料：姓名 {name}，身份证号 {rnd_id_card()}，手机号 {rnd_phone()}，住址 {random.choice(CITIES)}{random.choice(STREETS)}",
            f"入职档案：{name}，身份证 {rnd_id_card()}，银行卡 {rnd_bank_card()}（开户行 {random.choice(['工行','建行','招行','农行'])}），紧急联系人 {rnd_name()} {rnd_phone()}",
            f"工资发放信息：{name}，开户行 {random.choice(['工行','建行','招行'])}，卡号 {rnd_bank_card()}，手机号 {rnd_phone()}",
            f"报销单：{name}（{rnd_phone()}），身份证号 {rnd_id_card()}，转账至银行卡 {rnd_bank_card()}",
        ])

    def contract():
        proj = random.choice(["智能化改造","生产线升级","数据平台建设","厂房建设","设备采购"])
        return random.choice([
            f"合同编号 {random.choice('HT')}-{random.randint(2024,2026)}-{random.randint(100,999)}：{proj}项目合同，合同金额 {rnd_money(500000,80000000)} 元，"
            f"乙方承担保密义务，合同内容属商业秘密，未经甲方书面同意不得向第三方披露。",
            f"投标文件（{proj}）：投标报价 {rnd_money(1000000,90000000)} 元，技术方案与成本构成属商业秘密，开标前不得泄露。",
            f"内部会议纪要（{proj}复盘）：讨论 {rnd_money(100000,5000000)} 元成本分摊方案与下季度预算，参会人 {rnd_name()}、{rnd_name()}。本纪要属内部资料，不得外传。",
        ])

    def drawing():
        return random.choice([
            f"施工图号 {rnd_drawing_no()}，{random.choice(['三层框架','五层剪力墙','钢结构厂房','地下车库'])}结构图，"
            f"梁柱配筋 {random.choice(['HRB400','HRB500'])}，混凝土强度等级 {random.choice(['C30','C35','C40'])}，基础开挖深度 {random.randint(2,6)}.{random.randint(0,9)} 米。",
            f"图号 {rnd_drawing_no()} 工艺参数：切削速度 {random.randint(80,400)} m/min，进给量 {random.randint(5,60)} mm/min，主轴功率 {random.randint(5,30)} kW，该参数为工艺秘密。",
        ])

    # v3 新增：英文混合薪资财务 / 预算话术 / 短密钥 / 拼音绕过 / 分红 / 图纸口语 / 薪酬话题
    # （训练句式与 tests/fixtures/security_testset.jsonl 评测集错开，避免训测泄漏）
    def mixed_salary():
        n = rnd_name()
        return random.choice([
            f"把{n}的 salary package 发我一下，base {random.randint(20,60)}k，bonus 2个月",
            f"btw 我 offer 涨了，base {random.randint(30,60)}k，{random.randint(14,18)}薪",
            f"sync 一下，会上定的 bonus {random.randint(2,6)}个月外加13薪",
            f"{n}的 package 是 {random.randint(25,50)}k per month，含股票",
            f"帮我 polish 一下：my current package is {random.randint(25,50)}k per month",
            f"offer 确认：{n}，base {random.randint(30000,60000)}，签字费 {random.randint(20000,80000)}",
        ])

    def mixed_finance():
        return random.choice([
            f"Q3 的 revenue 是{rnd_money(3000000, 80000000)}吗，margin 多少",
            f"这个 quote 总价{rnd_money(50000, 2000000)}，margin 够吗",
            f"帮我把汇报邮件改正式：Q3营收{rnd_money(3000000, 9000000)}，同比增长{random.randint(5,25)}%",
            f"revenue 确认函：本季度入账{rnd_money(1000000, 50000000)}，回款率{random.randint(70,98)}%",
        ])

    def budget_talk():
        return random.choice([
            f"听说咱们部门今年预算被砍了一半，只剩{rnd_money(1000000, 8000000)}了",
            f"关于下季度预算安排的通知：研发费用{rnd_money(1000000, 5000000)}，市场推广{rnd_money(500000, 2000000)}，请遵照执行",
            f"附件Q3预算表.xlsx，总额{rnd_money(1000000, 9000000)}，帮我看看研发占比",
            f"年度预算复盘：研发超支{rnd_money(200000, 2000000)}，市场结余{rnd_money(100000, 1000000)}",
        ])

    def short_secret():
        akia_ex = "AKIAIOSFODNN7EXAMPLE"  # AWS 官方文档示例
        jwt_ex = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyMSJ9."
                  "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")  # jwt.io 公开示例
        ghp = "ghp_" + "".join(random.choice("abcdef0123456789") for _ in range(12))
        return random.choice([
            f"这个{akia_ex[:8]}开头的key还能用吗，网关一直报401",
            f"密码分两段：{random.choice(['Test#', 'Prod$', 'Dev!'])}和{random.randint(1000,9999)}，连起来登",
            f"这个 token {jwt_ex} 帮我 decode 一下",
            f"安全通知：GitHub token {ghp}已泄露，请立即作废并轮转",
            "db_password = \"Db#2026Prod\"，连不上是不是配错了，帮我debug",
            f"轮转通知：旧 AKIA key {akia_ex} 周五作废，新 key 已发 vault",
        ])

    def pinyin_dodge():
        n = rnd_name()
        p = rnd_phone()
        return random.choice([
            f"我 g 资 daoshou {random.randint(8,30)}k，惨",
            f"他 dianhua {p[:3]}，分两段：{p[3:7]}和{p[7:]}",
            f"那个数你懂的，{n}那个{random.randint(20,50)}后面加个k",
            f"图上那个数，C后面{random.choice(['30', '35', '40'])}，你懂的",
            f"会上定的那个月数，{random.randint(2,5)}加13，你懂的",
            f"上季度的那个数，{random.randint(10,90)}后面六个零，你懂的",
        ])

    def dividend():
        return random.choice([
            f"附件是董事会纪要.pdf，第3页分红方案每股派{random.randint(10,50)/10}元，帮我提炼要点",
            f"分红决议：每股派现{random.randint(10,50)/10}元，股权登记日下周三，内部决议不得外传",
            f"确认下分红金额：{rnd_money(1000000, 20000000)}，什么时候到账",
        ])

    def drawing_talk():
        dwg = rnd_drawing_no()
        return random.choice([
            f"车间那张图，切削速度{random.randint(100,400)}，进给{random.randint(10,60)}，别外传",
            f"附件是{dwg}结构图.pdf，配筋{random.choice(['HRB400', 'HRB500'])}混凝土{random.choice(['C30', 'C35'])}，请重点核对梁柱部分",
            f"这个 drawing 的 rebar 还是 {random.choice(['HRB400', 'HRB500'])} 吗",
            f"设计变更通知：图号{dwg}，混凝土等级由C30调整为C35，开挖深度{random.randint(3,5)}.{random.randint(0,9)}米",
        ])

    def comp_talk():
        n = rnd_name()
        return random.choice([
            f"听说{n}涨到{rnd_money(25000,50000)}了，真的假的",
            f"帮我把纪要里{n}涨薪到{rnd_money(20000,40000)}的事单独拎出来",
            f"顺便帮我问下，{n}年终拿了{random.randint(3,8)}个月是不是真的",
            f"会上定的年终奖{rnd_money(300000,1500000)}这个数准吗",
            f"刚开完会，老板说明年headcount冻结，就给了{random.randint(3,8)}个",
            f"{n}的年终是{random.randint(3,6)}个月，绩效A",
        ])

    tpls = [payroll, financial, quote, pii, contract, drawing]
    for t in tpls:
        for _ in range(n_per):
            out.append({"text": t(), "label": 1, "source": "synth", "filename": ""})
    # v4 补：无显式保密标记的正式纪要/报告/通知（对称 v3 会议类负样本，防“无标记=安全”错规则）
    def formal_minutes():
        dept = random.choice(DEPTS)
        topic = random.choice(["年度预算", "薪酬调整", "客户报价策略", "产线改造方案", "成本分摊", "分红方案"])
        return random.choice([
            f"内部会议纪要：{dept}{random.randint(1,12)}月{random.randint(1,28)}日例会，确定{topic}方案，"
            f"涉及金额{rnd_money(100000,9000000)}元，参会人员{rnd_name()}、{rnd_name()}。本纪要内部留存。",
            f"经营复盘会纪要：Q{random.randint(1,4)}营收{rnd_money(3000000,9000000)}，{topic}调整，"
            f"参会{rnd_name()}等。内部资料。",
            f"例会决议：{topic}通过，预算{rnd_money(500000,5000000)}，责任人{rnd_name()}，下周执行。",
        ])

    def test_report():
        return random.choice([
            f"试验报告（内部）：批次{random.randint(1000,9999)}盐雾试验{random.choice(['48', '96', '240'])}h后"
            f"不合格，失效模式分析见附件，报告编号S-{random.randint(100,999)}。",
            f"检测报告：{random.choice(['拉伸', '硬度', '探伤'])}不合格率{random.randint(2,15)}%，"
            f"涉及批次{random.randint(1000,9999)}，内部质量数据存档。",
            f"型式试验记录：{random.randint(100,999)}项通过{random.randint(90,99)}项，整改项见清单，报告内部存档。",
        ])

    def formal_notice_conf():
        return random.choice([
            f"设计变更通知：图号{rnd_drawing_no()}，混凝土等级由C30调整为C35，"
            f"开挖深度{random.randint(3,5)}.{random.randint(0,9)}米，请施工单位执行。",
            f"关于下季度预算安排的通知：研发费用{rnd_money(1000000,5000000)}，"
            f"市场推广{rnd_money(500000,2000000)}，请遵照执行。",
            f"调价通知：{random.choice(['数控加工', '液压系统'])}上调{random.randint(3,12)}%，新报价单见附件，内部掌握。",
        ])

    v3 = [mixed_salary, mixed_finance, budget_talk, short_secret, pinyin_dodge,
          dividend, drawing_talk, comp_talk]
    for t in v3:
        for _ in range(100):
            out.append({"text": t(), "label": 1, "source": "synth_v3", "filename": ""})
    v4 = [formal_minutes, test_report, formal_notice_conf]
    for t in v4:
        for _ in range(150):
            out.append({"text": t(), "label": 1, "source": "synth_v4", "filename": ""})
    return out


# 负样本模板：技术文档/讨论/闲聊/公开内容 —— 覆盖零样本误报重灾区
def synth_negatives(n_per=220):
    out = []

    def tech_doc():
        topic = random.choice([
            ("FastAPI 依赖注入", "Depends 可以复用鉴权逻辑，中间件负责全局横切关注点，路由函数保持纯粹，测试时可以用 dependency_overrides 替换实现"),
            ("MySQL 索引优化", "联合索引遵循最左前缀原则，覆盖索引避免回表，explain 看 type 和 rows，深分页用游标标记法替代 offset"),
            ("Docker 网络模式", "bridge 模式由 docker0 网桥隔离，host 模式共享宿主网络栈，overlay 用于跨主机容器网络，选型看隔离需求和性能"),
            ("Kubernetes 调度", "调度器先过滤再打分，亲和性用 nodeAffinity 表达，资源限制 request 决定调度、limit 决定驱逐，HPA 按 CPU 或自定义指标扩容"),
            ("Redis 数据结构", "string 支持位运算和分布式锁，hash 适合对象字段级更新，zset 做排行榜，list 做简单队列，注意大 key 分片"),
            ("消息队列选型", "Kafka 吞吐高适合日志流，RabbitMQ 路由灵活，RocketMQ 有事务消息，选型看吞吐、延迟、顺序性要求"),
            ("Python 异步模型", "event loop 单线程调度，async/await 让出控制权，阻塞调用要丢线程池，注意库的同步 API 会卡死整个 loop"),
            ("前端状态管理", "单向数据流：视图发 action，reducer 纯函数算新 state，中间件处理副作用，组件只读 state 渲染"),
            ("CI/CD 流水线", "代码提交触发构建、测试、镜像推送，制品库统一版本，发布走灰度，回滚保留上一个镜像标签"),
            ("数据库事务隔离", "RC 级别有幻读风险但锁少，RR 用 MVCC+间隙锁防幻读，分布式用 2PC 或 TCC，最终一致性靠对账"),
        ])
        t, d = topic
        return f"内容预览:\n{t}：{d}。本文档为团队技术分享材料，供内部学习参考。"

    def tech_discuss():
        return random.choice([
            f"今天团队讨论了数据库索引优化的几个方向，联合索引的最左前缀原则，覆盖索引避免回表，以及慢查询日志的定期分析机制，大家一致认为下季度要专门排期处理。",
            "周会记录：前端重构进度正常，组件库升级完成 80%，下周一联调支付回调，风险点是第三方沙箱环境不稳定，已准备降级开关。",
            "代码评审结论：这个 PR 把重试逻辑收敛到统一客户端，超时配置外提了，命名再改两处可以合，性能测试用例补一个就关闭。",
            f"架构讨论：服务拆分粒度按业务域走，{random.choice(['订单','库存','结算','权限'])} 模块先拆，共享数据库阶段用库表隔离，目标下季度独立部署。",
            "故障复盘：凌晨的告警是监控阈值设低了，扩容脚本的并发参数没跟上，已修阈值并加了演练，action items 都指派到人。",
        ])

    def chitchat():
        return random.choice([
            f"中午吃什么？楼下新开了家{random.choice(['面馆','米粉店','寿司店','轻食店'])}，听说{random.choice(['牛肉面','番茄汤','三文鱼','鸡胸沙拉'])}不错，要不要一起去尝尝",
            f"周末打算去{random.choice(['郊外','海边','古镇','山里'])}走走，天气不错适合露营，有人一起吗，周六早上十点集合",
            "最近在看一本推理小说，作者把线索埋得太深了，结局反转三次，强烈推荐悬疑爱好者去读，不过剧透党绕道",
            f"健身计划调整：这周加了一次{random.choice(['游泳','骑行','羽毛球'])}，跑量保持每周三次，蛋白质摄入要跟上，谁有推荐的补剂",
            "家里路由器换了新的，WiFi 信号覆盖好了不少，以前书房总掉线的问题解决了，晚上视频也不卡了",
        ])

    def public_doc():
        return random.choice([
            f"产品发布公告：新版本于 2026 年 {random.randint(3,9)} 月上线，支持多端同步、深色模式与插件市场，旧版用户可自动升级，详见官网帮助文档。",
            "公开培训课程介绍：本周六上午开设数据结构公开课，涵盖数组、链表、树与图的经典算法，面向零基础学员，免费报名。",
            f"行业报道摘要：据{random.choice(['公开报道','行业分析','市场研究'])}，2026 年国内新能源汽车销量同比增长 {random.randint(8,35)}%，充电桩基础设施建设持续加码。",
            "开源项目 README：本工具用于解析常见日志格式并输出结构化数据，支持命令行与库两种方式调用，采用 MIT 协议，欢迎提交 issue。",
        ])

    # 公开数字类负样本（2026-09-22 模拟评测发现：年报/公开价/中标公告/JD 薪资范围
    # 等"数字多但公开"内容零样本与 v1 微调都误判，补入训练）
    def public_numbers():
        return random.choice([
            f"上市公司年报摘要（公开披露）：营业收入 {random.randint(5,900):,}000000 元，净利润 {random.randint(2,60):,}000000 元，"
            f"研发投入 {random.randint(1,30):,}000000 元，数据已在{random.choice(['上交所','深交所','港交所'])}官网披露。",
            f"官网产品价格表：标准版 {random.randint(999,9999)}/年，专业版 {random.randint(4999,49999)}/年，企业版另议；教育与非营利组织{random.choice(['五折','六折','七折'])}。",
            f"政府网站中标公告：{random.choice(['办公设备采购','道路养护服务','监控系统建设','机房空调采购'])}项目中标人"
            f"{random.choice(['华','中','东','科'])}{random.choice(['达','信','腾','远'])}科技有限公司，中标金额 {random.randint(20,800):,}0000 元。",
            f"招聘启事（公开）：{random.choice(['后端工程师','算法工程师','测试工程师','产品经理'])}若干名，薪资 "
            f"{random.randint(15,45)}K-{random.randint(46,90)}K·15 薪，要求 {random.randint(3,8)} 年以上经验，简历投递至官网招聘页。",
            f"公开市场数据：{random.choice(['光伏组件','动力电池','工业机器人','服务器'])}行业 2026 年 {random.randint(1,8)} 月出货量 "
            f"{random.randint(8,90):,}000 台，环比{random.choice(['增长','持平'])} {random.randint(2,15)}%，来源为行业协会公开统计。",
            f"高校公开讲座：{random.choice(['量子计算','脑机接口','大模型安全','新能源材料'])}专题报告，主讲教授，"
            f"地点{random.choice(['一号报告厅','学术中心','大礼堂'])}，免费开放，报名链接见官网。",
        ])

    # v3 新增：短良性聊天（含关键词但无 payload）/ 英文技术 / 只谈话题无料 / 短通知
    def short_benign():
        return random.choice([
            "午饭去哪吃，楼下那家湘菜还行",
            "在吗，电话会议改到三点",
            "工资到账了，晚上我请喝奶茶",
            "发薪日是每月几号",
            "电话里说不清，当面聊",
            "登录密码私发你了，注意查收",
            "token过期了，重新登录一下",
            "报价下周会上定",
            "图纸问题找设计部对接人",
            "工资条邮件收到了吗",
            "行程单发我邮箱",
            "简历错别字帮我过一遍",
            "上午的会议室还有空的吗",
            "周五调休吗，怎么算",
            "年终总结写完了，发你看看",
            "绩效考核流程走完了吗",
            "收到offer了，周一入职",
            "分红险介绍发我一份",
            "配筋图集哪本有楼梯详图",
            "切削速度一般怎么选",
            "package.json 冲突了，谁锁的版本",
            "margin 设成 0 auto 还是塌",
            "薪资的事私下聊",
            "联系方式私聊发你",
        ])

    def english_benign():
        return random.choice([
            "please review this PR when you have time",
            "can we delay the deadline to next Friday",
            "base salary negotiation tips for new grads",
            "meeting room is available at 3pm",
            "standup moved to 10am daily",
            "call me when you are free",
            "translate this email to Chinese",
            "sync up tomorrow morning",
            "the drawing file is too large to send",
            "invoice template updated, please use v2",
        ])

    def topic_only():
        return random.choice([
            "会上内容别外传啊",
            "薪资的事私下聊，群里不讲",
            "财务数会上再同步",
            "salary package 的事回头细聊",
            "报价下周定，现在不讲",
            "行程单发我邮箱，别发群",
            "登录密码私发你了",
        ])

    def notice_short():
        return random.choice([
            "放假通知：端午调休安排如下",
            "值班安排见附件，有问题联系行政",
            "考勤统计截止28日，请假走OA",
            "会议通知：周五下午3点开会",
            "团建活动报名截止周三",
        ])

    tpls = [tech_doc, tech_discuss, chitchat, public_doc, public_numbers]
    for t in tpls:
        for _ in range(n_per):
            out.append({"text": t(), "label": 0, "source": "synth", "filename": ""})
    v3n = [short_benign, english_benign, topic_only, notice_short]
    for t in v3n:
        for _ in range(120):
            out.append({"text": t(), "label": 0, "source": "synth_v3", "filename": ""})
    return out


# ---------------------------------------------------------------- 对比对：同 payload × 不同修饰
# 2026-09-24 TorchSight swap 实验证实模型被修饰词劫持：同 SSN+薪资 payload，
# 仅评语 Below Expectations→Needs Improvement，结论 0.00→0.84（换名无影响）。
# 正：同一 payload 配多种包装，label 全 1 —— 逼模型锚定 payload 而非修饰词；
# 负：同包装去 payload（只剩名字+评语），label 0 —— 修饰词本身不定罪。
# 名字/号码全随机生成，与评测集样本不重合（评测集是固定外来文本）。
EN_FIRST = ["James", "Mary", "Robert", "Linda", "Michael", "Sarah", "David", "Emma",
            "Daniel", "Olivia", "Chris", "Anna", "Brian", "Kate", "Steven", "Laura"]
EN_LAST = ["Carter", "Mitchell", "Turner", "Baker", "Foster", "Coleman", "Barker",
           "Fuller", "Sharp", "Douglas", "Hobbs", "Mercer", "Nash", "Pratt"]
EN_RATINGS = ["Below Expectations", "Needs Improvement", "Meets Expectations",
              "Exceeds Expectations", "Outstanding"]
EN_FRAMES = ["PERFORMANCE REVIEW", "Quarterly compensation review", "HR file note",
             "Salary adjustment memo"]
ZH_WRAPS = ["绩效面谈记录", "调薪通知", "年终评定", "部门例会纪要"]
ZH_GRADES = ["待改进", "符合预期", "超出预期", "不合格", "优秀"]


def rnd_en_name():
    return random.choice(EN_FIRST) + " " + random.choice(EN_LAST)


def rnd_ssn():
    area = random.choice([i for i in range(100, 900) if i != 666])
    return f"{area}-{random.randint(10, 99):02d}-{random.randint(1000, 9999):04d}"


def synth_contrast(n_payloads=60):
    pos, neg = [], []
    for _ in range(n_payloads):
        name = rnd_en_name()
        payload = (f"Employee: {name} | ID: EMP-{random.randint(10000, 99999)} | "
                   f"SSN: {rnd_ssn()} | Salary: ${random.randint(60000, 180000):,}")
        for fr in random.sample(EN_FRAMES, 2):
            for rt in random.sample(EN_RATINGS, 3):
                pos.append({"text": f"{fr} | {payload} | Rating: {rt}",
                            "label": 1, "source": "synth_contrast", "filename": ""})
        for rt in random.sample(EN_RATINGS, 2):
            neg.append({"text": f"{random.choice(EN_FRAMES)} | Employee: {name} | "
                                f"Rating: {rt} | No changes this cycle.",
                        "label": 0, "source": "synth_contrast", "filename": ""})
    for _ in range(n_payloads):
        name, dept = rnd_name(), random.choice(DEPTS)
        payload = (f"{name} {dept} 基本工资{rnd_money()} 奖金{rnd_money(1000, 20000)} "
                   f"卡号{rnd_bank_card()}")
        for wp in random.sample(ZH_WRAPS, 2):
            for gd in random.sample(ZH_GRADES, 3):
                pos.append({"text": f"{wp}：{payload}，考核等次{gd}。",
                            "label": 1, "source": "synth_contrast", "filename": ""})
        for gd in random.sample(ZH_GRADES, 2):
            neg.append({"text": f"{random.choice(ZH_WRAPS)}：{name}考核等次{gd}，"
                                f"其余事项下次例会再议。",
                        "label": 0, "source": "synth_contrast", "filename": ""})
    return pos, neg


# ---------------------------------------------------------------- 长表：多行重复表（v6 在 12 行联系表上 1.0→0.09 坍缩，4 行内正常；
# 位置探针排除首尾效应，短表正常——坍缩点在"长重复表"形态，与长度/截断无关）。
# 正：8-14 行联系表/长工资表（含 1200 截断模拟，贴近生产 _call_laya 截断），label 全 1；
# 负：同形态无 payload 行（只有名字+部门/公开分机），label 0。
def rnd_contact_row_en():
    return (f"{rnd_en_name()},{rnd_en_name().replace(' ', '').lower()}{random.randint(1,99)}@example.com,"
            f"({random.randint(200, 989)}){random.randint(200, 989)}-{random.randint(1000, 9999)},"
            f"{random.randint(1, 999)} {random.choice(['Main St', 'Park Ave', 'Lake Rd'])}")


def rnd_contact_row_zh():
    return (f"{rnd_name()} {rnd_phone()} {random.choice(CITIES)}"
            f"{random.choice(STREETS)}")


def synth_tables(n_each=80):
    pos, neg = [], []
    for _ in range(n_each):
        n = random.randint(8, 14)
        t = "name,email,phone,address | " + " | ".join(rnd_contact_row_en() for _ in range(n))
        pos.append({"text": t, "label": 1, "source": "synth_tables", "filename": ""})
        if random.random() < 0.5:
            # 截断模拟：生产按 1200 字切，长表经常半行截断
            pos.append({"text": t[:random.randint(1050, 1200)], "label": 1,
                        "source": "synth_tables", "filename": ""})
    for _ in range(n_each):
        n = random.randint(8, 14)
        t = "通讯录（内部）：\n" + "\n".join(rnd_contact_row_zh() for _ in range(n))
        pos.append({"text": t, "label": 1, "source": "synth_tables", "filename": ""})
    for _ in range(n_each // 2):
        # 同形态无 payload：只有名字+部门/公开总机
        rows = [f"{rnd_en_name()} {random.choice(DEPTS)}" for _ in range(random.randint(8, 14))]
        neg.append({"text": "Directory | " + " | ".join(rows),
                    "label": 0, "source": "synth_tables", "filename": ""})
        rows = [f"{rnd_name()} {random.choice(DEPTS)} 分机{random.randint(8000, 8999)}"
                for _ in range(random.randint(8, 14))]
        neg.append({"text": "内部通讯录（公开分机）：\n" + "\n".join(rows),
                    "label": 0, "source": "synth_tables", "filename": ""})
    return pos, neg


# ---------------------------------------------------------------- 临床叙事：v7 medical 61.5%（心理治疗记录含患者 PII 被判 0.02）。
# 训练集从无临床形态，正：带患者标识符的诊疗叙事 → 1；
# 负：去标识化教学病例 → 0。另补正式财务预览/律师函形态（v7 漏 2 条）。
def rnd_dob():
    return f"{random.randint(1, 12):02d}/{random.randint(1, 28):02d}/{random.randint(1950, 2000)}"


def synth_clinical(n_each=60):
    pos, neg = [], []
    diags_en = ["carotid stenosis", "type 2 diabetes", "major depressive disorder",
                "lumbar disc herniation", "atrial fibrillation"]
    for _ in range(n_each):
        t = (f"OPERATIVE NOTE — CONFIDENTIAL | Patient: {rnd_en_name()} | DOB: {rnd_dob()} | "
             f"MRN: {random.randint(100000, 999999)} | Diagnosis: {random.choice(diags_en)} | "
             f"Procedure performed, blood loss {random.randint(50, 500)} cc.")
        pos.append({"text": t, "label": 1, "source": "synth_clinical", "filename": ""})
    for _ in range(n_each // 2):
        t = (f"Teaching case (de-identified): {random.randint(30, 70)}-year-old "
             f"{random.choice(['male', 'female'])} with {random.choice(diags_en)}, "
             f"standard protocol followed, no complications.")
        neg.append({"text": t, "label": 0, "source": "synth_clinical", "filename": ""})
    diags_zh = ["2 型糖尿病", "腰椎间盘突出", "抑郁发作", "房颤", "颈动脉狭窄"]
    for _ in range(n_each):
        t = (f"入院记录（内部）：姓名{rnd_name()}，住院号ZY{random.randint(100000, 999999)}，"
             f"身份证{rnd_id_card()}，诊断{random.choice(diags_zh)}，"
             f"主诉不适 {random.randint(3, 30)} 天。")
        pos.append({"text": t, "label": 1, "source": "synth_clinical", "filename": ""})
    for _ in range(n_each // 2):
        t = (f"教学查房记录（已去标识化）：中年患者，诊断{random.choice(diags_zh)}，"
             f"按临床路径处理，恢复可。")
        neg.append({"text": t, "label": 0, "source": "synth_clinical", "filename": ""})
    for _ in range(20):
        t = (f"BOARD OF DIRECTORS — CONFIDENTIAL | QUARTERLY FINANCIAL PREVIEW (PRE-RELEASE) | "
             f"Revenue: ${random.randint(50, 500)}M | Net Income: ${random.randint(5, 90)}M | "
             f"Not for distribution.")
        pos.append({"text": t, "label": 1, "source": "synth_clinical", "filename": ""})
        t = (f"ATTORNEY-CLIENT PRIVILEGED COMMUNICATION | DO NOT DISTRIBUTE | "
             f"Re: {random.choice(['acquisition', 'litigation', 'settlement'])} "
             f"amount ${random.randint(100000, 90000000):,} | Counsel: {rnd_en_name()}.")
        pos.append({"text": t, "label": 1, "source": "synth_clinical", "filename": ""})
    return pos, neg


# ---------------------------------------------------------------- 文件名提及（v9：现服务留出 FN2 全是 mention-only，
# 如 `# Files mentioned by the user: ## local_financial_salary.csv` p=0.000 自信放行）。
# 正：机密文件名被提及（无 payload 也标 1，与 taint/会话口径一致）；
# 负：同句式良性文件名（操作手册/公开课/allow 探针），label 0。
SENS_FILES = ["local_financial_salary.csv", "2026年3月工资表.xlsx", "生产过程问题记录追踪表_已同步_A_查询_超链接_最终版.xlsx",
              "2026年度财务决算表.xlsx", "设备采购报价单_含税.xlsx", "研发部花名册_含电话.xlsx", "劳动合同模板_薪资页.docx"]
BENIGN_FILES = ["设备操作手册.docx", "allow_plain_notes.txt", "公开培训课表.xlsx", "面馆推荐清单.txt",
                "CI流水线说明.md", "周会纪要_公开事项.docx"]


def _mention_variants(fname):
    codex = f"# Files mentioned by the user: ## {fname}"
    buddy = f"<user_query>文件路径：E:\\task1\\{fname}，帮我看一下"
    inline = f"帮我看看这个文件：{fname}，{random.choice(['急用', '今天要给领导', '开会前过一遍'])}"
    return [codex, buddy, inline]


def synth_mention(n_each=120):
    pos, neg = [], []
    for _ in range(n_each):
        for t in _mention_variants(random.choice(SENS_FILES)):
            pos.append({"text": t, "label": 1, "source": "synth_mention", "filename": ""})
    for _ in range(n_each):
        for t in _mention_variants(random.choice(BENIGN_FILES)):
            neg.append({"text": t, "label": 0, "source": "synth_mention", "filename": ""})
    return pos, neg


# ---------------------------------------------------------------- 正式中文镜像对（v9：线上影子 18/24 + 留出 FP4，
# CAP 定理 0.96/商务邮件改写 1.0——同一正式话术框，有 payload 标 1、无标 0，逼模型看 payload）。
ZHFORMAL_FRAMES = [
    "请详细介绍{topic}，{req}，字数不少于{words}字",
    "请帮我把下面这句话改写成更礼貌的商务邮件开头，保持原意不变，另外再给两个备选版本。",
    "咨询一下，我们部门{topic}什么时候出？",
    "请分点列出{topic}，{req}，要求尽可能详尽",
]
ZHFORMAL_TOPICS = ["CAP定理的三个特性以及在分布式系统中的应用场景和权衡", "工业机器人谐波减速器的选型要点",
                   "MySQL联合索引的最左前缀原则", "Kubernetes调度器的过滤打分两阶段", "Redis zset做排行榜的最佳实践"]


def synth_zhformal(n_each=120):
    pos, neg = [], []
    for _ in range(n_each):
        fr = random.choice(ZHFORMAL_FRAMES).format(
            topic=random.choice(ZHFORMAL_TOPICS), req=random.choice(["分点说明并给出判断依据", "结合生产案例展开"]),
            words=random.choice([60, 100, 200]))
        neg.append({"text": fr, "label": 0, "source": "synth_zhformal", "filename": ""})
        pay = (f"{rnd_name()} {random.choice(DEPTS)} 基本工资{rnd_money()} 奖金{rnd_money(1000, 20000)} "
               f"卡号{rnd_bank_card()} 电话{rnd_phone()}")
        pos.append({"text": f"{fr}\n相关数据：{pay}", "label": 1, "source": "synth_zhformal", "filename": ""})
    for _ in range(n_each // 3):
        # 灰区薪资闲聊（线上影子 18/24 误伤形态）：无 PII 无 payload → 0；同问句带数字 → 1
        q = random.choice(["这次分红方案什么时候能确定下来？", "今年的年终奖大概什么时候发？",
                           "绩效考核结果什么时候公布？", "咨询一下，我们部门涨薪方案什么时候出？"])
        neg.append({"text": f"灰区闲聊：{q}", "label": 0, "source": "synth_zhformal", "filename": ""})
        pos.append({"text": f"灰区闲聊：{q}顺便看下{random.choice(DEPTS)}{rnd_name()}上月实发{rnd_money()}",
                    "label": 1, "source": "synth_zhformal", "filename": ""})
    return pos, neg


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    real_train, real_eval = extract_real()
    synth_pos = synth_positives()
    synth_neg = synth_negatives()
    cpos, cneg = synth_contrast()
    tpos, tneg = synth_tables()
    clpos, clneg = synth_clinical()
    mpos, mneg = synth_mention()
    zpos, zneg = synth_zhformal()
    train = real_train + synth_pos + synth_neg + cpos + cneg + tpos + tneg + clpos + clneg + mpos + mneg + zpos + zneg
    random.shuffle(train)

    def dump(name, items):
        with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")

    dump("train.jsonl", train)
    dump("real_eval.jsonl", real_eval)
    lp = [len(v["text"]) for v in real_train if v["label"] == 1]
    ln = [len(v["text"]) for v in real_train if v["label"] == 0]
    stats = {
        "seed": SEED,
        "real_train": len(real_train), "real_eval": len(real_eval),
        "real_train_pos": sum(v["label"] for v in real_train),
        "synth_pos": len(synth_pos), "synth_neg": len(synth_neg),
        "synth_contrast_pos": len(cpos), "synth_contrast_neg": len(cneg),
        "synth_tables_pos": len(tpos), "synth_tables_neg": len(tneg),
        "synth_clinical_pos": len(clpos), "synth_clinical_neg": len(clneg),
        "synth_mention_pos": len(mpos), "synth_mention_neg": len(mneg),
        "synth_zhformal_pos": len(zpos), "synth_zhformal_neg": len(zneg),
        "total_train": len(train),
        "train_pos": sum(v["label"] for v in train),
        "real_pos_source": dict(Counter(v["source"] for v in real_train + real_eval if v["label"] == 1)),
        "real_len_p50_pos": statistics.median(lp) if lp else 0,
        "real_len_p50_neg": statistics.median(ln) if ln else 0,
    }
    with open(os.path.join(OUT_DIR, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    print(json.dumps(stats, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
