"""影子脉冲：l2-shadow-stats 窗口汇总 + nc/cn 分歧样本清单。

用法: .venv/bin/python scripts/shadow_pulse.py [hours=168] [--since[=<UTC ts>]]
  --since       取 var/l2-round.json 的轮次起点（灰区灌数那轮，"本轮灌数"窗）
  --since=...   显式轮次窗起点（"YYYY-MM-DD HH:MM:SS" UTC，gray_flood 写的那个格式）
  给了 since 就优先于 hours（与管理台轮次切窗同口径，见 admin_api._cutoff）。
汇总走 /admin/api/l2-shadow-stats（读 Redis 热缓存近 5000 条）；
分歧明细走 audit_logs（MySQL，ts 存 UTC——窗口 cutoff 用 datetime.utcnow()，
别用 SQL NOW()（北京时间），窗口条件会恒空，见 agents/gotchas.md）。
nc=主放影密（laya 更严），cn=主密影放（主过判密）；主判定 degraded 不可比，排除。
"""
import base64
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

import pymysql  # noqa: E402

STATS_URL = "http://127.0.0.1:8080/admin/api/l2-shadow-stats"
DIVERGENCE_LIMIT = 40
ROUND_FILE = os.path.join(ROOT, "var", "l2-round.json")


def _window_qs(hours: int, since: str = "") -> str:
    return f"&since={urllib.parse.quote(since)}" if since else f"hours={hours}"


def stats(hours: int, since: str = "") -> dict:
    cred = base64.b64encode(f"admin:{os.environ['AI_GATEWAY_ADMIN_PASSWORD']}".encode()).decode()
    req = urllib.request.Request(f"{STATS_URL}?{_window_qs(hours, since)}",
                                 headers={"Authorization": f"Basic {cred}"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def divergences(hours: int, since: str = ""):
    u = urllib.parse.urlparse(os.environ["AUDIT_MYSQL_URL"])
    conn = pymysql.connect(host=u.hostname, port=u.port or 3306, user=u.username,
                           password=u.password, database=u.path.lstrip("/"))
    cutoff = since or (datetime.now(timezone.utc).replace(tzinfo=None)
                       - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    out = []
    with conn.cursor() as cur:
        cur.execute("SELECT ts, text_preview, l2 FROM audit_logs "
                    "WHERE l2 IS NOT NULL AND ts >= %s ORDER BY ts DESC", (cutoff,))
        for ts, preview, l2 in cur.fetchall():
            try:
                d = json.loads(l2) if isinstance(l2, str) else (l2 or {})
            except Exception:
                continue
            sl, ml = d.get("shadow_label"), d.get("label")
            if sl not in ("CONFIDENTIAL", "NORMAL") or d.get("degraded") or ml == sl:
                continue
            sp = (d.get("shadow_probabilities") or {}).get("CONFIDENTIAL")
            out.append(("nc" if ml == "NORMAL" else "cn", str(ts), preview or "", sp))
            if len(out) >= DIVERGENCE_LIMIT:
                break
    conn.close()
    return out


def main():
    hours, since = 168, ""
    for a in sys.argv[1:]:
        if a.startswith("--since"):
            since = a.split("=", 1)[1] if "=" in a else json.load(
                open(ROUND_FILE, encoding="utf-8"))["since"]  # 裸 --since = 本轮灌数窗
        else:
            try:
                hours = max(1, min(int(a), 720))
            except ValueError:
                pass
    s = stats(hours, since)
    cells = s["cells"]
    cov = f"{s['shadowed'] / s['l2_total']:.0%}" if s["l2_total"] else "-"
    print(f"影子脉冲 {since or f'{hours}h'}（Redis 缓存 scanned={s['scanned']}）")
    print(f"  l2_total={s['l2_total']}  shadowed={s['shadowed']}  覆盖率={cov}  "
          f"degraded: main={s['main_degraded']} shadow={s['shadow_degraded']}")
    ar = s["agree_rate"]
    print(f"  一致率={ar if ar is not None else '-'}  (compared={s['compared']} agree={s['agree']})  "
          f"主延迟avg={s['avg_main_latency_ms']}ms")
    print(f"  cells: cc={cells['cc']} cn={cells['cn']} nc={cells['nc']} nn={cells['nn']}")
    div = divergences(hours, since)
    print(f"\n分歧样本 {len(div)} 条（{'截断' if len(div) >= DIVERGENCE_LIMIT else '全量'}，ts=UTC）")
    for cell, ts, preview, sp in div:
        tag = "nc 主放影密" if cell == "nc" else "cn 主密影放"
        print(f"  [{tag}] {ts}  p_conf={sp}  {(preview or '')[:60]!r}")


if __name__ == "__main__":
    main()
