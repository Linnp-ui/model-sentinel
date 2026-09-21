"""Admin Console storage layer (M0).

需求映射：1.4 管理员密码(Postgres 哈希+改密全 session 失效)、4.2 API KEY 登记/映射、
2 IP 黑白名单、3.x 请求明细落库。

设计约束：
- 双后端：DATABASE_URL 配置了 postgresql+psycopg:// 时用 Postgres（SQLAlchemy Core）；
  未配置或连接失败时自动降级为线程安全的内存后端（本地开发/测试零依赖）。
- 密码哈希用 hashlib.scrypt（stdlib），格式 scrypt$salt_hex$hash_hex，
  规避 passlib+bcrypt 4.x 的已知兼容问题。
- session 密钥派生：sha256("gw-admin-v2:" + 当前密码哈希)。改密码 -> 哈希变 ->
  密钥变 -> 所有已签发 cookie 立即失效（配合 main.py 的 HMAC 会话）。
- IP 判定结果带 5s TTL 缓存，避免每请求打 DB；写操作即时失效缓存。
- key 明文仅存 DB（内网可信库），所有 API 出口必须经 mask_key() 脱敏。
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import math
import os
import secrets
import threading
import time
from typing import Any, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    insert,
    select,
    update as sa_update,
    delete as sa_delete,
)

from .stat_scope import is_abnormal, is_blocked, is_local_route

_IP_CACHE_TTL_S = 5.0
_ENTRY_TTL_S = 30.0  # P1: 注册表 entry 缓存（精确+模糊+负缓存），写操作 bump 版本即时失效

metadata = MetaData()

admin_credential = Table(
    "admin_credential",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("username", String(64), nullable=False, unique=True),
    Column("password_hash", String(256), nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

api_key_map = Table(
    "api_key_map",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("key_plain", String(256), nullable=False, unique=True),
    Column("name", String(128), nullable=False),
    Column("owner", String(128), nullable=False, default=""),
    Column("note", Text, nullable=False, default=""),
    Column("disabled", Integer, nullable=False, default=0),
    Column("created_at", DateTime, nullable=False),
)

key_rule = Table(
    "key_rule",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(8), nullable=False),  # 'black' | 'white'
    Column("key_value", String(256), nullable=False),
    Column("note", Text, nullable=False, default=""),
    Column("source", String(16), nullable=False, default="manual"),  # manual|auto
    Column("created_at", DateTime, nullable=False),
    # (kind, key_value) 唯一由应用层保证（内存后端）；PG 侧加唯一索引见 ensure_schema
)

request_log = Table(
    "request_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False),
    Column("client_ip", String(64), nullable=False, default=""),
    Column("key_name", String(128), nullable=False, default=""),  # key 映射名称（人员，快照/回退）
    Column("key_id", Integer, nullable=True),  # api_key_map.id 稳定身份；改名不改它，读时 join 当前名
    Column("model", String(128), nullable=False, default=""),
    Column("provider", String(64), nullable=False, default=""),
    Column("action", String(32), nullable=False, default=""),  # allow|block|route_local|...
    Column("status_code", Integer, nullable=False, default=0),
    Column("blocked_reason", String(256), nullable=False, default=""),
    Column("duration_ms", Integer, nullable=False, default=0),
    Column("gateway_internal_ms", Integer, nullable=False, default=0),
    Column("upstream_ms", Integer, nullable=False, default=0),
    Column("prompt_tokens", Integer, nullable=False, default=0),
    Column("completion_tokens", Integer, nullable=False, default=0),
)


alias_group = Table(
    "alias_group",
    metadata,
    Column("name", String(64), primary_key=True),
    Column("description", String(256), nullable=False, default=""),
    Column("updated_at", DateTime, nullable=False),
)

alias_member = Table(
    "alias_member",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("group_name", String(64), nullable=False),
    Column("provider", String(64), nullable=False),
    Column("model", String(190), nullable=False),
    Column("priority", Integer, nullable=False, default=100),  # 小=优先；最小者为默认
    Column("enabled", Integer, nullable=False, default=1),
)

ops_log = Table(
    "ops_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, nullable=False),
    Column("action", String(64), nullable=False),
    Column("detail", String(512), nullable=False, default=""),
    Column("operator", String(128), nullable=False, default=""),
)


# ---------------- 密码哈希（stdlib scrypt） ----------------

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        h = hashlib.scrypt(
            pw.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32,
        )
        return hmac.compare_digest(h, bytes.fromhex(hash_hex))
    except Exception:
        return False


def mask_key(key: str) -> str:
    """API KEY 脱敏展示：保留前 3 后 4。"""
    k = (key or "").strip()
    if len(k) <= 8:
        return "***"
    return k[:3] + "***" + k[-4:]


def _fuzzy_parts(masked: str):
    """模糊 key（拦码）解析：'sk-dccc3*****a74a' -> ('sk-dccc3', 'a74a")。
    前缀=首个 * 前，后缀=尾个 * 后，均需 >=3 位；无 * 或格式不足返回 (None, None)。"""
    k = (masked or "").strip()
    if "*" not in k:
        return None, None
    prefix = k.split("*", 1)[0]
    suffix = k.rsplit("*", 1)[1]
    if len(prefix) >= 3 and len(suffix) >= 3:
        return prefix, suffix
    return None, None


_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _ts_str(ts) -> str:
    """Normalize ts (datetime/date/str) to "YYYY-MM-DD HH:MM:SS" string."""
    if ts is None:
        return ""
    if isinstance(ts, str):
        return ts
    if isinstance(ts, datetime.datetime):
        return ts.strftime(_TS_FMT)
    if isinstance(ts, datetime.date):
        return datetime.datetime(ts.year, ts.month, ts.day).strftime(_TS_FMT)
    try:
        return str(ts)
    except Exception:
        return ""


def _ts_beijing(ts) -> str:
    """UTC ts -> 北京时间字符串（仅展示用）。落库与 since/until 窗口过滤仍是 UTC。

    与 main._to_beijing 同口径；admin_store 不 import main（避免循环），故本地实现。
    解析失败原样返回。
    """
    s = _ts_str(ts)
    if not s:
        return s
    try:
        dt = datetime.datetime.strptime(s[:19], _TS_FMT)
        return (dt + datetime.timedelta(hours=8)).strftime(_TS_FMT)
    except Exception:
        return s


def _parse_ts(s):
    """Parse "YYYY-MM-DD HH:MM:SS" (or ISO/date) to datetime; None if unparseable."""
    if s is None:
        return None
    if isinstance(s, datetime.datetime):
        return s
    if isinstance(s, datetime.date):
        return datetime.datetime(s.year, s.month, s.day)
    txt = str(s).strip()
    if not txt:
        return None
    for fmt in (_TS_FMT, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(txt[:len(fmt)] if len(txt) >= len(fmt) else txt, fmt)
        except Exception:
            continue
    try:
        return datetime.datetime.fromisoformat(txt)
    except Exception:
        return None


class AdminStore:
    """单例存储。backend: 'postgres' | 'memory'。"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()
            return cls._instance

    # ---------- init ----------

    def _init(self):
        self._engine = None
        self._backend = "memory"
        self._mem_lock = threading.RLock()
        # 内存后端数据
        self._cred: Optional[dict] = None           # {username, password_hash, updated_at}
        self._keys: dict[int, dict] = {}
        self._key_seq = 1
        self._key_rules: dict[int, dict] = {}
        self._key_seq = 1
        self._logs: list[dict] = []
        self._log_seq = 1
        # IP 判定缓存（两种后端共用）
        self._key_cache: dict[str, tuple[Optional[str], float]] = {}
        # 对外模型别名组（内存后端数据 + 运行时 TTL 缓存）
        self._aliases: dict[str, dict] = {}
        self._alias_seq = 1
        self._alias_cache: Optional[tuple[dict, float]] = None
        # 运维操作留痕（内存后端数据）
        self._ops_logs: list[dict] = []
        self._ops_seq = 1
        self._entry_cache: dict[str, tuple[float, int, tuple]] = {}  # P1: key -> (ts, ver, entry)
        self._entry_ver: int = 0
        self._try_connect()

    def _try_connect(self):
        # ADMIN_DB_URL 优先（admin console 独立配库），回退 DATABASE_URL
        url = (os.getenv("ADMIN_DB_URL", "") or os.getenv("DATABASE_URL", "") or "").strip()
        if not url:
            return
        if url.startswith("postgresql+psycopg://"):
            pass  # psycopg3 async/sync 均可，SQLAlchemy 走 sync
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        elif url.startswith(("mysql+pymysql://", "mysql://")):
            if url.startswith("mysql://"):
                url = url.replace("mysql://", "mysql+pymysql://", 1)
            if "?" not in url:
                url += "?charset=utf8mb4"
        else:
            return  # 仅支持 postgres / mysql；其他交由 audit_store
        try:
            eng = create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=2)
            with eng.connect():
                pass
            self._engine = eng
            self._backend = "mysql" if url.startswith("mysql") else "postgres"
            self.ensure_schema()
        except Exception as e:  # 连不上 -> 内存兜底，不阻塞启动
            print(f"[admin_store] ADMIN_DB_URL/DATABASE_URL unreachable, fallback to memory: {e!r}")
            self._engine = None
            self._backend = "memory"

    @property
    def backend(self) -> str:
        return self._backend

    def ensure_schema(self):
        if not self._engine:
            return
        with self._engine.begin() as conn:
            metadata.create_all(conn)
            self._add_missing_columns(conn)
        dialect = self._engine.dialect.name
        try:
            with self._engine.begin() as conn:
                if dialect == "mysql":
                    # MySQL 无 CREATE INDEX IF NOT EXISTS：已存在则忽略
                    conn.exec_driver_sql(
                        "CREATE UNIQUE INDEX ux_key_rule_kind_key ON key_rule (kind, key_value)"
                    )
                else:
                    conn.exec_driver_sql(
                        "CREATE UNIQUE INDEX IF NOT EXISTS ux_key_rule_kind_key ON key_rule (kind, key_value)"
                    )
        except Exception:
            pass  # 索引已存在

    def _add_missing_columns(self, conn):
        """给已存在表补缺列（create_all 不给已存表加列；2026-09-17 S8：1cdeec5 加列后生产 500 约 2h）。

        仅自动补 nullable 列或带字面默认值的列（渲染 NOT NULL DEFAULT）；
        NOT NULL 且无默认值的列只警告跳过，留人工迁移。
        """
        from sqlalchemy import inspect as _sa_inspect
        insp = _sa_inspect(conn)
        dialect = conn.dialect
        for table in metadata.tables.values():
            if not insp.has_table(table.name):
                continue
            db_cols = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in db_cols:
                    continue
                dflt = None
                if col.default is not None and hasattr(col.default, "arg"):
                    dflt = col.default.arg
                if not col.nullable and dflt is None:
                    print(f"[admin_store] WARNING: {table.name}.{col.name} NOT NULL 无默认，不能自动补，需人工迁移")
                    continue
                ddl = "ALTER TABLE %s ADD COLUMN %s %s" % (
                    table.name, col.name, col.type.compile(dialect=dialect))
                if not col.nullable:
                    ddl += " NOT NULL"
                if dflt is not None:
                    if isinstance(dflt, str):
                        ddl += " DEFAULT '" + str(dflt).replace("'", "''") + "'"
                    else:
                        ddl += " DEFAULT " + str(dflt)
                conn.exec_driver_sql(ddl)
                print(f"[admin_store] auto-migrated: {table.name}.{col.name} added")
    # ---------- 1.4 管理员凭据 ----------

    def has_credential(self) -> bool:
        if self._backend != "memory":
            with self._engine.connect() as conn:
                row = conn.execute(select(admin_credential.c.id).limit(1)).first()
                return row is not None
        with self._mem_lock:
            return self._cred is not None

    def set_admin_password(self, pw: str, username: str = "admin") -> None:
        if not pw or len(pw) < 8:
            raise ValueError("password must be >= 8 chars")
        h = hash_password(pw)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if self._backend != "memory":
            if self._engine.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                with self._engine.begin() as conn:
                    conn.execute(
                        pg_insert(admin_credential)
                        .values(username=username, password_hash=h, updated_at=now)
                        .on_conflict_do_update(
                            index_elements=[admin_credential.c.username],
                            set_={"password_hash": h, "updated_at": now},
                        )
                    )
            elif self._engine.dialect.name == "mysql":
                from sqlalchemy.dialects.mysql import insert as my_insert
                with self._engine.begin() as conn:
                    conn.execute(
                        my_insert(admin_credential)
                        .values(username=username, password_hash=h, updated_at=now)
                        .on_duplicate_key_update(password_hash=h, updated_at=now)
                    )
            else:
                with self._engine.begin() as conn:
                    conn.execute(sa_delete(admin_credential))
                    conn.execute(
                        insert(admin_credential)
                        .values(username=username, password_hash=h, updated_at=now)
                    )
        else:
            with self._mem_lock:
                self._cred = {"username": username, "password_hash": h, "updated_at": now}

    def verify_admin_password(self, pw: str) -> bool:
        """校验密码：DB 凭据优先，无凭据时回退 env（引导期）。"""
        stored = None
        if self._backend != "memory":
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(admin_credential.c.password_hash).limit(1)
                ).first()
                stored = row[0] if row else None
        else:
            with self._mem_lock:
                stored = self._cred["password_hash"] if self._cred else None
        if stored:
            return verify_password(pw, stored)
        env_pw = (os.getenv("AI_GATEWAY_ADMIN_PASSWORD", "") or "").strip()
        if env_pw:
            return secrets.compare_digest((pw or "").encode("utf-8"), env_pw.encode("utf-8"))
        return False

    def session_secret(self) -> Optional[str]:
        """当前凭据对应的会话签名密钥（改密即轮换 -> 全 session 失效）。"""
        if self._backend != "memory":
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(admin_credential.c.password_hash).limit(1)
                ).first()
                stored = row[0] if row else None
        else:
            with self._mem_lock:
                stored = self._cred["password_hash"] if self._cred else None
        if not stored:
            return None
        return hashlib.sha256(("gw-admin-v2:" + stored).encode("utf-8")).hexdigest()

    # ---------- 4.2 API KEY 映射 ----------

    def list_api_keys(self) -> list[dict]:
        rows: list[dict]
        if self._backend != "memory":
            with self._engine.connect() as conn:
                rs = conn.execute(
                    select(api_key_map).order_by(api_key_map.c.id.desc())
                ).mappings().all()
                rows = [dict(r) for r in rs]
        else:
            with self._mem_lock:
                rows = [dict(v) for v in sorted(self._keys.values(), key=lambda x: -x["id"])]
        for r in rows:
            plain = r.pop("key_plain", "")
            r["key_masked"] = plain if "*" in plain else mask_key(plain)
        return rows

    def create_api_key(self, key: str, name: str, owner: str = "", note: str = "") -> dict:
        key = (key or "").strip()
        name = (name or "").strip()
        if "*" in key:
            if not _fuzzy_parts(key)[0]:
                raise ValueError("invalid fuzzy key: need >=3 char prefix and suffix around *")
        if not key:
            raise ValueError("key is required")
        if not name:
            raise ValueError("name is required")
        if len(name) > 128:
            raise ValueError("name too long")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if self._backend != "memory":
            with self._engine.begin() as conn:
                exists = conn.execute(
                    select(api_key_map.c.id).where(api_key_map.c.key_plain == key)
                ).first()
                if exists:
                    raise ValueError("key already registered")
                rid = conn.execute(
                    insert(api_key_map).values(
                        key_plain=key, name=name, owner=owner, note=note,
                        disabled=0, created_at=now,
                    )
                ).inserted_primary_key[0]
        else:
            with self._mem_lock:
                if any(v["key_plain"] == key for v in self._keys.values()):
                    raise ValueError("key already registered")
                rid = self._key_seq
                self._key_seq += 1
                self._keys[rid] = {
                    "id": rid, "key_plain": key, "name": name, "owner": owner,
                    "note": note, "disabled": 0, "created_at": now,
                }
        self._bump_entry_ver()
        return {"id": rid, "key_masked": (key if "*" in key else mask_key(key)), "name": name, "owner": owner}

    def generate_api_key(self, name: str, owner: str = "", note: str = "") -> dict:
        """Generate a random API key (``sk_`` + token_urlsafe(32), 256-bit entropy),
        store it via create_api_key, and return the full key in ``key_plain``
        (only exposed at creation time; never persisted masked on response)."""
        key = "sk_" + secrets.token_urlsafe(32)
        item = self.create_api_key(key, name, owner, note)
        return {**item, "key_plain": key}

    def update_api_key(self, kid: int, name: Optional[str] = None,
                       owner: Optional[str] = None, note: Optional[str] = None,
                       disabled: Optional[bool] = None) -> None:
        vals: dict[str, Any] = {}
        if name is not None:
            vals["name"] = name.strip()
        if owner is not None:
            vals["owner"] = owner
        if note is not None:
            vals["note"] = note
        if disabled is not None:
            vals["disabled"] = 1 if disabled else 0
        if not vals:
            return
        # 改名只改 api_key_map.name：request_log 存的是 key_id（稳定身份），
        # 统计读时按 key_id join 当前名（见 _log_rows/_resolve_key_name），
        # 无需重写历史，改名后占比图自动按新名合并。
        if self._backend != "memory":
            with self._engine.begin() as conn:
                conn.execute(sa_update(api_key_map).where(api_key_map.c.id == kid).values(**vals))
        else:
            with self._mem_lock:
                if kid not in self._keys:
                    raise KeyError("not found")
                self._keys[kid].update(vals)
        self._bump_entry_ver()

    def delete_api_key(self, kid: int) -> None:
        if self._backend != "memory":
            with self._engine.begin() as conn:
                conn.execute(sa_delete(api_key_map).where(api_key_map.c.id == kid))
        else:
            with self._mem_lock:
                self._keys.pop(kid, None)
        self._bump_entry_ver()

    def _bump_entry_ver(self) -> None:
        """P1: 注册表写操作后调用，精确+模糊+负缓存即时失效。"""
        self._entry_ver += 1
        self._entry_cache.clear()

    def find_key_entry(self, key: str):
        """key -> (kid, name, via_fuzzy)，带 30s 进程缓存（写 bump 即时失效）。

        纯读：模糊升级由调用方（auth 首鉴权）做，查询本身不加锁；
        auth 成功后调 note_promoted() 把缓存修正为精确，避免 30s 窗口重复 UPDATE。
        """
        key = (key or "").strip()
        if not key:
            return 0, "", False
        now = time.time()
        hit = self._entry_cache.get(key)
        if hit is not None:
            ts, ver, res = hit
            if ver == self._entry_ver and now - ts < _ENTRY_TTL_S:
                return res
        res = self._find_key_entry_uncached(key)
        self._entry_cache[key] = (now, self._entry_ver, res)
        if len(self._entry_cache) > 1000:
            self._entry_cache.pop(next(iter(self._entry_cache)))
        return res

    def note_promoted(self, key: str, entry: tuple) -> None:
        """P1: auth promote 成功后调用，把该 key 缓存修正为精确态。"""
        key = (key or "").strip()
        if not key:
            return
        self._entry_cache[key] = (time.time(), self._entry_ver, entry)

    def _find_key_entry_uncached(self, key: str):
        """key -> (kid, name, via_fuzzy)。精确优先，其次模糊（前缀+后缀匹配拦码条目）。
        未命中返回 (0, "", False)。模糊命中要求完整 key >=16 位且前后缀均匹配。"""
        key = (key or "").strip()
        if not key:
            return 0, "", False
        if self._backend != "memory":
            with self._engine.connect() as conn:
                row = conn.execute(
                    select(api_key_map.c.id, api_key_map.c.name).where(
                        (api_key_map.c.key_plain == key) & (api_key_map.c.disabled == 0)
                    )
                ).first()
                if row:
                    return row[0], row[1], False
            if len(key) >= 16:
                with self._engine.connect() as conn:
                    rows = conn.execute(
                        select(api_key_map.c.id, api_key_map.c.key_plain, api_key_map.c.name).where(
                            (api_key_map.c.key_plain.like("%*%")) & (api_key_map.c.disabled == 0)
                        )
                    ).all()
                for rid, kp, name in rows:
                    prefix, suffix = _fuzzy_parts(kp)
                    if prefix and key.startswith(prefix) and key.endswith(suffix):
                        return rid, name, True
            return 0, "", False
        with self._mem_lock:
            for v in self._keys.values():
                if v["key_plain"] == key and not v["disabled"]:
                    return v["id"], v["name"], False
            if len(key) >= 16:
                for v in self._keys.values():
                    prefix, suffix = _fuzzy_parts(v["key_plain"])
                    if prefix and not v["disabled"] and key.startswith(prefix) and key.endswith(suffix):
                        return v["id"], v["name"], True
        return 0, "", False

    def find_key_name(self, key: str) -> str:
        """按 key 查映射名称（统计归因用）。精确优先，其次模糊。未命中返回 ''。"""
        return self.find_key_entry(key)[1]

    def promote_key(self, kid: int, key: str) -> None:
        """模糊条目升级为精确：首次模糊命中时存入完整 key，之后拦码不可再伪造。"""
        key = (key or "").strip()
        if not key:
            return
        if self._backend != "memory":
            with self._engine.begin() as conn:
                conn.execute(sa_update(api_key_map).where(api_key_map.c.id == kid).values(key_plain=key))
            return
            self._bump_entry_ver()
            return
        with self._mem_lock:
            if kid in self._keys:
                self._keys[kid]["key_plain"] = key
        self._bump_entry_ver()

    # ---------- 2 IP 黑白名单 ----------

    # ---------- 运维操作留痕 ----------

    def log_ops(self, action: str, detail: str = "", operator: str = "") -> None:
        row = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": str(action or "")[:64],
            "detail": str(detail or "")[:512],
            "operator": str(operator or "")[:128],
        }
        if self._backend != "memory":
            try:
                with self._engine.begin() as conn:
                    conn.execute(insert(ops_log).values(**row))
                return
            except Exception as e:
                print(f"[admin_store] ops_log insert failed, fallback memory: {e!r}")
        with self._mem_lock:
            self._ops_seq += 1
            self._ops_logs.append(dict(row, id=self._ops_seq))
            if len(self._ops_logs) > 500:  # 内存后端只留最近 500 条
                del self._ops_logs[:-500]

    def list_ops_log(self, limit: int = 50) -> list[dict]:
        """运维留痕，最新在前。"""
        limit = max(1, min(int(limit or 50), 200))
        if self._backend != "memory":
            try:
                with self._engine.connect() as conn:
                    rows = conn.execute(
                        select(ops_log).order_by(ops_log.c.id.desc()).limit(limit)
                    ).mappings().all()
                    return [dict(r) for r in rows]
            except Exception as e:
                print(f"[admin_store] ops_log select failed, fallback memory: {e!r}")
        with self._mem_lock:
            return [dict(r) for r in list(reversed(self._ops_logs))[:limit]]

    # ---------- 对外模型别名（ext-flash / ext-pro 动态路由；纯 DB/Console 管理，无内置种子） ----------

    def _invalidate_alias_cache(self) -> None:
        self._alias_cache = None

    def list_alias_groups(self) -> list[dict]:
        """管理视图：全部组 + 成员（含未启用）。"""
        groups: list[dict] = []
        if self._backend != "memory":
            with self._engine.connect() as conn:
                g_rows = [dict(r) for r in conn.execute(select(alias_group)).mappings().all()]
                m_rows = [dict(r) for r in conn.execute(
                    select(alias_member).order_by(alias_member.c.group_name, alias_member.c.priority)
                ).mappings().all()]
            for g in g_rows:
                groups.append({
                    "name": g["name"], "description": g.get("description") or "",
                    "members": [m for m in m_rows if m["group_name"] == g["name"]],
                })
        else:
            with self._mem_lock:
                for name in sorted(self._aliases):
                    g = self._aliases[name]
                    groups.append({
                        "name": g["name"], "description": g.get("description") or "",
                        "members": [dict(m) for m in sorted(g["members"], key=lambda x: x["priority"])],
                    })
        return groups

    def update_alias_group(self, name: str, members: list[dict], description: str = "") -> dict:
        """整组替换成员。members: [{provider, model, priority, enabled}]，按 priority 升序重排为 10,20,..."""
        name = (name or "").strip()
        if not name:
            raise ValueError("group name is required")
        if not isinstance(members, list) or not members:
            raise ValueError("members must be a non-empty list")
        norm: list[dict] = []
        for m in members:
            pr = str(m.get("provider") or "").strip()
            mo = str(m.get("model") or "").strip()
            if not pr or not mo:
                raise ValueError("each member needs provider and model")
            try:
                pri = int(m.get("priority") or 0)
            except (TypeError, ValueError):
                pri = 0
            norm.append({"provider": pr, "model": mo, "priority": pri,
                         "enabled": 1 if m.get("enabled", 1) in (1, True, "1", "true") else 0})
        norm.sort(key=lambda x: x["priority"])
        for i, m in enumerate(norm):
            m["priority"] = (i + 1) * 10
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if self._backend != "memory":
            with self._engine.begin() as conn:
                _gvals = dict(name=name, description=description, updated_at=now)
                if self._engine.dialect.name == "mysql":
                    from sqlalchemy.dialects.mysql import insert as _my_insert
                    conn.execute(
                        _my_insert(alias_group).values(**_gvals)
                        .on_duplicate_key_update(description=description, updated_at=now)
                    )
                elif self._engine.dialect.name == "postgresql":
                    from sqlalchemy.dialects.postgresql import insert as _pg_insert
                    conn.execute(
                        _pg_insert(alias_group).values(**_gvals)
                        .on_conflict_do_update(index_elements=["name"],
                                               set_={"description": description, "updated_at": now})
                    )
                else:
                    conn.execute(sa_update(alias_group).where(alias_group.c.name == name)
                                 .values(description=description, updated_at=now))
                    conn.execute(insert(alias_group).values(**_gvals))
                conn.execute(sa_delete(alias_member).where(alias_member.c.group_name == name))
                for m in norm:
                    conn.execute(insert(alias_member).values(
                        group_name=name, provider=m["provider"], model=m["model"],
                        priority=m["priority"], enabled=m["enabled"],
                    ))
        else:
            with self._mem_lock:
                self._aliases[name] = {
                    "name": name, "description": description,
                    "members": [dict(m, id=self._alias_seq + i) for i, m in enumerate(norm)],
                }
                self._alias_seq += len(norm) + 1
        self._invalidate_alias_cache()
        return self.get_alias_routes().get(name.lower()) or {}

    def delete_alias_group(self, name: str) -> dict:
        """删除别名组及其全部成员（组名大小写不敏感）；不存在则抛 ValueError。

        删除后 get_alias_routes() 立即不再返回该别名 —— /v1/models 不再展示，
        指向它的请求按未知模型处理（404）。
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("group name is required")
        if self._backend != "memory":
            with self._engine.begin() as conn:
                names = [r[0] for r in conn.execute(select(alias_group.c.name)).all()]
                real = next((r for r in names if str(r).lower() == name.lower()), None)
                if real is None:
                    raise ValueError("alias group not found: %s" % name)
                conn.execute(sa_delete(alias_member).where(alias_member.c.group_name == real))
                conn.execute(sa_delete(alias_group).where(alias_group.c.name == real))
        else:
            with self._mem_lock:
                real = next((k for k in self._aliases if k.lower() == name.lower()), None)
                if real is None:
                    raise ValueError("alias group not found: %s" % name)
                self._aliases.pop(real, None)
        self._invalidate_alias_cache()
        return {"group": real}

    def get_alias_routes(self) -> dict:
        """运行时视图（带 5s TTL 缓存）：{别名小写: {"name", "candidates": [{provider, model}]}}"""
        now = time.time()
        if self._alias_cache and now - self._alias_cache[1] < _IP_CACHE_TTL_S:
            return self._alias_cache[0]
        routes: dict = {}
        for g in self.list_alias_groups():
            cands = [
                {"provider": m["provider"], "model": m["model"]}
                for m in sorted(g["members"], key=lambda x: x["priority"])
                if m.get("enabled", 1)
            ]
            if cands:
                routes[g["name"].lower()] = {"name": g["name"], "candidates": cands}
        self._alias_cache = (routes, now)
        return routes

    def _invalidate_key_cache(self):
        self._key_cache.clear()

    def list_key_rules(self, kind: Optional[str] = None) -> list[dict]:
        if self._backend != "memory":
            with self._engine.connect() as conn:
                q = select(key_rule).order_by(key_rule.c.id.desc())
                if kind:
                    q = q.where(key_rule.c.kind == kind)
                rows = [dict(r) for r in conn.execute(q).mappings().all()]
        else:
            with self._mem_lock:
                rows = [
                    dict(v) for v in sorted(self._key_rules.values(), key=lambda x: -x["id"])
                    if not kind or v["kind"] == kind
                ]
        return rows

    def add_key_rule(self, kind: str, key_value: str, note: str = "", source: str = "manual") -> dict:
        kind = (kind or "").strip().lower()
        key_value = (key_value or "").strip()
        if kind not in ("black", "white"):
            raise ValueError("kind must be black|white")
        if not (8 <= len(key_value) <= 256):
            raise ValueError("key_value must be 8-256 chars")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if self._backend != "memory":
            with self._engine.begin() as conn:
                exists = conn.execute(
                    select(key_rule.c.id).where(
                        (key_rule.c.kind == kind) & (key_rule.c.key_value == key_value)
                    )
                ).first()
                if exists:
                    raise ValueError("rule already exists")
                rid = conn.execute(
                    insert(key_rule).values(
                        kind=kind, key_value=key_value, note=note, source=source, created_at=now
                    )
                ).inserted_primary_key[0]
        else:
            with self._mem_lock:
                if any(v["kind"] == kind and v["key_value"] == key_value for v in self._key_rules.values()):
                    raise ValueError("rule already exists")
                rid = self._key_seq
                self._key_seq += 1
                self._key_rules[rid] = {
                    "id": rid, "kind": kind, "key_value": key_value, "note": note,
                    "source": source, "created_at": now,
                }
        self._invalidate_key_cache()
        return {"id": rid, "kind": kind, "key_value": mask_key(key_value)}

    def remove_key_rule(self, rid: int) -> None:
        if self._backend != "memory":
            with self._engine.begin() as conn:
                conn.execute(sa_delete(key_rule).where(key_rule.c.id == rid))
        else:
            with self._mem_lock:
                self._key_rules.pop(rid, None)
        self._invalidate_key_cache()

    def lookup_key(self, key_value: str) -> Optional[str]:
        """返回 'white' | 'black' | None。白名单优先。带 TTL 缓存。"""
        key_value = (key_value or "").strip()
        if not key_value:
            return None
        now = time.time()
        hit = self._key_cache.get(key_value)
        if hit and now - hit[1] < _IP_CACHE_TTL_S:
            return hit[0]
        verdict: Optional[str] = None
        rules = self.list_key_rules()
        kinds = [r["kind"] for r in rules if r["key_value"] == key_value]
        # deny-overrides：white/black 共存时 black 优先。旧实现 white 优先，
        # 已加白 key 一键拉黑后两条规则共存 → 拉黑静默失效（2026-09-18 修）
        if "black" in kinds:
            verdict = "black"
        elif "white" in kinds:
            verdict = "white"
        self._key_cache[key_value] = (verdict, now)
        return verdict

    def list_api_keys_raw(self) -> list[dict]:
        """内部用：含 key 原文（仅服务端建议引擎使用，不对外暴露）。"""
        if self._backend != "memory":
            with self._engine.connect() as conn:
                rows = [
                    {"name": r["name"], "key_plain": r["key_plain"]}
                    for r in conn.execute(
                        select(api_key_map.c.name, api_key_map.c.key_plain)
                    ).mappings().all()
                ]
        else:
            with self._mem_lock:
                rows = [
                    {"name": v["name"], "key_plain": v["key_plain"]}
                    for v in self._keys.values()
                ]
        return rows

    # ---------- 3.x 请求明细 ----------

    def insert_request_log(self, entry: dict) -> None:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        ts_raw = entry.get("ts") or now
        if self._backend != "memory":
            _parsed = _parse_ts(ts_raw) if isinstance(ts_raw, str) else ts_raw
            ts_val = _parsed if _parsed is not None else datetime.datetime.now()
        else:
            ts_val = _ts_str(ts_raw) or now
        _kid = entry.get("key_id")
        try:
            _kid = int(_kid) if _kid else None
        except (TypeError, ValueError):
            _kid = None
        row = {
            "ts": ts_val,
            "client_ip": entry.get("client_ip", "") or "",
            "key_name": entry.get("key_name", "") or "",
            "key_id": _kid,
            "model": entry.get("model", "") or "",
            "provider": entry.get("provider", "") or "",
            "action": entry.get("action", "") or "",
            "status_code": int(entry.get("status_code", 0) or 0),
            "prompt_tokens": int(entry.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(entry.get("completion_tokens", 0) or 0),
            "blocked_reason": entry.get("blocked_reason", "") or "",
            "duration_ms": int(entry.get("duration_ms", 0) or 0),
            "gateway_internal_ms": int(entry.get("gateway_internal_ms", 0) or 0),
            "upstream_ms": int(entry.get("upstream_ms", 0) or 0),
        }
        if self._backend != "memory":
            with self._engine.begin() as conn:
                conn.execute(insert(request_log).values(**row))
        else:
            with self._mem_lock:
                row["id"] = self._log_seq
                self._log_seq += 1
                self._logs.append(row)

    def stats_key_model(self, since: str, until: str, limit_keys: int = 20) -> list:
        """按 KEY(人员) × 模型聚合 token（前端 KEY×模型占比环图用）。

        返回 [{key, model, tokens, calls}]，先按 KEY 总 token 取前 limit_keys 个 KEY，
        KEY 内按 tokens 降序。未登记 KEY 归为“未登记”。ts 统一走 _ts_str 归一（见 _log_rows）。

        model 是**实际模型**（`provider/model`，别名按当前别名组主候选解析，
        如 ext-flash → deepseek/deepseek-flash）；tokens==0 的组直接丢弃
        （被拦截/无用量请求不进 Token 占比）。
        """
        rows = self._log_rows(since, until)
        try:
            _routes = self.get_alias_routes()
        except Exception:
            _routes = {}

        def _real_label(provider: str, model: str) -> str:
            m = (model or "").strip()
            p = (provider or "").strip()
            cands = (_routes.get(m.lower()) or {}).get("candidates") or []
            if cands:
                return f"{cands[0]['provider']}/{cands[0]['model']}"
            return f"{p}/{m}" if p else (m or "(unknown)")

        agg: dict = {}
        for r in rows:
            key = (r.get("key_name") or "").strip() or "未登记"
            label = _real_label(r.get("provider") or "", r.get("model") or "")
            g = agg.setdefault((key, label), {"key": key, "model": label, "tokens": 0, "calls": 0})
            g["tokens"] += int(r.get("prompt_tokens") or 0) + int(r.get("completion_tokens") or 0)
            g["calls"] += 1
        key_tokens: dict = {}
        for (key, _), g in agg.items():
            if g["tokens"] <= 0:
                continue
            key_tokens[key] = key_tokens.get(key, 0) + g["tokens"]
        try:
            n = max(1, int(limit_keys or 20))
        except Exception:
            n = 20
        top = set(sorted(key_tokens, key=lambda k: -key_tokens[k])[:n])
        return sorted((g for k, g in agg.items() if k[0] in top and g["tokens"] > 0),
                      key=lambda g: (-key_tokens[g["key"]], -g["tokens"]))

    def stats_key_tier(self, since: str = "", until: str = "") -> list:
        """KEY 分级统计：[{key_name, calls, blocks}]，按 calls 降序。

        blocks = action 以 block 开头 或 status_code==403（口径见 stat_scope.is_blocked，
        与 _row_blocked 同源）。
        locals  = action == "route_local"（stat_scope.is_local_route）
        violations = 拦截 ∪ 本地路由（stat_scope.is_abnormal）—— **建议引擎的触发指标**。
                  route_local 本身不是「坏行为」，但它是「判定改变了处置」，对分级决策
                  与拦截同等重要（2026-09-16 B 案；见 is_abnormal docstring）。
        仅登记 KEY（key_name 非空）参与。"""
        agg: dict = {}
        for r in self._log_rows(since, until):
            kn = (r.get("key_name") or "").strip()
            if not kn:
                continue
            g = agg.setdefault(kn, {"key_name": kn, "calls": 0, "blocks": 0,
                                    "locals": 0, "violations": 0})
            _act, _sc = r.get("action"), r.get("status_code")
            g["calls"] += 1
            if is_blocked(_act, _sc):
                g["blocks"] += 1
            if is_local_route(_act):
                g["locals"] += 1
            if is_abnormal(_act, _sc):
                g["violations"] += 1
        return sorted(agg.values(), key=lambda g: -g["calls"])

    def count_request_logs(self) -> int:
        if self._backend != "memory":
            with self._engine.connect() as conn:
                return int(conn.execute(select(func.count()).select_from(request_log)).scalar() or 0)
        with self._mem_lock:
            return len(self._logs)


    # ---------- 3.x 统计聚合（M3） ----------

    @staticmethod
    def _row_blocked(row: dict) -> bool:
        """row 是否算「拦截」——口径唯一实现在 stat_scope.is_blocked()。

        历史坑：这里曾多一支 `action == "route_local"`，把合规本地路由算成拦截，
        与 stats_key_tier 的口径不一致；且 request_log 只写 allow/block，该分支
        永远不命中（定义里有、数据里没有）。
        """
        return is_blocked(row.get("action"), row.get("status_code"))

    def _key_id_name_map(self) -> dict:
        """{kid: 当前 name}。api_key_map 很小，每次建一次即可。"""
        m: dict = {}
        if self._backend != "memory":
            with self._engine.connect() as conn:
                for _kid, _name in conn.execute(select(api_key_map.c.id, api_key_map.c.name)).all():
                    m[_kid] = _name
        else:
            with self._mem_lock:
                for _kid, v in self._keys.items():
                    m[_kid] = v.get("name")
        return m

    @staticmethod
    def _resolve_key_name(d: dict, name_map: dict) -> str:
        """行级解析展示名：有 key_id 且仍在登记表 → 当前名（改名自动合并）；
        否则回退存储的 key_name 快照（已删除的 key / 未登记）。"""
        kid = d.get("key_id")
        if kid:
            try:
                _name = name_map.get(int(kid))
                if _name:
                    return _name
            except (TypeError, ValueError):
                pass
        return (d.get("key_name") or "").strip()

    def _log_rows(self, since: str = "", until: str = "") -> list:
        """取时间窗内明细（内存后端直接过滤；PG 走 SQL）。仅供内部聚合。

        返回行里 `key_name` 已解析为**当前名**（按 key_id join api_key_map），
        所以下游 stats_group / stats_key_model / stats_key_tier / list_request_logs
        一律按当前名分组/过滤——改名无需重写历史。"""
        name_map = self._key_id_name_map()
        if self._backend != "memory":
            with self._engine.connect() as conn:
                q = select(request_log).order_by(request_log.c.ts)
                conds = []
                if since:
                    _dt = _parse_ts(since)
                    conds.append(request_log.c.ts >= (_dt if _dt is not None else since))
                if until:
                    _dt = _parse_ts(until)
                    conds.append(request_log.c.ts <= (_dt if _dt is not None else until))
                if conds:
                    q = q.where(*conds)
                rows = conn.execute(q).mappings().all()
                out = []
                for r in rows:
                    d = dict(r)
                    d["ts"] = _ts_str(d.get("ts"))
                    d["key_name"] = self._resolve_key_name(d, name_map)
                    out.append(d)
                return out
        with self._mem_lock:
            rows = [dict(r, ts=_ts_str(r.get("ts"))) for r in list(self._logs)]
        if since:
            _s = _ts_str(since)
            rows = [r for r in rows if (r.get("ts") or "") >= _s]
        if until:
            _u = _ts_str(until)
            rows = [r for r in rows if (r.get("ts") or "") <= _u]
        for d in rows:
            d["key_name"] = self._resolve_key_name(d, name_map)
        return rows

    def stats_overview(self, since: str = "", until: str = "") -> dict:
        """总调用量 / 拦截量 / 本地路由 / 活跃 IP 数 / 平均耗时 / token 量。

        blocked 口径见 stat_scope.is_blocked（block* 或 403）；local_routed 走
        stat_scope.is_local_route（action == "route_local"）。

        ⚠️ 2026-09-16 订正：本 docstring 曾写「request_log 目前只写 allow/block，
        本地路由落在 allow 里，生产实测 route_local=0」—— 该结论已过期。中间件
        改用 stat_scope.resolve_action() 兜底后，request_log **确实会写 route_local**
        （同日 168h 实测：block 1166 / allow 1140 / route_local 214），本字段已是真值，
        界面「本地路由」不必再走审计侧。
        """
        rows = self._log_rows(since, until)
        total = len(rows)
        blocked = sum(1 for r in rows if self._row_blocked(r))
        local_routed = sum(1 for r in rows if is_local_route(r.get("action")))
        ips = {r.get("client_ip", "") for r in rows if r.get("client_ip")}
        durs = [int(r.get("duration_ms") or 0) for r in rows if int(r.get("duration_ms") or 0) > 0]
        avg_ms = int(sum(durs) / len(durs)) if durs else 0
        tp = sum(int(r.get("prompt_tokens") or 0) for r in rows)
        tc = sum(int(r.get("completion_tokens") or 0) for r in rows)
        return {"total": total, "blocked": blocked, "local_routed": local_routed,
                "active_ips": len(ips), "avg_ms": avg_ms,
                "tokens_prompt": tp, "tokens_completion": tc, "tokens_total": tp + tc}

    @staticmethod
    def _percentile(vals: list[int], p: float) -> int | None:
        """Hidden contract: vals already sorted ascending (caller pre-filters gateway_internal_ms>0)."""
        if not vals:
            return None
        idx = max(0, math.ceil(len(vals) * p / 100) - 1)
        return int(vals[idx])

    def latency_percentiles(self, since: str = "", until: str = "") -> dict:
        """P50/P95/P99 网关自身延迟分位（毫秒）→ {p50,p95,p99,count}，nearest-rank；
        只计 gateway_internal_ms>0 行（L1+L2+策略路由，不含上游模型生成时间）；
        全零窗口 = count 0 / 三值全 None。供 metrics_ring 每 tick 调用。"""
        vals = sorted(int(r.get("gateway_internal_ms") or 0) for r in self._log_rows(since, until) if int(r.get("gateway_internal_ms") or 0) > 0)
        return {
            "p50": self._percentile(vals, 50),
            "p95": self._percentile(vals, 95),
            "p99": self._percentile(vals, 99),
            "count": len(vals),
        }

    def stats_group(self, since: str, until: str, dim: str) -> list:
        """按 dim 分组统计：dim in (model|key_name|client_ip|provider|action)。

        返回 [{label, calls, tokens, blocked, local_routed, errors}]，按 calls 降序。
        """
        rows = self._log_rows(since, until)
        agg: dict = {}
        for r in rows:
            label = (r.get(dim) or "").strip() or "(unknown)"
            g = agg.setdefault(label, {"label": label, "calls": 0, "tokens": 0,
                                       "blocked": 0, "local_routed": 0, "errors": 0})
            g["calls"] += 1
            g["tokens"] += int(r.get("prompt_tokens") or 0) + int(r.get("completion_tokens") or 0)
            if self._row_blocked(r):
                g["blocked"] += 1
            if is_local_route(r.get("action")):
                g["local_routed"] += 1
            if int(r.get("status_code") or 0) >= 500:
                g["errors"] += 1
        return sorted(agg.values(), key=lambda g: -g["calls"])

    def stats_daily(self, since: str, until: str) -> list:
        """按天聚合（本地日期），返回 [{date, calls, blocked}] 升序。"""
        rows = self._log_rows(since, until)
        agg: dict = {}
        for r in rows:
            day = _ts_beijing(r.get("ts"))[:10]  # 北京日期分桶
            g = agg.setdefault(day, {"date": day, "calls": 0, "blocked": 0})
            g["calls"] += 1
            if self._row_blocked(r):
                g["blocked"] += 1
        return [agg[d] for d in sorted(agg)]

    def list_request_logs(
        self,
        limit: int = 100,
        offset: int = 0,
        ip: str = "",
        key_name: str = "",
        model: str = "",
        action: str = "",
        since: str = "",
        until: str = "",
    ) -> dict:
        """明细分页查询（新→旧），带过滤。返回 {items, total}。"""
        rows = self._log_rows(since, until)
        if ip:
            rows = [r for r in rows if r.get("client_ip") == ip]
        if key_name:
            rows = [r for r in rows if r.get("key_name") == key_name]
        if model:
            rows = [r for r in rows if r.get("model") == model]
        if action:
            rows = [r for r in rows if r.get("action") == action]
        for r in rows:
            r["ts"] = _ts_str(r.get("ts"))
        rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
        total = len(rows)
        items = rows[offset: offset + max(1, min(int(limit), 1000))]
        for r in items:
            r["ts"] = _ts_beijing(r.get("ts"))  # 展示 +8h（排序已按 UTC，顺序不变）
        return {"items": items, "total": total}

    def purge_request_logs(self, before: str) -> int:
        """删除 before（'YYYY-MM-DD HH:MM:SS'）之前的明细，返回删除行数。"""
        if self._backend != "memory":
            with self._engine.begin() as conn:
                _dt = _parse_ts(before)
                res = conn.execute(sa_delete(request_log).where(request_log.c.ts < (_dt if _dt is not None else before)))
                return int(res.rowcount or 0)
        with self._mem_lock:
            _b = _ts_str(before)
            keep = [r for r in self._logs if _ts_str(r.get("ts", "")) >= _b]
            removed = len(self._logs) - len(keep)
            self._logs = keep
            return removed



def get_admin_store() -> AdminStore:
    return AdminStore()
