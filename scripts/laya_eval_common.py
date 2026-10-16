"""L2 评测脚本公共件（eval_laya_simulated / eval_laya_confidential /
eval_laya_live / eval_ood_holdout / eval_security_testset 的去重收口）。

只收三样（都是逐字节验过一致的）：
- Q：laya noul 判定题（simulated 与 confidential 原文一致）
- PROBES：8 例探针（confidential 与 live 仅差一个空行）
- classify_p：POST /classify → CONFIDENTIAL 概率（ood _laya_p /
  security l2_classify / live cls 三份拷贝的并集；异常直接抛，
  需要 (None, err) 口径的调用方自己包 try）
- THRESH：0.7 判定口径（五处硬编码统一到这里；0.5 只是报告阈值，各留原处）

render 文本形态各脚本不同（模型输入，动了会扰动分数），故意不收。
独立脚本直接跑时 scripts/ 自动在 sys.path 上，`import laya_eval_common` 即可。
"""
import json
import urllib.request

THRESH = 0.7
DEFAULT_URL = "http://127.0.0.1:8003/classify"

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


def classify_p(text, filename="", url=None, timeout=30):
    """打一次 /classify，返回 CONFIDENTIAL 概率（float）。异常直接抛。"""
    body = json.dumps({"text": text, "filename": filename}).encode()
    req = urllib.request.Request(url or DEFAULT_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return float(json.load(r)["probabilities"]["CONFIDENTIAL"])
