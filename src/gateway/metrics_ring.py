"""运行指标快照环：10s 差分 tick → 6h 内存环（spec 2026-09-14-metrics-console-upgrade）。"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlsplit

TICK_S = float(os.getenv("AI_GATEWAY_RING_TICK_S", "10"))
# 24h window needs 24*3600/10 points at the default tick; all window/persist
# cutoffs derive from this constant, so capacity bump is the only change.
RING_POINTS = 8640

# Tail percentiles need far more samples than one tick holds on a quiet
# gateway (nearest-rank P95==P99 for a handful of requests). Ticks still
# record every 10s; only the percentile sample window is wider.
PCT_WINDOW_S = float(os.getenv("AI_GATEWAY_PCT_WINDOW_S", "300"))

# ---- 落盘持久化 -----------------------------------------------------------
# Ring.points 过去是纯内存 deque，一次部署重启（生产走 kill -9，见
# AGENTS.d/03-deploy.md）就把 6h 运行指标清零，表现为「刚灌完流量，
# 部署完窗口只剩几发」。这里每个 tick 追加一行 JSONL、启动时回灌尾部，
# compaction 保证文件不无限增长。
# 约束：uvicorn 单进程（生产 unit 无 --workers）；多进程会互相踩 compaction。
PERSIST_ENV = "AI_GATEWAY_RING_PERSIST"
PATH_ENV = "AI_GATEWAY_RING_PATH"
_PERSIST_OFF = {"0", "off", "false", "no", "none"}


def _persist_enabled() -> bool:
    return (os.getenv(PERSIST_ENV, "on") or "").strip().lower() not in _PERSIST_OFF


def _default_path() -> str:
    return str(Path(__file__).resolve().parents[2] / "var" / "metrics_ring.jsonl")


def _resolve_path(path=None) -> str:
    return str(path or os.getenv(PATH_ENV) or "").strip() or _default_path()


def snapshot_counters(m) -> dict:
    return {
        "v2": dict(m._requests_v2),
        "rate_limited": dict(m._rate_limited),
        "l2_sum": dict(m._l2_sum),
        "l2_total": dict(m._l2_total),
        "l2_skipped": m._l2_skipped_total,
        "l2_sampled": m._l2_sampled_total,
        "l2_degraded": m._l2_degraded_total,
        "override_denied": m._override_denied,
        "fallback": m._fallback_total,
        "gw_internal_sum": m._gw_internal_sum,
        "gw_internal_total": m._gw_internal_total,
        "upstream_sum": m._upstream_sum,
        "upstream_total": m._upstream_total,
        "in_flight": m.in_flight(),
    }


def _clamp(v: int) -> int:
    return v if v > 0 else 0


def derive(prev: dict, cur: dict, elapsed: float, pct: dict, l2_up) -> dict:
    delta: dict = {}
    for k, v in cur["v2"].items():
        d = v - prev["v2"].get(k, 0)
        if d > 0:
            delta[k] = d
    n = sum(delta.values())
    by_type: defaultdict = defaultdict(int)
    cnt_5xx = cnt_403 = cnt_429 = 0
    for (typ, _a, _r, _p, _m, _l, status), d in delta.items():
        by_type[typ] += d
        s = int(status or 0)
        if s >= 500:
            cnt_5xx += d
        elif s == 403:
            cnt_403 += d
        elif s == 429:
            cnt_429 += d
    l2_nd = _clamp(sum(cur["l2_total"].values()) - sum(prev["l2_total"].values()))
    l2_sd = _clamp(sum(cur["l2_sum"].values()) - sum(prev["l2_sum"].values()))
    gw_i_nd = _clamp(cur["gw_internal_total"] - prev["gw_internal_total"])
    gw_i_sd = _clamp(cur["gw_internal_sum"] - prev["gw_internal_sum"])
    up_nd = _clamp(cur["upstream_total"] - prev["upstream_total"])
    up_sd = _clamp(cur["upstream_sum"] - prev["upstream_sum"])
    rl: defaultdict = defaultdict(int)
    for k, v in cur["rate_limited"].items():
        d = v - prev["rate_limited"].get(k, 0)
        if d > 0:
            rl[k[0]] += d
    return {
        "t": int(time.time()),
        "qps_total": round(n / elapsed, 4) if elapsed > 0 else 0.0,
        "qps_by_type": {k: round(v / elapsed, 4) for k, v in by_type.items()} if n else {},
        "err_5xx_rate": (cnt_5xx / n) if n else 0.0,
        "err_403_rate": (cnt_403 / n) if n else 0.0,
        "err_429_rate": (cnt_429 / n) if n else 0.0,
        "p50": pct["p50"], "p95": pct["p95"], "p99": pct["p99"],
        "l2_count": l2_nd,
        "l2_avg_ms": round(l2_sd / l2_nd, 1) if l2_nd else None,
        "l2_skipped": _clamp(cur["l2_skipped"] - prev["l2_skipped"]),
        "l2_sampled": _clamp(cur["l2_sampled"] - prev["l2_sampled"]),
        "l2_degraded": _clamp(cur["l2_degraded"] - prev["l2_degraded"]),
        "rate_limited_by_scope": dict(rl),
        "fallback": _clamp(cur["fallback"] - prev["fallback"]),
        "override_denied": _clamp(cur["override_denied"] - prev["override_denied"]),
        "in_flight": cur["in_flight"],
        "l2_up": l2_up,
        "gw_internal_avg_ms": round(gw_i_sd / gw_i_nd, 1) if gw_i_nd else None,
        "upstream_avg_ms": round(up_sd / up_nd, 1) if up_nd else None,
    }


def _percentiles_for_window(since: str, until: str) -> dict:
    from .admin_store import get_admin_store
    return get_admin_store().latency_percentiles(since, until)


class Ring:
    def __init__(self, tick_s: float = TICK_S, path: str | None = None,
                 persist: bool | None = None):
        self.tick_s = max(1.0, tick_s)
        self.points = deque(maxlen=RING_POINTS)
        self._prev = None
        self._l2_up = None
        self._task = None
        self.persist_path = None
        self.persist_error = ""
        self._disk_lines = 0
        self._fail_streak = 0
        if persist if persist is not None else _persist_enabled():
            p = _resolve_path(path)
            self.persist_path = p
            if not self._load(p):
                self.persist_path = None

    async def probe_l2(self):
        from . import small_model
        if not getattr(small_model, "SMALL_MODEL_ENABLED", True):
            self._l2_up = None
            return
        u = urlsplit(small_model.SMALL_MODEL_URL)
        try:
            import httpx
            # 小模型在内网：必须绕过系统代理（与 small_model._get_client 对齐），
            # 否则装了 HTTP_PROXY 的机器探活会走代理超时，误报 L2 断线。
            async with httpx.AsyncClient(timeout=1.0, trust_env=False) as c:
                r = await c.get(f"{u.scheme}://{u.netloc}/health")
                self._l2_up = r.status_code == 200
        except Exception:
            self._l2_up = False

    def _now(self) -> float:
        return time.time()

    def tick_now(self, m, time_ok) -> None:
        cur = snapshot_counters(m)
        if self._prev is None:
            self._prev = cur
            return
        now = self._now()

        def fmt(ts: float) -> str:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

        try:
            pct = _percentiles_for_window(fmt(now - PCT_WINDOW_S), fmt(now))
        except Exception:
            pct = {"p50": None, "p95": None, "p99": None, "count": 0}
        tick = derive(self._prev, cur, self.tick_s, pct, time_ok)
        tick["t"] = int(now)
        self._insert_gap(now)
        self.points.append(tick)
        self._prev = cur
        self._persist_append(tick)

    def _insert_gap(self, now: float) -> None:
        """停机（重启/长阻塞）后补一个断点，别把停机画成平滑过渡。

        断点只带 t 与 gap 标记、不含任何指标 key —— LineChart 遇 v==null
        提笔断线（web/admin/src/LineChart.jsx:30），前端因此无需改动。
        """
        if not self.points:
            return
        last_t = int(self.points[-1].get("t") or 0)
        if now - last_t <= self.tick_s * 2.5:
            return
        blank = {"t": int(last_t + (now - last_t) / 2), "gap": True}
        self.points.append(blank)
        self._persist_append(blank)

    def _load(self, path: str) -> bool:
        """回灌上次进程留下的环。任何异常都降级为「本次不落盘」——
        指标采集绝不能因为一个日志文件写不进去而中断。"""
        tail: deque = deque(maxlen=RING_POINTS)
        total = 0
        try:
            with open(path, "rb") as f:
                for raw in f:
                    total += 1
                    tail.append(raw)
        except FileNotFoundError:
            # 首次运行，或 path 的父级根本不是目录（Windows 上同为 WinError 3）：
            # 探一下目录建不建得出来，建不出来就当落盘不可用。
            return self._ensure_dir(path)
        except Exception as e:               # noqa: BLE001
            self.note_persist_failure(e)
            return False
        cutoff = int(time.time()) - RING_POINTS * self.tick_s
        for raw in tail:
            try:
                rec = json.loads(raw.decode("utf-8"))
            except Exception:                # noqa: BLE001
                continue                     # kill -9 打断的半行 / 空行
            if isinstance(rec, dict) and isinstance(rec.get("t"), int) and rec["t"] >= cutoff:
                self.points.append(rec)
        self._disk_lines = total
        if total > RING_POINTS * 2:
            self._compact()
        return True

    def _compact(self) -> None:
        """把 JSONL 收敛回环窗口（原子替换，读者不会看到半截文件）。"""
        p = self.persist_path
        if not p:
            return
        tmp = p + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for rec in self.points:
                    f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            os.replace(tmp, p)
            self._disk_lines = len(self.points)
        except Exception as e:               # noqa: BLE001
            self.note_persist_failure(e)

    def _ensure_dir(self, path: str) -> bool:
        d = os.path.dirname(path)
        try:
            if d:
                os.makedirs(d, exist_ok=True)
            return True
        except Exception as e:               # noqa: BLE001
            self.note_persist_failure(e)
            return False

    def _persist_append(self, rec: dict) -> None:
        p = self.persist_path
        if not p:
            return
        if not self._ensure_dir(p):
            return
        try:
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._disk_lines += 1
            self._fail_streak = 0
        except Exception as e:               # noqa: BLE001
            self.note_persist_failure(e)
            return
        if self._disk_lines > RING_POINTS * 2:
            self._compact()

    def note_persist_failure(self, e: Exception) -> None:
        self.persist_error = f"{type(e).__name__}: {e}"
        self._fail_streak += 1
        if self._fail_streak >= 3:
            self.persist_path = None         # 持续故障：停写，别拖慢 tick

    def status(self) -> dict:
        return {"persist": bool(self.persist_path), "path": self.persist_path,
                "points": len(self.points), "disk_lines": self._disk_lines,
                "error": self.persist_error}

    async def loop(self):
        from .metrics import get_metrics
        while True:
            try:
                await self.probe_l2()
                self.tick_now(get_metrics(), self._l2_up)
            except Exception:
                pass
            await asyncio.sleep(self.tick_s)

    def start(self):
        if TICK_S > 0 and self._task is None:
            self._task = asyncio.get_running_loop().create_task(self.loop())


_ring = None


def get_ring() -> Ring:
    global _ring
    if _ring is None:
        _ring = Ring()
    return _ring
