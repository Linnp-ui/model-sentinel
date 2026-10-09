"""护栏：audit_logs 写入链路（列序对齐 / 批量写 / 丢行计数 / 启动重试）。

历史 bug：requested_model 排在第 14 位而 SQL 里在第 8 位，layer='L1' 落进
local_flag 列，MySQL 报 1366 整行插入失败（见 audit_store._insert_sql 注释）。
l2 列（2026-09-24 加，l2/影子 JSON 落库）使列数 18↔19 随 _has_l2 变化，两种形态都要保序。
"""
import json
import queue
import re
import time

from src.gateway.audit_store import AuditStore, _MySQLWriter

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


# ---------- _flush 批量写（executemany 多行 VALUES + 逐行兜底） ----------

class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.executemany_calls = []
        self.execute_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def executemany(self, sql, rows):
        self.executemany_calls.append(list(rows))
        if self.conn.executemany_error is not None:
            raise self.conn.executemany_error

    def execute(self, sql, row):
        if row[1] == self.conn.fail_req_id:
            raise self.conn.bad_error
        self.execute_calls.append(row)


class _FakeConn:
    def __init__(self):
        self.executemany_error = None
        self.fail_req_id = None
        self.bad_error = Exception("1406 Data too long")
        self.rolled_back = 0
        self.committed = 0
        self.cursor_obj = _FakeCursor(self)

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        pass


def _flush_writer() -> _MySQLWriter:
    w = _writer(True)
    w.cfg = {"host": "h", "port": 3306, "database": "d"}
    w._q = queue.Queue(maxsize=100)
    w._last_error = None
    w._err_count = 0
    w._disabled = False
    w._disabled_since = None
    w._dropped = {"disabled": 0, "queue_full": 0, "build": 0, "insert": 0, "flush": 0}
    return w


def test_insert_sql_matches_pymysql_executemany_rewrite():
    # 模板必须能被 pymysql 的 RE_INSERT_VALUES 命中，否则 executemany
    # 退化成逐行循环，批量写形同虚设
    from pymysql.cursors import RE_INSERT_VALUES
    for has_l2 in (False, True):
        assert RE_INSERT_VALUES.match(_writer(has_l2)._insert_sql())


def test_flush_executemany_multirow_and_commit():
    w = _flush_writer()
    conn = _FakeConn()
    w._connect = lambda: conn
    w._flush([ENTRY, {**ENTRY, "id": "req-2"}])
    assert len(conn.cursor_obj.executemany_calls) == 1
    assert len(conn.cursor_obj.executemany_calls[0]) == 2  # 整批一次，不逐行
    assert conn.cursor_obj.execute_calls == []
    assert conn.committed == 1 and conn.rolled_back == 0
    assert w._last_error is None


def test_flush_fallback_per_row_on_batch_failure():
    # 坏行弄挂整条多行语句 ⇒ 回滚已执行块（防重复）+ 逐行兜底（丢坏行、留好行）
    w = _flush_writer()
    conn = _FakeConn()
    conn.executemany_error = Exception("1406 Data too long")
    conn.fail_req_id = "req-2"
    w._connect = lambda: conn
    w._flush([ENTRY, {**ENTRY, "id": "req-2"}])
    assert conn.rolled_back == 1
    assert len(conn.cursor_obj.execute_calls) == 1
    assert conn.cursor_obj.execute_calls[0][1] == "req-1"  # 好行保留
    assert conn.committed == 1
    assert "insert failed" in w._last_error


# ---------- 丢行计数（fail-open 的代价要可观测） ----------

def test_enqueue_disabled_counts_drop():
    w = _flush_writer()
    w._disabled = True
    w._disabled_since = time.time()  # 距 disable < 重试窗 ⇒ 直接丢
    w.enqueue(ENTRY)
    assert w._dropped["disabled"] == 1
    assert w._q.qsize() == 0


def test_enqueue_queue_full_drops_oldest_and_counts():
    w = _flush_writer()
    w._q = queue.Queue(maxsize=2)
    w._q.put(1)
    w._q.put(2)
    w.enqueue(3)
    assert w._q.qsize() == 2
    assert list(w._q.queue) == [2, 3]  # 最旧一条（1）被丢
    assert w._dropped["queue_full"] == 1


def test_flush_counts_build_and_insert_drops():
    w = _flush_writer()
    conn = _FakeConn()
    conn.executemany_error = Exception("1406 Data too long")
    conn.fail_req_id = "req-2"
    w._connect = lambda: conn
    w._flush([{"time": "garbage"}, ENTRY, {**ENTRY, "id": "req-2"}])
    assert w._dropped["build"] == 1    # time 解析失败 → 建行丢
    assert w._dropped["insert"] == 1   # req-2 逐行插入失败 → 丢
    assert len(conn.cursor_obj.execute_calls) == 1
    assert conn.cursor_obj.execute_calls[0][1] == "req-1"  # 好行保留
    assert conn.committed == 1


def test_flush_connect_failure_counts_flush_drop():
    w = _flush_writer()

    def _dead():
        raise Exception("2003 Can't connect to MySQL server")

    w._connect = _dead
    w._flush([ENTRY, {**ENTRY, "id": "req-2"}])
    assert w._dropped["flush"] == 2  # 连接失败 = 整批丢且不重入
    assert w._err_count == 1
    assert "connect/flush failed" in w._last_error


def test_status_reports_dropped():
    w = _flush_writer()
    w._dropped["build"] = 3
    st = w.status()
    assert st["dropped"]["build"] == 3 and st["dropped"]["total"] == 3


# ---------- 启动重试（启动时 MySQL 不在线 ⇒ 旧代码整个进程生命周期无 writer） ----------

class _FakeProbeConn:
    def close(self):
        pass


def test_audit_store_retries_mysql_after_startup_failure(monkeypatch):
    from src.gateway import audit_store as A

    state = {"n": 0}

    def fake_connect(**kw):
        state["n"] += 1
        if state["n"] == 1:
            raise Exception("2003 Can't connect to MySQL server")
        return _FakeProbeConn()

    monkeypatch.setenv("AUDIT_MYSQL_URL", "mysql://u:p@127.0.0.1:3306/gw_test")
    monkeypatch.setattr(A, "pymysql", type("_FakePymysql", (),
                                            {"connect": staticmethod(fake_connect)})())

    store = object.__new__(A.AuditStore)  # 绕单例，不污染 session 共享 store
    store._init()
    assert store._mysql_want is True
    assert store._mysql_writer is None
    st = store.mysql_status()
    assert st["enabled"] is False
    assert "retrying" in st["reason"] and "not set" not in st["reason"]

    store._try_connect_mysql()  # 模拟重试线程的第二次尝试成功
    assert store._mysql_writer is not None
    assert store.mysql_status()["enabled"] is True
