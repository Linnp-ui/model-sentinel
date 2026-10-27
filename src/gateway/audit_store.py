"""Add MySQL backend to audit_store.py while preserving Redis hot cache.

Strategy: keep AuditStore as the public API. Add a MySQL writer thread that
drains a queue and writes async, fail-open. /admin/csv can pull from MySQL
with time-range filters; tail(n) still uses Redis cache for speed.
"""
import json
import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional
from urllib.parse import urlsplit

try:
    import redis
except ImportError:
    redis = None

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:
    pymysql = None

REDIS_KEY = "gateway:audit"
MAX_ENTRIES = 5000  # Redis hot cache cap
MYSQL_QUEUE_MAX = 10000  # in-memory queue cap before drop-oldest
MYSQL_BATCH_SIZE = 100  # rows per INSERT
MYSQL_FLUSH_INTERVAL = 1.0  # seconds
REDIS_RETRY_INTERVAL = 30.0  # seconds, auto-recover Redis after failure
MYSQL_RETRY_INTERVAL = 60.0  # seconds, auto-recover MySQL after 3 failures


def _parse_mysql_url(url: str) -> Optional[dict]:
    """mysql://user:pass@host:port/db or mysql+pymysql://...（必须带 user:pass）"""
    if not url:
        return None
    u = urlsplit(url.strip())
    if u.scheme not in ("mysql", "mysql+pymysql") or not u.hostname or not u.username:
        return None
    return {"host": u.hostname, "port": u.port or 3306,
            "user": u.username, "password": u.password or "",
            "database": (u.path or "").lstrip("/").split("?")[0]}


class _MySQLWriter:
    """Background thread that batches audit writes to MySQL. Fail-open."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._q: queue.Queue = queue.Queue(maxsize=MYSQL_QUEUE_MAX)
        self._stop = threading.Event()
        self._disabled = False
        self._disabled_since: Optional[float] = None
        self._err_count = 0
        self._thread: Optional[threading.Thread] = None
        self._last_error: Optional[str] = None
        self._has_l2 = False  # audit_logs.l2 列存在性（l2/影子 JSON 落库，见 _ensure_l2_column）
        self._l2_ensure_ts = 0.0
        # 丢行计数：fail-open 的代价要可观测。每个键只被单一线程写
        # （disabled/queue_full ← enqueue 所在线程；build/insert/flush ← writer 线程）
        self._dropped = {"disabled": 0, "queue_full": 0, "build": 0, "insert": 0, "flush": 0}
        self._start()

    def _start(self):
        self._thread = threading.Thread(target=self._run, name="audit-mysql-writer", daemon=True)
        self._thread.start()

    def enqueue(self, entry: dict) -> None:
        if self._disabled:
            if self._disabled_since is not None and (time.time() - self._disabled_since) >= MYSQL_RETRY_INTERVAL:
                self._disabled = False
                self._err_count = 0
                self._disabled_since = None
            else:
                self._dropped["disabled"] += 1
                return
        # Note: don't bail on _stop.is_set() — caller may want final flush
        try:
            self._q.put_nowait(entry)
        except queue.Full:
            # Drop oldest by getting then putting
            try:
                self._q.get_nowait()
                self._q.put_nowait(entry)
                self._dropped["queue_full"] += 1
            except Exception:
                pass

    def _connect(self):
        return pymysql.connect(
            host=self.cfg["host"],
            port=self.cfg["port"],
            user=self.cfg["user"],
            password=self.cfg["password"],
            database=self.cfg["database"],
            connect_timeout=3,
            read_timeout=5,
            write_timeout=5,
            autocommit=False,
        )

    def _ensure_l2_column(self) -> None:
        """audit_logs.l2 列自检：缺则 ALTER 补上（MySQL 无 ADD COLUMN IF NOT EXISTS）。

        l2（主判 + 影子 shadow_*）此前只落 Redis 热缓存（5000 条 ≈2 天），超窗即丢，
        影子统计/训练原料无从回看。60s 节流重试；失败保持 _has_l2=False 继续按
        无 l2 列写（fail-open，不拖垮主写入）。"""
        now = time.time()
        if self._has_l2 or now - self._l2_ensure_ts < 60:
            return
        self._l2_ensure_ts = now
        try:
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='audit_logs' AND COLUMN_NAME='l2'",
                        (self.cfg["database"],))
                    if not cur.fetchone()[0]:
                        cur.execute("ALTER TABLE audit_logs ADD COLUMN l2 MEDIUMTEXT NULL")
                        conn.commit()
            finally:
                conn.close()
            self._has_l2 = True
        except Exception:
            self._has_l2 = False

    def _run(self):
        while not self._stop.is_set():
            batch: list[dict] = []
            try:
                # Block briefly to accumulate a batch
                first = self._q.get(timeout=MYSQL_FLUSH_INTERVAL)
                batch.append(first)
            except queue.Empty:
                continue
            except Exception:
                continue
            # Drain more (non-blocking)
            while len(batch) < MYSQL_BATCH_SIZE:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            self._flush(batch)
        # Final drain on shutdown
        rest = []
        while True:
            try:
                rest.append(self._q.get_nowait())
            except queue.Empty:
                break
        if rest:
            self._flush(rest)

    def _flush(self, batch: list[dict]) -> None:
        if not batch:
            return
        self._ensure_l2_column()
        conn = None
        try:
            conn = self._connect()
            with conn.cursor() as cur:
                rows = []
                for entry in batch:
                    row = self._row(entry)
                    if row is None:
                        self._dropped["build"] += 1
                    else:
                        rows.append(row)
                if rows:
                    try:
                        # pymysql 把单行模板重写为多行 VALUES 并按 max_stmt_length 分块，
                        # 一批从 ~N 次查询往返降到 ~1 次/块（旧实现逐行 execute）
                        cur.executemany(self._insert_sql(), rows)
                    except Exception as e:
                        # 单条坏行会弄挂整条语句 ⇒ 先回滚已执行块（防重复），
                        # 再逐行兜底，保留「丢坏行、留好行」原语义
                        self._last_error = f"batch insert failed, per-row fallback: {e}"
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        for row in rows:
                            try:
                                cur.execute(self._insert_sql(), row)
                            except Exception as e2:
                                self._dropped["insert"] += 1
                                self._last_error = f"insert failed: {e2}"
                conn.commit()
            self._err_count = 0
            if self._disabled:
                self._disabled = False
                self._disabled_since = None
        except Exception as e:
            self._dropped["flush"] += len(batch)  # 连接/commit 失败 = 整批不重入，全丢
            self._last_error = f"connect/flush failed: {e}"
            self._err_count += 1
            if self._err_count >= 3:
                self._disabled = True
                if self._disabled_since is None:
                    self._disabled_since = time.time()
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _row(self, entry: dict) -> Optional[tuple]:
        try:
            ts_str = entry.get("time") or entry.get("ts")
            if ts_str:
                # Accept "2026-09-01 07:48:34" or ISO
                if "T" in ts_str:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                else:
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            else:
                ts = datetime.now(timezone.utc).replace(tzinfo=None)
            _risk = entry.get("risk_score")
            if _risk is not None:
                _risk = int(_risk)
            row = (
                ts,
                str(entry.get("id") or "")[:32],
                str(entry.get("type") or "")[:16],
                str(entry.get("action") or "")[:16],
                str(entry.get("rule") or entry.get("policy_rule") or "")[:64],
                str(entry.get("provider") or "")[:64] or None,
                str(entry.get("model") or "")[:128] or None,
                str(entry.get("requested_model") or "")[:128] or None,
                1 if entry.get("local") else 0,
                str(entry.get("layer") or "")[:8] or None,
                str(entry.get("downgraded_from") or "")[:64] or None,
                1 if entry.get("override_denied") else 0,
                str(entry.get("text_preview") or "") or None,
                json.dumps(entry.get("findings"), ensure_ascii=False) if entry.get("findings") else None,
                str(entry.get("client_ip") or "")[:64] or None,
                str(entry.get("token_masked") or "")[:16] or None,
                str(entry.get("filename") or "")[:255] or None,
                _risk,
            )
            if self._has_l2:  # 必须与 _insert_sql 同条件，否则列值数不配（1136 整批失败）
                row = row + (
                    json.dumps(entry.get("l2"), ensure_ascii=False)
                    if isinstance(entry.get("l2"), dict) else None,)
            return row
        except Exception:
            return None

    def _insert_sql(self) -> str:
        # 列顺序必须与 _row() 的返回顺序逐位对齐；
        # 护栏：tests/test_audit_key_column.py::test_mysql_writer_column_order_matches_row_values
        # （历史 bug：requested_model 排在第 14 位，而 SQL 里在第 8 位，导致
        #  layer='L1' 落进 local_flag 列，MySQL 报 1366 整行插入失败）
        cols = ("ts, req_id, type, action, rule, provider, model, requested_model, local_flag, layer, "
                "downgraded_from, override_denied, text_preview, findings_json, "
                "client_ip, token_masked, filename, risk_score")
        n = 18
        if self._has_l2:
            cols += ", l2"
            n = 19
        return (f"INSERT INTO audit_logs ({cols}) "
                f"VALUES ({','.join(['%s'] * n)})")

    def shutdown(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def status(self) -> dict:
        retry_in = 0
        if self._disabled and self._disabled_since is not None:
            retry_in = max(0, int(MYSQL_RETRY_INTERVAL - (time.time() - self._disabled_since)))
        dropped = dict(self._dropped)
        dropped["total"] = sum(dropped.values())
        return {
            "enabled": not self._disabled,
            "queue_size": self._q.qsize(),
            "last_error": self._last_error,
            "err_count": self._err_count,
            "retry_in": retry_in,
            "dropped": dropped,
            "cfg": {"host": self.cfg["host"], "port": self.cfg["port"], "database": self.cfg["database"]},
        }


    @staticmethod
    def query(cfg: dict, since: Optional[datetime] = None, until: Optional[datetime] = None,
              action: Optional[str] = None, rule: Optional[str] = None,
              type_: Optional[str] = None, limit: int = 5000) -> List[dict]:
        """Read rows from MySQL with optional filters. Returns oldest-first dict list."""
        if pymysql is None:
            return []
        sql = "SELECT * FROM audit_logs WHERE 1=1"
        args: list = []
        if since:
            sql += " AND ts >= %s"
            args.append(since)
        if until:
            sql += " AND ts < %s"
            args.append(until)
        if action:
            sql += " AND action = %s"
            args.append(action)
        if rule:
            sql += " AND rule = %s"
            args.append(rule)
        if type_:
            sql += " AND type = %s"
            args.append(type_)
        sql += " ORDER BY ts DESC LIMIT %s"
        args.append(int(limit))
        try:
            conn = pymysql.connect(
                host=cfg["host"], port=cfg["port"],
                user=cfg["user"], password=cfg["password"],
                database=cfg["database"], connect_timeout=3, read_timeout=10,
                cursorclass=DictCursor,
            )
            with conn.cursor() as cur:
                cur.execute(sql, args)
                rows = list(cur.fetchall())
            conn.close()
            # Convert datetimes to strings for JSON serialization
            for r in rows:
                if isinstance(r.get("ts"), datetime):
                    r["ts"] = r["ts"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                if isinstance(r.get("findings_json"), str):
                    try:
                        r["findings_json"] = json.loads(r["findings_json"])
                    except Exception:
                        pass
                if isinstance(r.get("l2"), str):
                    try:
                        r["l2"] = json.loads(r["l2"])
                    except Exception:
                        pass
            # Return oldest-first for CSV/log order
            return list(reversed(rows))
        except Exception as e:
            return [{"error": f"query failed: {e}"}]

    @staticmethod
    def count(cfg: dict, since: Optional[datetime] = None, until: Optional[datetime] = None,
              action: Optional[str] = None, rule: Optional[str] = None) -> Optional[dict]:
        """KPI 真值（审计库口径）：total/allow/route_local/block 单遍聚合。

        block 判式与 main._entry_blocked 同构（stat_scope.is_blocked）：action 以
        block 开头，或 rule=egress_denied（推断 403）。LIKE 的 % 必须写 %%——pymysql
        对非空 args 做 query % args 格式化，单 % 会直接 ValueError。失败返回 None，
        调用方回退热窗口径（fail-open，KPI 不 500）。
        """
        if pymysql is None:
            return None
        sql = ("SELECT COUNT(*) AS total,"
               " SUM(action = 'allow') AS c_allow,"
               " SUM(action = 'route_local') AS c_route,"
               " SUM(action LIKE 'block%%' OR rule = 'egress_denied') AS c_block"
               " FROM audit_logs WHERE 1=1")
        args: list = []
        if since:
            sql += " AND ts >= %s"
            args.append(since)
        if until:
            sql += " AND ts < %s"
            args.append(until)
        if action:
            sql += " AND action = %s"
            args.append(action)
        if rule:
            sql += " AND rule = %s"
            args.append(rule)
        try:
            conn = pymysql.connect(
                host=cfg["host"], port=cfg["port"],
                user=cfg["user"], password=cfg["password"],
                database=cfg["database"], connect_timeout=3, read_timeout=5,
                cursorclass=DictCursor,
            )
            with conn.cursor() as cur:
                cur.execute(sql, args)
                row = cur.fetchone()
            conn.close()
        except Exception:
            return None
        return {k: int(v or 0) for k, v in row.items()}


class AuditStore:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()
            return cls._instance

    def _init(self):
        self._mem: List[dict] = []
        self._mem_lock = threading.Lock()
        self._tail_cache: dict = {}   # n -> (Redis 版本签名, 解析好的 oldest-first 行)
        self._tail_lock = threading.Lock()
        self._r: Optional["redis.Redis"] = None
        self._r_url: Optional[str] = None
        self._r_disabled = False
        self._r_disabled_since: Optional[float] = None
        self._mysql_writer: Optional[_MySQLWriter] = None
        self._mysql_cfg: Optional[dict] = None
        self._mysql_want = False            # URL 有效（连接失败时值得重试）
        self._mysql_init_error: Optional[str] = None
        self._try_connect_redis()
        self._try_connect_mysql()
        if self._mysql_want and self._mysql_writer is None:
            # 启动时 MySQL 不在线：旧代码 writer 整个进程生命周期缺失（静默到重启）。
            # 后台 60s 一重试，DB 恢复后审计落库自动接上
            threading.Thread(target=self._mysql_retry_loop,
                             name="audit-mysql-retry", daemon=True).start()
        if self._r is not None:
            try:
                self._backfill_from_redis()
            except Exception:
                pass

    def _try_connect_redis(self):
        url = os.getenv("REDIS_URL", "").strip()
        if not url or redis is None:
            return
        if self._r is not None and self._r_url == url:
            return
        try:
            self._r = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
            self._r.ping()
            self._r_url = url
            self._r_disabled = False
            self._r_disabled_since = None
        except Exception:
            self._r = None
            self._r_url = None
            self._r_disabled = True
            if self._r_disabled_since is None:
                self._r_disabled_since = time.time()

    def _try_connect_mysql(self):
        url = os.getenv("AUDIT_MYSQL_URL", "").strip() or os.getenv("AUDIT_DATABASE_URL", "").strip()
        if not url or pymysql is None:
            return
        cfg = _parse_mysql_url(url)
        if not cfg or not cfg.get("database"):
            return
        self._mysql_want = True
        try:
            conn = pymysql.connect(
                host=cfg["host"], port=cfg["port"],
                user=cfg["user"], password=cfg["password"],
                database=cfg["database"], connect_timeout=3,
            )
            conn.close()
            self._mysql_cfg = cfg
            self._mysql_writer = _MySQLWriter(cfg)
            self._mysql_init_error = None
        except Exception as e:
            self._mysql_cfg = None
            self._mysql_writer = None
            self._mysql_init_error = repr(e)

    def _mysql_retry_loop(self):
        while True:
            time.sleep(MYSQL_RETRY_INTERVAL)
            if self._mysql_writer is not None:
                return
            self._try_connect_mysql()
            if self._mysql_writer is not None:
                print(f"[audit_store] mysql writer recovered: "
                      f"{self._mysql_cfg['host']}:{self._mysql_cfg['port']}/{self._mysql_cfg['database']}")
                return

    def _backfill_from_redis(self):
        try:
            raw = self._r.lrange(REDIS_KEY, 0, MAX_ENTRIES - 1)
            for item in reversed(raw):  # Redis 为 newest-first(lpush)，转成时间升序与 append() 一致
                try:
                    self._mem.append(json.loads(item))
                except Exception:
                    continue
            self._mem = self._mem[-MAX_ENTRIES:]
        except Exception:
            pass

    def append(self, entry: dict) -> None:
        if self._r is None:
            if self._r_disabled:
                if self._r_disabled_since is not None and (time.time() - self._r_disabled_since) >= REDIS_RETRY_INTERVAL:
                    self._r_disabled = False
                    self._r_disabled_since = None
                    self._try_connect_redis()
            else:
                self._try_connect_redis()
        with self._mem_lock:
            self._mem.append(entry)
            if len(self._mem) > MAX_ENTRIES:
                self._mem = self._mem[-MAX_ENTRIES:]
            if self._r is not None:
                try:
                    payload = json.dumps(entry, ensure_ascii=False)
                    pipe = self._r.pipeline()
                    pipe.lpush(REDIS_KEY, payload)
                    pipe.ltrim(REDIS_KEY, 0, MAX_ENTRIES - 1)
                    pipe.execute()
                except Exception:
                    self._r = None
                    self._r_disabled = True
                    if self._r_disabled_since is None:
                        self._r_disabled_since = time.time()
        # Fan out to MySQL (non-blocking, fail-open)
        if self._mysql_writer is not None:
            self._mysql_writer.enqueue(entry)

    def tail(self, n: int = 100) -> List[dict]:
        # Prefer Redis shared view for multi-worker consistency
        if self._r is not None:
            try:
                # 版本签名 = (LLEN, 最新一条)：lpush 只往头部加、ltrim 只裁尾部，
                # 签名不变即内容不变 → 翻页/改筛选不必重做 5000 次 json.loads（~45ms/次）。
                # ponytail: 缓存整份解析结果 ≈10MB/进程；返回 list 浅拷贝，调用方
                # 若要就地改 dict 只允许幂等补字段（现只 key_name 兜底），别改语义。
                sig = (self._r.llen(REDIS_KEY), self._r.lindex(REDIS_KEY, 0))
                with self._tail_lock:
                    hit = self._tail_cache.get(n)
                    if hit is not None and hit[0] == sig:
                        return list(hit[1])
                raw = self._r.lrange(REDIS_KEY, 0, n - 1)
                out = []
                for item in raw:
                    try:
                        # item may be bytes
                        if isinstance(item, bytes):
                            item = item.decode("utf-8")
                        out.append(json.loads(item))
                    except Exception:
                        continue
                if out:
                    # lpush gives newest first, return oldest-first for display
                    rows = list(reversed(out))
                    with self._tail_lock:
                        self._tail_cache[n] = (sig, rows)
                    return list(rows)
            except Exception:
                pass
        with self._mem_lock:
            if n <= 0 or n >= len(self._mem):
                return list(self._mem)
            return list(self._mem[-n:])

    def all(self) -> List[dict]:
        if self._r is not None:
            try:
                raw = self._r.lrange(REDIS_KEY, 0, MAX_ENTRIES - 1)
                out = []
                for item in raw:
                    try:
                        if isinstance(item, bytes):
                            item = item.decode("utf-8")
                        out.append(json.loads(item))
                    except Exception:
                        continue
                if out:
                    return list(reversed(out))
            except Exception:
                pass
        with self._mem_lock:
            return list(self._mem)

    def clear(self) -> None:
        with self._mem_lock:
            self._mem.clear()
            if self._r is not None:
                try:
                    self._r.delete(REDIS_KEY)
                except Exception:
                    self._r = None
                    self._r_disabled = True
                    if self._r_disabled_since is None:
                        self._r_disabled_since = time.time()

    def backend(self) -> str:
        parts = []
        if self._r is not None:
            _u = urlsplit(self._r_url or "")
            _hp = (_u.hostname or "") + (f":{_u.port}" if _u.port else "")
            parts.append(f"redis ({_u.scheme}://{_hp}{_u.path})")
        elif self._r_disabled:
            retry_in = 0
            if self._r_disabled_since is not None:
                retry_in = max(0, int(REDIS_RETRY_INTERVAL - (time.time() - self._r_disabled_since)))
            parts.append(f"memory (redis disabled, retry in {retry_in}s)")
        else:
            parts.append("memory (no REDIS_URL)")
        if self._mysql_writer is not None:
            wstatus = self._mysql_writer.status()
            if wstatus["enabled"]:
                parts.append(f"mysql ({wstatus['cfg']['host']}:{wstatus['cfg']['port']}/{wstatus['cfg']['database']})")
            else:
                parts.append(f"mysql (disabled after error: {wstatus.get('last_error', '?')})")
        else:
            parts.append("mysql (disabled)")
        return " + ".join(parts)

    def mysql_status(self) -> dict:
        if self._mysql_writer is None:
            if self._mysql_want:
                return {"enabled": False,
                        "reason": f"connect failed, retrying every {int(MYSQL_RETRY_INTERVAL)}s: "
                                  f"{self._mysql_init_error}"}
            return {"enabled": False, "reason": "AUDIT_MYSQL_URL not set or pymysql not installed"}
        return self._mysql_writer.status()

    def query_mysql(self, since: Optional[datetime] = None, until: Optional[datetime] = None,
                    action: Optional[str] = None, rule: Optional[str] = None,
                    type_: Optional[str] = None, limit: int = 5000) -> List[dict]:
        if self._mysql_cfg is None:
            return []
        return _MySQLWriter.query(self._mysql_cfg, since, until, action, rule, type_, limit)

    def count_mysql(self, since: Optional[datetime] = None, until: Optional[datetime] = None,
                    action: Optional[str] = None, rule: Optional[str] = None) -> Optional[dict]:
        """审计库 KPI 聚合（未配 MySQL 或查询失败 → None，调用方回退热窗口径）。"""
        if self._mysql_cfg is None:
            return None
        return _MySQLWriter.count(self._mysql_cfg, since, until, action, rule)


_store: Optional[AuditStore] = None


def get_audit_store() -> AuditStore:
    global _store
    if _store is None:
        _store = AuditStore()
    return _store
