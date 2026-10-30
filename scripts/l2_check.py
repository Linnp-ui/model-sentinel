#!/usr/bin/env python3
"""影子候选体检一条命令：训练报告三门 + OOD 分布外对照（候选 vs 现役 vs qwen 权威）。

    .venv/bin/python scripts/l2_check.py [版本号]

不带参数 = 取盘上版本号最大的 checkpoint（通常就是刚训完那个）。做四件事：
  1. 打印 var/l2-reports/vN.json 的三门（battery / 标注回归 / 分歧回归）+ 相对上一版 diff
  2. 起 8004 临时服务加载候选（**显式 PID**，别用 pgrep）
  3. 候选与现役各跑一遍 scripts/eval_ood_holdout.py（220 条分布外），并排对比
  4. 收尾 kill 8004（try/finally，崩了也不留进程占 GPU5）

**比较先看漏报**：want=1 判 NORMAL 是漏报（安全方向），want=0 判 CONFIDENTIAL 是误报
（只是烦，不出境）。误报可接受、漏报不可接受——所以表里两列分开列，不只看准确率。
不训练、不晋升：训练走 POST /admin/api/l2/retrain，晋升要人拍板。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))  # qwen 权威端点（OOD 脚本要）

MODELS = os.path.expanduser("~/models")
LAYA_PY = os.path.expanduser("~/venvs/laya/bin/python")
LIVE_URL = "http://127.0.0.1:8003"
PORT = int(os.getenv("AI_GATEWAY_L2_RETRAIN_EVAL_PORT", "8004"))
TRAIN_GPU = os.getenv("AI_GATEWAY_L2_TRAIN_GPU", "5")
EVAL_DIR = os.path.join(ROOT, "var", "eval")
REPORT_DIR = os.path.join(ROOT, "var", "l2-reports")


def _get(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def live_version() -> str:
    try:
        return str(_get(f"{LIVE_URL}/health").get("model", "")).strip()
    except Exception:
        return ""


def resolve_candidate(arg: str, live: str) -> str:
    if arg:
        return arg if arg.startswith("/") else f"{MODELS}/laya-finetuned-confidential-{arg}"
    cands = []
    for d in os.listdir(MODELS):
        m = re.fullmatch(r"laya-finetuned-confidential-v(\d+)", d)
        if m and d != os.path.basename(live):
            cands.append((int(m.group(1)), os.path.join(MODELS, d)))
    if not cands:
        sys.exit("盘上没有候选 checkpoint（除现役外）")
    return max(cands)[1]


def print_gates(ckpt: str) -> None:
    v = os.path.basename(ckpt).rsplit("-", 1)[-1]
    path = os.path.join(REPORT_DIR, f"{v}.json")
    if not os.path.exists(path):
        print(f"（无训练报告 {path} —— 只跑 OOD 对照）")
        return
    with open(path, encoding="utf-8") as f:
        blob = json.load(f)
    r = blob.get("report") or blob  # 落盘格式 {"version","ts","report":{...}}
    b = r.get("battery") or {}
    lb = r.get("labeled") or {}
    dg = r.get("divergences") or {}
    cats = " ".join(f"{k}={v2['correct']}/{v2['total']}" for k, v2 in (b.get("cats") or {}).items())
    print(f"[三门] battery {b.get('correct')}/{b.get('total')}  {cats}")
    print(f"       标注回归 {lb.get('matched')}/{lb.get('total')}   分歧回归 {dg.get('matched')}/{dg.get('total')}")
    reg = r.get("regressions") or {}
    print(f"       相对 v{reg.get('against')}: 新退 {reg.get('new')}  修好 {reg.get('fixed')}")


def start_8004(ckpt: str):
    env = {**os.environ, "LAYA_MODEL_PATH": ckpt, "LAYA_PORT": str(PORT),
           "CUDA_VISIBLE_DEVICES": TRAIN_GPU}
    name = os.path.basename(ckpt)
    proc = subprocess.Popen([LAYA_PY, "scripts/laya_l2_server.py"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(5)
        try:
            if name in str(_get(f"http://127.0.0.1:{PORT}/health", 3).get("model", "")):
                return proc
        except Exception:
            pass
        if proc.poll() is not None:
            sys.exit(f"8004 候选服务启动即退出（rc={proc.returncode}）")
    proc.kill()
    sys.exit("8004 候选服务 5 分钟未就绪")


def run_ood(ckpt_label: str) -> dict:
    """跑一遍 OOD 评测，返回它落的 JSON（按 mtime 认最新的那个）。"""
    url = LIVE_URL if ckpt_label == "live" else f"http://127.0.0.1:{PORT}"
    before = set(os.listdir(EVAL_DIR)) if os.path.isdir(EVAL_DIR) else set()
    env = {**os.environ, "OOD_L2_URL": f"{url}/classify"}
    print(f"  · OOD 评测 {ckpt_label} @ {url} ...", flush=True)
    subprocess.run([os.path.join(ROOT, ".venv/bin/python"), "scripts/eval_ood_holdout.py"],
                   cwd=ROOT, env=env, stdout=subprocess.DEVNULL, check=True)
    files = [f for f in os.listdir(EVAL_DIR) if f.startswith("ood-") and f.endswith(".json")]
    new = [f for f in files if f not in before] or files
    with open(os.path.join(EVAL_DIR, max(new, key=lambda f: os.path.getmtime(
            os.path.join(EVAL_DIR, f)))), encoding="utf-8") as f:
        return json.load(f)


def _score(rows: list, want_key: str) -> dict:
    out = {}
    for r in rows:
        got = 1 if r["laya_p"] >= 0.7 else 0
        g = out.setdefault(r["group"], {"n": 0, "ok": 0, "miss": 0, "false": 0})
        g["n"] += 1
        if got == r[want_key]:
            g["ok"] += 1
        elif r[want_key] == 1:
            g["miss"] += 1
        else:
            g["false"] += 1
    return out


def compare(cand: dict, live: dict | None) -> None:
    qwen = _score(cand["cases"], "want")
    cq, lq = _score(cand["cases"], "want"), (_score(live["cases"], "want") if live else {})
    groups = list(dict.fromkeys(list(cq) + list(lq)))
    thr = cand.get("threshold", 0.7)
    print(f"\n{'组':<11}{'n':>4}   {'候选':>13}   {'现役':>13}   {'qwen权威':>13}")
    print("-" * 62)
    for g in groups:
        c, l = cq.get(g), lq.get(g)
        cells = []
        for s in (c, l):
            cells.append(f"{s['ok']}/{s['n']} 漏{s['miss']}误{s['false']}" if s else "-")
        q = qwen.get(g)
        print(f"{g:<11}{q['n']:>4}   {cells[0]:>13}   {cells[1]:>13}   "
              f"{q['ok']}/{q['n']} 漏{q['miss']}误{q['false']}".rstrip())
    tot = lambda s: (sum(x["ok"] for x in s.values()), sum(x["n"] for x in s.values()),
                     sum(x["miss"] for x in s.values()), sum(x["false"] for x in s.values()))
    co, cn, cm, cf = tot(cq)
    qo = sum(x["ok"] for x in qwen.values())
    print("-" * 62)
    if live:
        lo, ln, lm, lf = tot(lq)
        cand_s = f"{co}/{cn} 漏{cm}误{cf}"
        live_s = f"{lo}/{ln} 漏{lm}误{lf}"
        print(f"{'合计':<11}{cn:>4}   {cand_s:>13}   {live_s:>13}   {f'{qo}/{cn}':>13}")
        print(f"  漏报变化 {cm - lm:+d}（负=候选更好）  误报变化 {cf - lf:+d}")
    else:
        print(f"{'合计':<11}{cn:>4}   {co}/{cn} 漏{cm}误{cf}   {f'{qo}/{cn}':>13}")
    print(f"  阈值 {thr}；漏报=want1判放行(危险) 误报=want0判密(仅烦)")
    errs = [r for r in cand["cases"] if (1 if r["laya_p"] >= thr else 0) != r["want"]]
    if errs:
        print(f"\n候选错题 {len(errs)} 条：")
        for r in sorted(errs, key=lambda x: (x["group"], x["laya_p"])):
            kind = "漏报" if r["want"] == 1 else "误报"
            print(f"  [{kind}] {r['group']:<11} p={r['laya_p']:.3f} qwen={r['qwen_p']} "
                  f"| {r['text'][:52]!r}")


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    live = live_version()
    ckpt = resolve_candidate(arg, live)
    print(f"现役 {os.path.basename(live) or '(8003 无响应)'}   候选 {os.path.basename(ckpt)}")
    if os.path.basename(ckpt) == os.path.basename(live):
        print("候选=现役，对比无意义（要量某版请显式给版本号）")
        return 1
    print_gates(ckpt)
    proc = start_8004(ckpt)
    try:
        cand = run_ood("cand")
        live_rows = run_ood("live") if live else None
    finally:
        proc.kill()  # 显式 PID；绝不 pgrep -f（会命中 8003 生产服务）
        time.sleep(3)
    compare(cand, live_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
