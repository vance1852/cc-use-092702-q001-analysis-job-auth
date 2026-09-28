"""分类实验观察采信服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 3

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_protocol_catalog (
    evidence_protocol_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    title TEXT NOT NULL,
    task_family TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_at TEXT NOT NULL,
    PRIMARY KEY (evidence_protocol_id, version),
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('operator', 'statistician', 'approver', 'auditor', 'admin')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS user_qualifications (
    user_id TEXT NOT NULL REFERENCES users(user_id),
    task_family TEXT NOT NULL,
    granted_by TEXT NOT NULL REFERENCES users(user_id),
    granted_at TEXT NOT NULL,
    revoked_at TEXT,
    PRIMARY KEY (user_id, task_family)
);

CREATE TABLE IF NOT EXISTS capture_devices (
    device_id TEXT PRIMARY KEY,
    model_name TEXT NOT NULL,
    vendor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS builds (
    build_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES capture_devices(device_id),
    version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (device_id, version),
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    evidence_protocol_id TEXT NOT NULL,
    evidence_protocol_version INTEGER NOT NULL,
    build_id TEXT NOT NULL REFERENCES builds(build_id),
    state TEXT NOT NULL CHECK (state IN ('draft', 'running', 'sealed', 'analyzing', 'analyzed', 'decided')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    started_at TEXT,
    sealed_at TEXT,
    FOREIGN KEY (evidence_protocol_id, evidence_protocol_version) REFERENCES evidence_protocol_catalog(evidence_protocol_id, version)
);

CREATE TABLE IF NOT EXISTS evidence_items (
    evidence_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    source_batch TEXT NOT NULL,
    source_row TEXT NOT NULL,
    device_id TEXT NOT NULL REFERENCES capture_devices(device_id),
    evidence_group_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    indicators_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    imported_by TEXT NOT NULL REFERENCES users(user_id),
    imported_at TEXT NOT NULL,
    UNIQUE (batch_id, source_batch, source_row)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS exclusion_requests (
    exclusion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_item_id INTEGER NOT NULL REFERENCES evidence_items(evidence_item_id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'revoked')),
    reason TEXT NOT NULL,
    requested_by TEXT NOT NULL REFERENCES users(user_id),
    requested_at TEXT NOT NULL,
    reviewed_by TEXT REFERENCES users(user_id),
    reviewed_at TEXT,
    review_note TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_exclusion_per_evidence_item
ON exclusion_requests(evidence_item_id)
WHERE status IN ('pending', 'approved');

CREATE TABLE IF NOT EXISTS analysis_jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    batch_revision INTEGER NOT NULL,
    task_family TEXT,
    state TEXT NOT NULL CHECK (state IN ('queued', 'leased', 'succeeded', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    claimed_by TEXT REFERENCES users(user_id),
    lease_expires_at TEXT,
    last_lease_expires_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (batch_id, batch_revision)
);

CREATE TABLE IF NOT EXISTS analyses (
    analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    batch_revision INTEGER NOT NULL,
    evidence_protocol_sha256 TEXT NOT NULL CHECK (length(evidence_protocol_sha256) = 64),
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    algorithm_version TEXT NOT NULL,
    seed INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, batch_revision, input_sha256)
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    analysis_id INTEGER NOT NULL REFERENCES analyses(analysis_id),
    decision TEXT NOT NULL CHECK (decision IN ('needs_more_data', 'approved', 'rejected')),
    reason TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES users(user_id),
    decided_at TEXT NOT NULL,
    UNIQUE (batch_id, analysis_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lease_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES analysis_jobs(job_id),
    batch_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'acquired', 'renewed', 'released', 'taken_over', 'rejected', 'succeeded', 'failed'
    )),
    reason_code TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES users(user_id),
    lease_owner TEXT,
    lease_expires_at TEXT,
    request_key TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (job_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS job_requests (
    scope TEXT NOT NULL,
    request_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    job_id INTEGER REFERENCES analysis_jobs(job_id),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, request_key)
);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "evidence_protocol_catalog", "users", "user_qualifications",
    "capture_devices", "builds", "batches",
    "evidence_items", "idempotency_keys", "exclusion_requests", "analysis_jobs",
    "analyses", "decisions", "audit_events", "lease_events", "job_requests",
})

# 旧版本数据库需要补齐的 analysis_jobs 列：列名 -> 建列语句。
_JOB_MIGRATION_COLUMNS = (
    ("task_family", "ALTER TABLE analysis_jobs ADD COLUMN task_family TEXT"),
    ("claimed_by", "ALTER TABLE analysis_jobs ADD COLUMN claimed_by TEXT REFERENCES users(user_id)"),
    ("last_lease_expires_at", "ALTER TABLE analysis_jobs ADD COLUMN last_lease_expires_at TEXT"),
)

_USERS_TABLE_SQL = """
CREATE TABLE users_new (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('operator', 'statistician', 'approver', 'auditor', 'admin')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
)
"""


def _users_check_allows_admin(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    # 全新数据库尚无 users 表，由 SCHEMA_SQL 直接按最新定义创建，无需迁移。
    return row is None or "'admin'" in (row[0] or "")


def _migrate_users_table(connection: sqlite3.Connection) -> None:
    """旧版 users 表角色约束缺少 admin：按 SQLite 推荐流程重建并保留数据。"""

    if _users_check_allows_admin(connection):
        return
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        with transaction(connection, immediate=True):
            connection.execute(_USERS_TABLE_SQL)
            connection.execute(
                "INSERT INTO users_new(user_id,display_name,role,active) "
                "SELECT user_id,display_name,role,active FROM users"
            )
            connection.execute("DROP TABLE users")
            connection.execute("ALTER TABLE users_new RENAME TO users")
        bad_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
        if bad_rows:
            raise sqlite3.IntegrityError(f"迁移后存在悬挂外键: {bad_rows}")
    finally:
        if foreign_keys:
            connection.execute("PRAGMA foreign_keys=ON")


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。"""

    connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row["name"] == column for row in connection.execute(f"PRAGMA table_info({table})"))


def initialize(connection: sqlite3.Connection) -> None:
    """初始化基础资料表，重复执行不改变已有数据。"""

    # 先升级旧版 users 表角色约束，executescript 才能安全创建引用它的新表。
    _migrate_users_table(connection)
    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        # 兼容 v2 数据库：为已存在的 analysis_jobs 补齐新列，旧任务的 task_family 未知故允许为空。
        for column, ddl in _JOB_MIGRATION_COLUMNS:
            if not _column_exists(connection, "analysis_jobs", column):
                connection.execute(ddl)
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
