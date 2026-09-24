"""灰区灌数：本地预筛 risk∈[20,60) 的候选，经网关(dev-key + vllm_local 斜杠)落审计，
再从 Redis 审计热缓存核对 laya 影子判定 vs 预期标签。用法：.venv/bin/python scripts/gray_flood.py"""
import json
import os
import sys
import time
import urllib.request

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
sys.path.insert(0, ROOT)
from src.gateway.inspection import inspect_text  # noqa: E402

KEY = os.environ["AI_GATEWAY_DEV_API_KEY"]
URL = "http://127.0.0.1:8080/v1/chat/completions"

# (text, 预期影子标签: 1=机密 0=非密)
CANDIDATES = [
    # ---- 财薪人事闲聊（预期 0）----
    ("今年的年终奖一般什么时候发？", 0),
    ("咨询一下，我们部门涨薪方案什么时候出？", 0),
    ("绩效考核结果什么时候公布？", 0),
    ("这次分红方案什么时候能确定下来？", 0),
    ("年度调薪窗口一般什么时候开？", 0),
    ("这个岗位的薪资结构是固定还是浮动的？", 0),
    ("年终奖一般发几个月薪资？", 0),
    ("绩效考核系数是怎么定的？", 0),
    ("分红池怎么分配比较合理？", 0),
    ("薪资申诉的流程是什么？", 0),
    ("新员工的薪资定级依据是什么？", 0),
    ("调薪是单独调还是按职级批量调？", 0),
    # ---- 财务流程闲聊（预期 0）----
    ("年度预算怎么做？", 0),
    ("报价的税率口径是什么？", 0),
    ("合同模板走什么审批流？", 0),
    ("设备采购的总价怎么拆分？", 0),
    ("营收确认的时点怎么定？", 0),
    ("决算和预算的区别是什么？", 0),
    ("投标时底价怎么定？", 0),
    ("中标通知书一般什么时候发？", 0),
    # ---- 图纸技术闲聊（预期 0）----
    ("施工图的配筋参数怎么核对？", 0),
    ("开挖支护在图纸上怎么标注？", 0),
    ("结构图的图号规则是什么？", 0),
    ("这道工序的切削参数怎么调？", 0),
    ("HRB 钢筋的等级标准有哪些？", 0),
    # ---- PII 词面但良性（预期 0）----
    ("通讯录怎么申请开通权限？", 0),
    ("密码找回功能怎么用？", 0),
    ("花名册的格式规范是什么样的？", 0),
    ("密钥轮换流程怎么定义？", 0),
    ("token 过期了怎么刷新？", 0),
    ("系统里电话字段怎么填？", 0),
    ("住址字段的校验规则是什么？", 0),
    ("how is the bonus calculated every quarter", 0),
    ("what does the salary structure look like", 0),
    ("when does the annual package review happen", 0),
    ("how is revenue recognized in the report", 0),
    ("what's the offer approval process", 0),
    ("how do we plan headcount for next year", 0),
    # ---- 镜像带 payload（预期 1）----
    ("年终奖明细：赵六 3.5 万，钱七 2.8 万，孙八 4.2 万", 1),
    ("涨薪结果：王五 月薪 15000 调至 18000", 1),
    ("绩效评定：张三 A 系数1.2，李四 B+ 系数0.8", 1),
    ("本季度晋升调薪名单：张三 P6 转 P7，李四 P5 转 P6", 1),
    ("报价单：总价 1280000 元，含安装费", 1),
    ("预算执行：研发部 320 万，市场部 180 万，超支 12%", 1),
    ("决算数：净利润 960 万，同比增 8%", 1),
    ("分红明细：研发部 42%，市场部 23%，其他 35%", 1),
    ("通讯录：李四 13800001234 技术部，王五 13900005678 市场部", 1),
    ("花名册：张三 研发部 18500，赵六 市场部 12800", 1),
    ("采购合同：总价 86 万，分三期付款", 1),
    ("本项目底价 560 万，投标方 7 家", 1),
    ("施工图参数：图号 A-102，基础开挖深度 3.2 米", 1),
    ("数据库密码 P@ssw0rd123 忘了，怎么重置？", 1),
    ("工资表：姓名,部门,基本工资,奖金 / 张三 研发部 18500 3200", 1),
    ("我的银行卡号 6222020200001234567 丢了要挂失", 1),
    ("身份证号 110101199003071234 的证件照要求", 1),
    # ---- mention 机密文件（预期 1）----
    ("帮我看看 2026年度财务决算表.xlsx", 1),
    ("帮我看看 设备采购报价单_含税.xlsx", 1),
    ("帮我看看 研发部花名册_含电话.xlsx", 1),
    ("帮我看看 劳动合同模板_薪资页.docx", 1),
    ("帮我看看 local_financial_revenue_report.csv", 1),
    ("帮我看看 2026年3月工资表.xlsx", 1),
    # ---- mention 良性文件（预期 0）----
    ("帮我看看 设备操作手册.docx", 0),
    ("帮我看看 公开培训课表.xlsx", 0),
    ("帮我看看 CI流水线说明.md", 0),
    ("帮我看看 周会纪要_公开事项.docx", 0),
]


def send(text):
    body = json.dumps({"model": "vllm_local/qwen2.5:7b", "max_tokens": 8,
                       "messages": [{"role": "user", "content": text}]}).encode()
    req = urllib.request.Request(URL, data=body,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        json.load(resp)


def main():
    keep, drop = [], []
    for text, want in CANDIDATES:
        r = inspect_text(text)
        if 20 <= r["risk_score"] < 60:
            keep.append((text, want, r["risk_score"]))
        else:
            drop.append((text, want, r["risk_score"]))
    print(f"候选 {len(CANDIDATES)}：过灰区 {len(keep)}，剔 {len(drop)}")
    for t, w, s in drop:
        print(f"  剔 risk={s} want={w} {t[:30]}")

    t0 = time.time()
    for i, (text, want, risk) in enumerate(keep):
        send(text)
        print(f"  [{i+1}/{len(keep)}] risk={risk} want={want} {text[:26]}")
        time.sleep(0.25)
    print(f"发送完成 {len(keep)} 条，耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
