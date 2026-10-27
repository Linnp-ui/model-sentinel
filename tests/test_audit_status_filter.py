"""审计日志状态码筛选（_audit_filter_rows status=）：类与精确值口径。"""

from src.gateway.main import _audit_filter_rows, _entry_status


def _rows():
    return [
        {"action": "allow", "provider": "deepseek", "model": "m1"},                           # 200（反推）
        {"action": "allow", "provider": "deepseek", "model": "m2", "upstream_error": 502},    # 失败行
        {"action": "route_local", "provider": "vllm_local", "model": "m3"},                   # 200（反推）
        {"action": "block", "provider": "deepseek", "model": "m4", "upstream_error": 403},
    ]


def test_entry_status_prefers_recorded_error_over_action_infer():
    rows = _rows()
    assert _entry_status(rows[0]) == 200
    assert _entry_status(rows[1]) == 502
    assert _entry_status(rows[3]) == 403


def test_status_class_and_exact():
    rows = _rows()
    by = lambda rs: sorted(r["model"] for r in rs)
    assert by(_audit_filter_rows(rows, status="5xx")) == ["m2"]
    assert by(_audit_filter_rows(rows, status="4xx")) == ["m4"]
    assert by(_audit_filter_rows(rows, status="403")) == ["m4"]
    assert by(_audit_filter_rows(rows, status="200")) == ["m1", "m3"]
    # 未知文本 = 不筛
    assert by(_audit_filter_rows(rows, status="x")) == by(rows)
