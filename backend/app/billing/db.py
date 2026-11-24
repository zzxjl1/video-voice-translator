"""
计费账本（SQLite）。钱是**账号级**的，所以和工程文件分开存。

和 `auth.sqlite3` 分两个库：身份/会话是会话级的小事务，账本是高频追加，
分开写不会互相锁；用户用 `user_id` 文本关联（SQLite 跨库没有外键）。

三条硬规则：

  1. **append-only**：金额永不 UPDATE。更正只能追加一条反向分录（status='void'
     加上一笔 `adjustment`），这样"为什么余额是这个数"永远能重放出来。
  2. **整数 micro 元**：见 pricing.MICRO。
  3. **余额扣减与分录在同一个事务里**：否则崩溃会留下"记了账没扣钱"或反之。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

from app import config

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    project_id      TEXT,
    step            TEXT NOT NULL,
    model           TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT '',
    -- 用量快照（原样保留，便于事后核对；金额只认下面两列）
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
    calls           INTEGER NOT NULL DEFAULT 1,
    -- 钱
    cost_micro      INTEGER NOT NULL DEFAULT 0,   -- 成本：只在内部看，接口不返回
    price_micro     INTEGER NOT NULL DEFAULT 0,   -- 用户价：成本 × 倍率
    markup          REAL NOT NULL DEFAULT 1.0,
    currency        TEXT NOT NULL DEFAULT 'CNY',
    cost_source     TEXT NOT NULL DEFAULT 'computed',  -- computed|api|no_price|free
    status          TEXT NOT NULL DEFAULT 'settled',   -- settled|void
    idempotency_key TEXT UNIQUE,
    meta            TEXT NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_user_time ON ledger(user_id, created_at);
CREATE INDEX IF NOT EXISTS ledger_project_time ON ledger(project_id, created_at);

CREATE TABLE IF NOT EXISTS account (
    user_id        TEXT PRIMARY KEY,
    balance_micro  INTEGER NOT NULL DEFAULT 0,
    currency       TEXT NOT NULL DEFAULT 'CNY',
    updated_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS adjustment (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    delta_micro INTEGER NOT NULL,     -- 正=充值/补偿，负=人工扣减
    note        TEXT NOT NULL DEFAULT '',
    actor       TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS adjustment_user_time ON adjustment(user_id, created_at);
"""


def db_path() -> str:
    return config.BILLING_DB_PATH


def connect() -> sqlite3.Connection:
    path = db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)  # 自己管事务
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        conn = connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()
        _initialized = True


def conn() -> sqlite3.Connection:
    """每线程复用一个连接（SQLite 连接不能跨线程共享）。"""
    init()
    c = getattr(_local, "conn", None)
    if c is None:
        c = connect()
        _local.conn = c
    return c


def reset_for_tests() -> None:
    """测试用：丢掉缓存的连接和初始化标记（换了 DB 路径之后要调）。"""
    global _initialized
    with _init_lock:
        _initialized = False
    c = getattr(_local, "conn", None)
    if c is not None:
        try:
            c.close()
        except Exception:  # noqa: BLE001 - 关不掉也不影响测试
            pass
        _local.conn = None


def now() -> int:
    return int(time.time())
