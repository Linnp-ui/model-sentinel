"""护栏：audit_logs INSERT 列序必须与 _row() 返回值逐位对齐。

历史 bug：requested_model 排在第 14 位而 SQL 里在第 8 位，layer='L1' 落进
local_flag 列，MySQL 报 1366 整行插入失败（见 audit_store._insert_sql 注释）。
l2 列（2026-09-24 加，l2/影子 JSON 落库）使列数 18↔19 随 _has_l2 变化，两种形态都要保序。
"""
import json
import re

from src.gateway.audit_store import _MySQLWriter

EXPECTED_COLS = ["ts", "req_id", "type", "action", "rule", "provider", "model",
                 "requested_model", "local_flag", "layer", "downgraded_from",
                 "override_denied", "text_preview", "findings_json", "client_ip",
                 "token_masked", "filename", "risk_score"]

ENTRY = {
    "time": "2026-09-24 08:00:00", "id": "req-1", "type": "chat", "action": "allow",
    "rule": "r1", "provider": "p", "model": "m", "requested_model": "rm", "local": 1,
    "layer": "L2", "downgraded_from": "", "override_denied": 0, "text_preview": "t",
    "findings": {"a": 1}, "client_ip": "1.2.3.4", "token_masked": "tk",
    "filename": "f.xlsx", "risk_score": 20,
    "l2": {"label": "NORMAL", "shadow_label": "CONFIDENTIAL", "shadow_conf": 0.9},
}


def _writer(has_l2: bool) -> _MySQLWriter:
    w = _MySQLWriter.__new__(_MySQLWriter)  # 不起后台线程，只测纯建行逻辑
    w._has_l2 = has_l2
    return w


def _parse(sql: str):
    m = re.search(r"INSERT INTO audit_logs \((.*?)\)\s*VALUES\s*\((.*)\)", sql, re.S)
    assert m, sql
    cols = [c.strip() for c in m.group(1).split(",")]
    ph = [p.strip() for p in m.group(2).split(",")]
    return cols, ph


def test_mysql_writer_column_order_matches_row_values():
    for has_l2 in (False, True):
        w = _writer(has_l2)
        cols, ph = _parse(w._insert_sql())
        row = w._row(ENTRY)
        assert row is not None
        assert cols == EXPECTED_COLS + (["l2"] if has_l2 else [])
        assert len(ph) == len(cols) == len(row)


def test_row_serializes_l2_json():
    row = _writer(True)._row(ENTRY)
    assert json.loads(row[-1])["shadow_label"] == "CONFIDENTIAL"
    # l2 非 dict（缺/None）→ NULL，不写垃圾
    assert _writer(True)._row({**ENTRY, "l2": None})[-1] is None
    assert _writer(True)._row({k: v for k, v in ENTRY.items() if k != "l2"})[-1] is None
