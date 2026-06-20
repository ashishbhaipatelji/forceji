"""Database initialisation for Shield Bot.

Supports two backends:
  - PostgreSQL  when DATABASE_URL env var is set (Railway, VPS, etc.)
  - SQLite      otherwise (local development, Replit)
"""
import os
import logging

log = logging.getLogger("ShieldDB")

DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ── PostgreSQL ───────────────────────────────────────────────
def _init_postgres(url: str) -> None:
    import psycopg2

    conn = psycopg2.connect(url)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS muted_users (
            id          SERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL,
            chat_id     BIGINT NOT NULL,
            muted_at    TIMESTAMPTZ DEFAULT NOW(),
            unmuted_at  TIMESTAMPTZ,
            UNIQUE(user_id, chat_id)
        );
        CREATE TABLE IF NOT EXISTS bot_logs (
            id          SERIAL PRIMARY KEY,
            level       TEXT NOT NULL,
            message     TEXT NOT NULL,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS stats (
            id          SERIAL PRIMARY KEY,
            event       TEXT NOT NULL,
            chat_id     BIGINT,
            user_id     BIGINT,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS users (
            id          SERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL UNIQUE,
            first_name  TEXT,
            username    TEXT,
            first_seen  TIMESTAMPTZ DEFAULT NOW(),
            last_seen   TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS warnings (
            id          SERIAL PRIMARY KEY,
            user_id     BIGINT NOT NULL,
            chat_id     BIGINT NOT NULL,
            warned_by   BIGINT,
            reason      TEXT DEFAULT '',
            created_at  TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    conn.commit()
    cur.close()
    conn.close()
    log.info("PostgreSQL database ready")
    print("[DB] PostgreSQL database ready")


# ── SQLite ───────────────────────────────────────────────────
def _init_sqlite() -> None:
    import sqlite3

    db_dir = "data"
    db_path = os.path.join(db_dir, "shield.db")
    os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS muted_users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            chat_id     INTEGER NOT NULL,
            muted_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            unmuted_at  TIMESTAMP,
            UNIQUE(user_id, chat_id)
        );
        CREATE TABLE IF NOT EXISTS bot_logs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            level       TEXT NOT NULL,
            message     TEXT NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS stats (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event       TEXT NOT NULL,
            chat_id     INTEGER,
            user_id     INTEGER,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL UNIQUE,
            first_name  TEXT,
            username    TEXT,
            first_seen  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS warnings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            chat_id     INTEGER NOT NULL,
            warned_by   INTEGER,
            reason      TEXT DEFAULT '',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()
    log.info("SQLite database ready at %s", db_path)
    print(f"[DB] SQLite database ready: {db_path}")


# ── Entry point ──────────────────────────────────────────────
def init_db() -> None:
    if DATABASE_URL:
        log.info("DATABASE_URL detected – using PostgreSQL")
        _init_postgres(DATABASE_URL)
    else:
        log.info("No DATABASE_URL – using SQLite")
        _init_sqlite()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
