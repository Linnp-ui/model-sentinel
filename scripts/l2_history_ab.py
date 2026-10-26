#!/usr/bin/env python3
"""读审计库，汇总 history A/B 影子结果（配合 AI_GATEWAY_L2_HISTORY_SHADOW=true）。

用法：source .env && .venv/bin/python scripts/l2_history_ab.py [天数=3]

输出：一致/分歧计数 + 分歧明细（时间/类型/预览/双方标签置信度），供人工裁决
「history 增量拦截」里哪些是真漏报、哪些是转话题后的过度拦截。
"""
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pymysql


def main() -> None:
    days = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    url = (os.getenv("AUDIT_MYSQL_URL") or os.getenv("AUDIT_DATABASE_URL") or "").strip()
    if not url:
        sys.exit("AUDIT_MYSQL_URL 未设置（先 source .env）")
    p = urlparse(url)
    conn = pymysql.connect(host=p.hostname, port=p.port or 3306, user=p.username,
                           password=p.password or "", database=(p.path or "/").lstrip("/"))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ts, type, text_preview, l2 FROM audit_logs "
            "WHERE ts >= %s AND l2 LIKE %s ORDER BY ts",
            (since, "%history_shadow%"))
        rows = cur.fetchall()
    conn.close()

    agg: Counter = Counter()
    disagree = []
    for ts, typ, prev, l2json in rows:
        try:
            l2 = json.loads(l2json)
        except (TypeError, ValueError):
            continue
        hs = l2.get("history_shadow")
        if not isinstance(hs, dict) or "label" not in hs:
            continue
        ml, sl = l2.get("label"), hs.get("label")
        if l2.get("degraded") or hs.get("degraded"):
            agg["degraded"] += 1
            continue
        if ml == sl:
            agg[f"一致({ml})"] += 1
        else:
            agg[f"分歧: 主={ml} / 无history={sl}"] += 1
            disagree.append((ts, typ, (prev or "").replace("\n", " ")[:60],
                             ml, l2.get("confidence"), sl, hs.get("confidence")))

    if not rows:
        print(f"近 {days:g} 天没有带 history_shadow 的审计行（影子 env 开了吗？有流量吗？）")
        return
    print(f"近 {days:g} 天 history 影子样本 {len(rows)} 条：")
    for k, n in sorted(agg.items()):
        print(f"  {k}: {n}")
    print(f"\n分歧明细（{len(disagree)} 条）——逐条人工裁决是真漏报还是过度拦截：")
    for ts, typ, prev, ml, mc, sl, sc in disagree:
        print(f"  {ts} [{typ}] 主={ml}/{mc} 无history={sl}/{sc} | {prev}")


if __name__ == "__main__":
    main()
