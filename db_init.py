import os
import sqlite3
import logging

log = logging.getLogger("ShieldDB")

DB_DIR = "data"
DB_PATH = os.path.join(DB_DIR, "shield.db")


def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.executescript("""
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
    """)

    conn.commit()
    conn.close()
    log.info("Database initialised at %s", DB_PATH)
    print(f"[DB] Database ready: {DB_PATH}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
