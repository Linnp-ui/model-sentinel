"""时基锚定测试：2026-10-12 宿主 TZ 切 Asia/Shanghai 后，时间写入/窗口/展示必须显式锚定 UTC。

测试在 TZ=Asia/Shanghai 下跑，证明不依赖宿主 TZ：
- 写入（request_log ts 兜底）= UTC 墙钟
- 窗口 cutoff（_window）= UTC 墙钟
- 展示 to_beijing = +8h（UTC 落库 → 北京读出，round-trip 回本地墙钟）
- _parse_window_ts 朴素日期：存储值解析按 UTC 原样，用户输入按北京解释
宿主=UTC 的机器上这些测试同样通过（gmtime==localtime，展示仍 +8），行为不变。
"""
import os
import time
from datetime import datetime, timedelta

FMT = "%Y-%m-%d %H:%M:%S"


def _set_tz(tz):
    os.environ["TZ"] = tz
    time.tzset()


def _restore_tz(old):
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_window_and_request_log_anchored_utc_under_shanghai_tz():
    old = os.environ.get("TZ")
    _set_tz("Asia/Shanghai")
    try:
        from src.gateway import stats_service
        from src.gateway.admin_store import get_admin_store

        # 窗口 cutoff = UTC 墙钟；Shanghai TZ 下本地墙钟 +8h，回退本地必不在此区间
        lo = (datetime.utcnow() - timedelta(seconds=5)).strftime(FMT)
        hi = (datetime.utcnow() + timedelta(seconds=5)).strftime(FMT)
        slo = (datetime.utcnow() - timedelta(hours=1, seconds=5)).strftime(FMT)
        shi = (datetime.utcnow() - timedelta(hours=1, seconds=-5)).strftime(FMT)
        since, until = stats_service._window(1)
        assert slo <= since <= shi
        assert lo <= until <= hi

        # request_log 写入兜底 = UTC；经展示层 +8h 读回 = 北京墙钟 ≈ 本地 now
        store = get_admin_store()
        store.insert_request_log({"key_name": "tz-anchor-iso-test"})
        # 同 session 共享 memory store：按 key_name 精确过滤，别被别的测试行挤出 limit
        res = store.list_request_logs(limit=5, key_name="tz-anchor-iso-test")
        row = res["items"][0]
        shown = datetime.strptime(row["ts"], FMT)
        assert abs((shown - datetime.now()).total_seconds()) < 10
    finally:
        _restore_tz(old)


def test_to_beijing_and_parse_window_ts():
    from src.gateway.main import _parse_window_ts
    from src.gateway.stat_scope import to_beijing

    assert to_beijing("2026-10-12 06:40:45") == "2026-10-12 14:40:45"
    # 存储值解析：朴素串 = UTC 原样
    assert _parse_window_ts("2026-10-12 06:40:45") == datetime(2026, 10, 12, 6, 40, 45)
    # 用户输入：朴素日期按北京解释（-8h 落 UTC）
    assert _parse_window_ts("2026-10-12", naive_as_utc=False) == datetime(2026, 10, 11, 16, 0, 0)
    assert _parse_window_ts("2026-10-12", is_until=True, naive_as_utc=False) == datetime(2026, 10, 12, 15, 59, 59, 999999)
    # ISO 带显式偏移：精确换算 UTC，与 naive_as 无关
    assert _parse_window_ts("2026-10-12T14:40:45+08:00") == datetime(2026, 10, 12, 6, 40, 45)
    assert _parse_window_ts("2026-10-12T06:40:45Z", naive_as_utc=False) == datetime(2026, 10, 12, 6, 40, 45)
    # 相对窗以 UTC 为基准
    rel = _parse_window_ts("1h")
    assert abs((datetime.utcnow() - rel).total_seconds() - 3600) < 5
