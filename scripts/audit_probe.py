#!/usr/bin/env python3
"""审计热窗（Redis gateway:audit，5000 条）直查探针——比管理台快、可任意组合过滤。

用法：
  .venv/bin/python scripts/audit_probe.py [关键词] [--action allow|route_local|block]
      [--l2 agree|disagree|main-only] [--limit 20]

  例：scripts/audit_probe.py dualview --l2 agree
      scripts/audit_probe.py --action route_local --limit 5

REDIS_URL 从环境读（source .env 或脚本自己解析 .env）；**不要把带密码的 URL 写死进本文件**。
主影口径与 _audit_shadow_agree/_audit_shadow_mismatch 相同：双方 label 齐全且主判未降级。
"""
import argparse
import json
import os


def _redis_url():
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        # .env 可能重复定义 REDIS_URL（docker 示例行 + 宿主机行）：取**最后**一行，
        # 与 systemd EnvironmentFile / source 的「后者覆盖」语义一致
        for line in open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
                         encoding="utf-8"):
            line = line.strip()
            if line.startswith("REDIS_URL="):
                url = line.split("=", 1)[1].strip()
    if not url:
        raise SystemExit("REDIS_URL 未设置（source .env 后重试）")
    return url


def _l2_cls(e):
    l2 = e.get("l2")
    if not isinstance(l2, dict):
        return "none"
    ml, sl = l2.get("label"), l2.get("shadow_label")
    if l2.get("degraded") or ml not in ("CONFIDENTIAL", "NORMAL") or sl not in ("CONFIDENTIAL", "NORMAL"):
        return "degraded"
    if ml == sl:
        return "agree"
    return "disagree"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("q", nargs="?", default="")
    ap.add_argument("--action", default="")
    ap.add_argument("--l2", default="", choices=["", "agree", "disagree", "main-only", "degraded"])
    ap.add_argument("--limit", type=int, default=20)
    a = ap.parse_args()

    import redis
    r = redis.Redis.from_url(_redis_url(), socket_timeout=3)
    n = r.llen("gateway:audit")
    shown = 0
    for i in range(n):
        if shown >= a.limit:
            break
        item = r.lindex("gateway:audit", i)
        if isinstance(item, bytes):
            item = item.decode()
        try:
            e = json.loads(item)
        except Exception:
            continue
        if a.q and a.q.lower() not in str(e.get("text_preview") or "").lower():
            continue
        if a.action and e.get("action") != a.action:
            continue
        if a.l2 == "main-only":
            if not (isinstance(e.get("l2"), dict) and e["l2"].get("label") and not e["l2"].get("shadow_label")):
                continue
        elif a.l2 and _l2_cls(e) != a.l2:
            continue
        l2 = e.get("l2") if isinstance(e.get("l2"), dict) else None
        l2s = f"主={l2.get('label')} 影={l2.get('shadow_label')}" if l2 else "无l2"
        print(f"{e.get('time')} {e.get('action'):>12} {e.get('rule') or '':<20} {l2s:<26} "
              f"{str(e.get('text_preview') or '')[:40].replace(chr(10), ' ')}")
        shown += 1
    print(f"-- 热窗 {n} 条，命中显示 {shown} 条（limit={a.limit}）--")


if __name__ == "__main__":
    main()
