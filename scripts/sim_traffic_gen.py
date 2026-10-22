"""模拟真实流量-生成：高温度本地 27B 批量生成贴近真实客户端分布的请求样本。

经网关全链路调用（dev-key + vllm_local/qwen2.5:7b 斜杠写法，内容不出境），
审计自动落地。输出 ~/models/sim-traffic/traffic_<ts>.jsonl（纯合成虚构数据，
不进仓库）。用法: .venv/bin/python scripts/sim_traffic_gen.py [--per-call 8] [--calls 2]
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
sys.path.insert(0, ROOT)

KEY = os.environ["AI_GATEWAY_DEV_API_KEY"]
URL = "https://127.0.0.1:8080/v1/chat/completions"
MODEL = "vllm_local/qwen2.5:7b"
TEMP = 1.15
TOP_P = 0.95
OUT_DIR = os.path.expanduser("~/models/sim-traffic")

# 类名 -> (生成指令, 期望网关路径备注)
CLASSES = {
    "code_qa": ("生成{PERCALL}条程序员日常代码问答（FastAPI/MySQL/Redis/Docker/K8s/前端方向），"
                "每条一句话提问。", "allow"),
    "code_gen": ("生成{PERCALL}条代码生成需求（一句话描述要写什么函数/脚本，含具体需求细节），", "allow"),
    "salary_chat": ("生成{PERCALL}条员工关于薪资奖金分红的闲聊问句（只问时间流程、不带任何具体数字和人名），", "gray"),
    "doc_ask": ("生成{PERCALL}条向同事索要工资表/报价单/图纸/花名册的请求话术（只要文件，不贴内容），", "gray"),
    "payload": ("生成{PERCALL}条带虚构敏感数据的片段（工资奖金数字、报价总价、电话、卡号全部随手编造，"
                "绝不使用真实信息；混工资条/报价单/通讯录/报销单形态），", "route_local"),
    "file_mention": ("生成{PERCALL}条文件提及请求，格式混用下面两种（文件名照抄，后半句编一个具体请求）：\n"
                     "A: # Files mentioned by the user: ## <文件名>\nB: <user_query>文件路径：E:\\task1\\<文件名>，帮我看一下\n"
                     "文件名池：2026年3月工资表.xlsx，设备采购报价单_含税.xlsx，研发部花名册_含电话.xlsx，"
                     "设备操作手册.docx，公开培训课表.xlsx，CI流水线说明.md", "mixed"),
    "short_benign": ("生成{PERCALL}条日常短闲聊（吃饭/开会/请假/团建方向，十个字左右），", "allow"),
    "english": ("生成{PERCALL}条英文技术提问（code review/standup/deadline/drawing file方向，一句话），", "allow"),
}

TAIL = ("要求：每条一行，用“1. ”编号，只输出列表不要解释。前后条之间换说法、换主语、换句式，"
        "越像不同真人随手敲的越好。")


def gen_once(instruction):
    body = json.dumps({
        "model": MODEL,
        "temperature": TEMP,
        "top_p": TOP_P,
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": instruction + TAIL}],
    }).encode()
    req = urllib.request.Request(URL, data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.load(resp)
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def parse_lines(raw):
    out = []
    for line in raw.splitlines():
        m = re.match(r"\s*\d+\s*[.\)、:：]\s*(.+)", line)
        if m and len(m.group(1).strip()) >= 2:
            out.append(m.group(1).strip())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-call", type=int, default=8)
    ap.add_argument("--calls", type=int, default=2)
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUT_DIR, f"traffic_{ts}.jsonl")
    seen, total = set(), 0
    with open(path, "w", encoding="utf-8") as f:
        for name, (instr, path_note) in CLASSES.items():
            got = 0
            for _ in range(args.calls):
                try:
                    texts = parse_lines(gen_once(instr.format(PERCALL=args.per_call)))
                except Exception as e:
                    print(f"[{name}] 调用失败: {e!r}", flush=True)
                    continue
                for t in texts:
                    h = hashlib.sha1(t.encode()).hexdigest()
                    if h in seen:
                        continue
                    seen.add(h)
                    f.write(json.dumps({"class": name, "want_path": path_note,
                                        "text": t, "gen_temp": TEMP},
                                       ensure_ascii=False) + "\n")
                    got += 1
                time.sleep(0.3)
            total += got
            print(f"[{name}] +{got}（期望走{path_note}）", flush=True)
    print(f"共 {total} 条 -> {path}")


if __name__ == "__main__":
    main()
