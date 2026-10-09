#!/usr/bin/env python3
"""一次性回填：Redis 热窗里有、audit_logs 里缺的审计行（2026-09-01~09-11
writer 启动无重试静默丢失的 598 条）补进 MySQL。

幂等：先 SELECT 全部已有 req_id，只插缺的；重跑插 0 条。
列映射走运行中 writer 的 _row()/_insert_sql()（含 l2 列同条件），保证与
日常双写逐位一致。默认 dry-run，加 --apply 才写。

用法：source .env && .venv/bin/python scripts/backfill_audit_redis.py [--apply]
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.gateway.audit_store import get_audit_store


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写入；缺省 dry-run 只报数")
    args = ap.parse_args()

    store = get_audit_store()
    w = store._mysql_writer
    if w is None:
        sys.exit("MySQL writer 未连接，拒绝执行（先确认 AUDIT_MYSQL_URL / 服务健康）")
    w._ensure_l2_column()  # 懒自检：新进程还没 flush 过时 _has_l2 未置位
    if not w._has_l2:
        sys.exit("l2 列未自检到，拒绝执行（避免列值数不配 1136）")

    entries = store.tail(5000) or []
    import pymysql
    conn = pymysql.connect(
        host=w.cfg["host"], port=w.cfg["port"], user=w.cfg["user"],
        password=w.cfg["password"], database=w.cfg["database"],
        connect_timeout=3, read_timeout=10, write_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT req_id FROM audit_logs")
            have = {x[0] for x in cur.fetchall()}
        todo, skipped = [], 0
        for e in entries:
            rid = str(e.get("id") or "")[:32]
            if not rid or rid in have:
                continue
            r = w._row(e)
            if r is None:
                skipped += 1
                continue
            todo.append(r)
        if not todo:
            print(f"热窗 {len(entries)} 条全部已在审计库，无需回填（跳过不可转换 {skipped}）")
            return
        msg = f"缺 {len(todo)} 条（跳过不可转换 {skipped}）"
        if not args.apply:
            print(msg + "，dry-run 未写入；确认无误加 --apply 重跑")
            return
        with conn.cursor() as cur:
            cur.executemany(w._insert_sql(), todo)
        conn.commit()
        print(msg + f"，已写入 {len(todo)} 条")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
