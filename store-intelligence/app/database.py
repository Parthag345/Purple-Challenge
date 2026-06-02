import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator, List, Optional, Tuple
logger = logging.getLogger(__name__)
DB_PATH = os.getenv('DB_PATH', 'data/store_intelligence.db')

def get_db_path() -> str:
    return DB_PATH

@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA synchronous=NORMAL')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else '.', exist_ok=True)
    logger.info(f'Initializing database at {DB_PATH}')
    with get_db() as conn:
        conn.executescript("\n            -- ─── Events Table (append-only, idempotent by event_id) ────────────────\n            CREATE TABLE IF NOT EXISTS events (\n                event_id        TEXT PRIMARY KEY,\n                store_id        TEXT NOT NULL,\n                camera_id       TEXT NOT NULL,\n                visitor_id      TEXT NOT NULL,\n                event_type      TEXT NOT NULL,\n                timestamp       TEXT NOT NULL,\n                zone_id         TEXT,\n                dwell_ms        INTEGER DEFAULT 0,\n                is_staff        INTEGER DEFAULT 0,\n                confidence      REAL NOT NULL,\n                queue_depth     INTEGER,\n                sku_zone        TEXT,\n                session_seq     INTEGER DEFAULT 0,\n                ingested_at     TEXT DEFAULT (datetime('now')),\n                raw_json        TEXT\n            );\n\n            CREATE INDEX IF NOT EXISTS idx_events_store_ts\n                ON events(store_id, timestamp);\n            CREATE INDEX IF NOT EXISTS idx_events_visitor\n                ON events(visitor_id, timestamp);\n            CREATE INDEX IF NOT EXISTS idx_events_type\n                ON events(event_type, store_id, timestamp);\n\n            -- ─── Sessions Table (materialized visitor sessions) ──────────────────\n            CREATE TABLE IF NOT EXISTS sessions (\n                session_id      TEXT PRIMARY KEY,\n                store_id        TEXT NOT NULL,\n                visitor_id      TEXT NOT NULL,\n                entry_time      TEXT,\n                exit_time       TEXT,\n                is_staff        INTEGER DEFAULT 0,\n                reentry_count   INTEGER DEFAULT 0,\n                zones_visited   TEXT,       -- JSON array of zone IDs\n                reached_billing INTEGER DEFAULT 0,\n                did_purchase    INTEGER DEFAULT 0,\n                total_dwell_ms  INTEGER DEFAULT 0,\n                updated_at      TEXT DEFAULT (datetime('now'))\n            );\n\n            CREATE INDEX IF NOT EXISTS idx_sessions_store\n                ON sessions(store_id, entry_time);\n            CREATE INDEX IF NOT EXISTS idx_sessions_visitor\n                ON sessions(visitor_id);\n\n            -- ─── POS Transactions ────────────────────────────────────────────────\n            CREATE TABLE IF NOT EXISTS pos_transactions (\n                transaction_id  TEXT PRIMARY KEY,\n                store_id        TEXT NOT NULL,\n                timestamp       TEXT NOT NULL,\n                basket_value_inr REAL NOT NULL,\n                correlated_visitor_id TEXT,\n                ingested_at     TEXT DEFAULT (datetime('now'))\n            );\n\n            CREATE INDEX IF NOT EXISTS idx_pos_store_ts\n                ON pos_transactions(store_id, timestamp);\n\n            -- ─── Anomaly Log ─────────────────────────────────────────────────────\n            CREATE TABLE IF NOT EXISTS anomaly_log (\n                anomaly_id      TEXT PRIMARY KEY,\n                anomaly_type    TEXT NOT NULL,\n                severity        TEXT NOT NULL,\n                store_id        TEXT NOT NULL,\n                zone_id         TEXT,\n                detected_at     TEXT NOT NULL,\n                resolved_at     TEXT,\n                description     TEXT,\n                suggested_action TEXT,\n                metric_value    REAL,\n                threshold_value REAL\n            );\n\n            CREATE INDEX IF NOT EXISTS idx_anomaly_store\n                ON anomaly_log(store_id, detected_at);\n\n            -- ─── Metric Snapshots (for 7-day trend comparison) ──────────────────\n            CREATE TABLE IF NOT EXISTS metric_snapshots (\n                snapshot_id     TEXT PRIMARY KEY,\n                store_id        TEXT NOT NULL,\n                snapshot_date   TEXT NOT NULL,    -- YYYY-MM-DD\n                unique_visitors INTEGER,\n                conversion_rate REAL,\n                avg_dwell_seconds REAL,\n                total_revenue   REAL,\n                created_at      TEXT DEFAULT (datetime('now'))\n            );\n\n            CREATE INDEX IF NOT EXISTS idx_snapshots_store\n                ON metric_snapshots(store_id, snapshot_date);\n        ")
    logger.info('Database initialized successfully')

def check_db_health() -> bool:
    try:
        with get_db() as conn:
            conn.execute('SELECT 1')
        return True
    except Exception as e:
        logger.error(f'Database health check failed: {e}')
        return False