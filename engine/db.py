"""Database Layer: Unified support for PostgreSQL and SQLite.
Provides connection management, schema initialization, and query abstractions."""
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

ROOT = Path(__file__).resolve().parent.parent
DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_PATH = os.getenv("DB_PATH", str(ROOT / "resolver.db"))

# SQL DDL for SQLite / Postgres compatible initialization
SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS capability_profile (
    customer_id TEXT,
    capability TEXT,
    provider TEXT DEFAULT 'FALLBACK',
    fallback TEXT,
    healthy INTEGER DEFAULT 1,
    last_ok TEXT,
    max_age_min INTEGER DEFAULT 15,
    PRIMARY KEY (customer_id, capability)
);

CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    customer TEXT,
    po_number TEXT,
    supplier TEXT DEFAULT '',
    status TEXT,
    case_class TEXT,
    gap INTEGER DEFAULT 0,
    hold REAL DEFAULT 0.0,
    action TEXT,
    decision TEXT,
    reasons TEXT,
    draft TEXT,
    attempts INTEGER DEFAULT 0,
    created TEXT,
    updated_at TEXT,
    UNIQUE(customer, po_number)
);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT,
    kind TEXT,
    data TEXT,
    provenance TEXT,
    method TEXT,
    confidence REAL DEFAULT 1.0,
    is_claim INTEGER DEFAULT 0,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT,
    at TEXT,
    type TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT,
    at TEXT,
    case_class TEXT,
    ok INTEGER,
    why TEXT,
    status TEXT,
    source TEXT,
    details TEXT
);

CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id TEXT,
    supplier_name TEXT,
    supplier_email TEXT,
    msme_flag INTEGER DEFAULT 0,
    tolerance_pct REAL DEFAULT 2.0,
    price_tolerance_pct REAL DEFAULT 1.0,
    created_at TEXT,
    UNIQUE(customer_id, supplier_name)
);

CREATE TABLE IF NOT EXISTS emails (
    message_id TEXT PRIMARY KEY,
    case_id TEXT,
    subject TEXT,
    body TEXT,
    method TEXT,
    received_at TEXT
);

CREATE TABLE IF NOT EXISTS action_keys (
    key TEXT PRIMARY KEY,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS path_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT,
    customer TEXT,
    capability TEXT,
    path TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS health (
    customer TEXT,
    capability TEXT,
    healthy INTEGER DEFAULT 1,
    updated_at TEXT,
    PRIMARY KEY (customer, capability)
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
"""

SCHEMA_POSTGRES = """
CREATE TABLE IF NOT EXISTS capability_profile (
    customer_id VARCHAR(64) NOT NULL,
    capability VARCHAR(64) NOT NULL,
    provider VARCHAR(32) NOT NULL DEFAULT 'FALLBACK',
    fallback VARCHAR(64),
    healthy BOOLEAN DEFAULT TRUE,
    last_ok TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    max_age_min INT DEFAULT 15,
    PRIMARY KEY (customer_id, capability)
);

CREATE TABLE IF NOT EXISTS cases (
    id VARCHAR(64) PRIMARY KEY,
    customer VARCHAR(64) NOT NULL,
    po_number VARCHAR(64) NOT NULL,
    supplier VARCHAR(255) DEFAULT '',
    status VARCHAR(32) NOT NULL,
    case_class VARCHAR(64) NOT NULL,
    gap INT DEFAULT 0,
    hold REAL DEFAULT 0.0,
    action VARCHAR(64),
    decision VARCHAR(32),
    reasons TEXT,
    draft TEXT,
    attempts INT DEFAULT 0,
    created VARCHAR(64),
    updated_at VARCHAR(64),
    CONSTRAINT uq_customer_po UNIQUE (customer, po_number)
);

CREATE TABLE IF NOT EXISTS evidence (
    id SERIAL PRIMARY KEY,
    case_id VARCHAR(64) NOT NULL,
    kind VARCHAR(64) NOT NULL,
    data TEXT NOT NULL,
    provenance VARCHAR(255),
    method VARCHAR(64),
    confidence REAL DEFAULT 1.0,
    is_claim INT DEFAULT 0,
    fetched_at VARCHAR(64)
);

CREATE TABLE IF NOT EXISTS events (
    id SERIAL PRIMARY KEY,
    case_id VARCHAR(64) NOT NULL,
    at VARCHAR(64) NOT NULL,
    type VARCHAR(64) NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS verifications (
    id SERIAL PRIMARY KEY,
    case_id VARCHAR(64) NOT NULL,
    at VARCHAR(64) NOT NULL,
    case_class VARCHAR(64) NOT NULL,
    ok BOOLEAN NOT NULL,
    why TEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    source VARCHAR(64),
    details TEXT
);

CREATE TABLE IF NOT EXISTS suppliers (
    id SERIAL PRIMARY KEY,
    customer_id VARCHAR(64) NOT NULL,
    supplier_name VARCHAR(255) NOT NULL,
    supplier_email VARCHAR(255),
    msme_flag INT DEFAULT 0,
    tolerance_pct REAL DEFAULT 2.0,
    price_tolerance_pct REAL DEFAULT 1.0,
    created_at VARCHAR(64),
    CONSTRAINT uq_customer_supplier UNIQUE (customer_id, supplier_name)
);

CREATE TABLE IF NOT EXISTS emails (
    message_id VARCHAR(255) PRIMARY KEY,
    case_id VARCHAR(64),
    subject TEXT,
    body TEXT,
    method VARCHAR(64),
    received_at VARCHAR(64)
);

CREATE TABLE IF NOT EXISTS action_keys (
    key VARCHAR(255) PRIMARY KEY,
    created_at VARCHAR(64)
);

CREATE TABLE IF NOT EXISTS path_log (
    id SERIAL PRIMARY KEY,
    at VARCHAR(64) NOT NULL,
    customer VARCHAR(64) NOT NULL,
    capability VARCHAR(64) NOT NULL,
    path VARCHAR(32) NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS health (
    customer VARCHAR(64) NOT NULL,
    capability VARCHAR(64) NOT NULL,
    healthy INT DEFAULT 1,
    updated_at VARCHAR(64),
    PRIMARY KEY (customer, capability)
);

CREATE TABLE IF NOT EXISTS config (
    key VARCHAR(64) PRIMARY KEY,
    value TEXT,
    updated_at VARCHAR(64)
);
"""

# Track active SQLite connections so they can be closed cleanly on Windows
_ACTIVE_SQLITE_CONNECTIONS = set()


def is_postgres() -> bool:
    url = os.getenv("DATABASE_URL", "").strip().lower()
    return url.startswith("postgres://") or url.startswith("postgresql://")


def _get_pg_conn():
    try:
        import psycopg2
        import psycopg2.extras
        url = os.getenv("DATABASE_URL")
        conn = psycopg2.connect(url)
        conn.autocommit = False
        return conn
    except Exception as e:
        # Fall back to sqlite if postgres is configured but unreachable
        return None


class DatabaseAdapter:
    """Unified DB wrapper providing dict rows and uniform parameter syntax."""
    def __init__(self, is_pg: bool = False, raw_conn=None):
        self.is_pg = is_pg
        self.conn = raw_conn

    def _convert_query(self, query: str) -> str:
        if self.is_pg:
            # Convert SQLite-style ? placeholders to %s for psycopg2
            return query.replace("?", "%s")
        return query

    def execute(self, query: str, params: Union[Tuple, List] = ()):
        q = self._convert_query(query)
        cur = self.conn.cursor()
        cur.execute(q, params)
        return cur

    def executemany(self, query: str, param_list: List[Union[Tuple, List]]):
        q = self._convert_query(query)
        cur = self.conn.cursor()
        cur.executemany(q, param_list)
        return cur

    def executescript(self, script: str):
        if self.is_pg:
            with self.conn.cursor() as cur:
                cur.execute(script)
            self.conn.commit()
        else:
            self.conn.executescript(script)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass
        if not self.is_pg:
            _ACTIVE_SQLITE_CONNECTIONS.discard(self.conn)


def get_connection() -> DatabaseAdapter:
    """Returns an active DatabaseAdapter configured for PostgreSQL or SQLite."""
    if is_postgres():
        pg_conn = _get_pg_conn()
        if pg_conn:
            adapter = DatabaseAdapter(is_pg=True, raw_conn=pg_conn)
            # Ensure schema exists on postgres
            try:
                adapter.executescript(SCHEMA_POSTGRES)
            except Exception:
                adapter.rollback()
            return adapter

    # SQLite connection
    db_path = os.getenv("DB_PATH", str(ROOT / "resolver.db"))
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, timeout=30.0)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA_SQLITE)
    _ACTIVE_SQLITE_CONNECTIONS.add(con)
    return DatabaseAdapter(is_pg=False, raw_conn=con)


@contextmanager
def db_session():
    """Context manager for database operations."""
    adapter = get_connection()
    try:
        yield adapter
        adapter.commit()
    except Exception:
        adapter.rollback()
        raise
    finally:
        adapter.close()


def close_all_connections():
    """Close all open SQLite connections to release Windows file locks."""
    for conn in list(_ACTIVE_SQLITE_CONNECTIONS):
        try:
            conn.close()
        except Exception:
            pass
    _ACTIVE_SQLITE_CONNECTIONS.clear()


def reset_database():
    """Cleans all tables and resets seed data for tests or demo."""
    close_all_connections()
    db_path = os.getenv("DB_PATH", str(ROOT / "resolver.db"))
    
    if not is_postgres() and os.path.exists(db_path):
        try:
            os.remove(db_path)
        except (PermissionError, OSError):
            # If still locked on Windows, truncate tables
            with db_session() as con:
                tables = ["cases", "evidence", "events", "verifications", "suppliers",
                          "emails", "action_keys", "path_log", "health", "config", "capability_profile"]
                for t in tables:
                    try:
                        con.execute(f"DELETE FROM {t}")
                    except Exception:
                        pass
    elif is_postgres():
        with db_session() as con:
            tables = ["cases", "evidence", "events", "verifications", "suppliers",
                      "emails", "action_keys", "path_log", "health", "config", "capability_profile"]
            for t in tables:
                try:
                    con.execute(f"TRUNCATE TABLE {t} CASCADE")
                except Exception:
                    pass
