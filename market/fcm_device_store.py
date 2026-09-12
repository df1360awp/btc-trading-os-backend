import sqlite3
from datetime import datetime, timezone

DEFAULT_DB_PATH = "/opt/btc-trading-os/market.db"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path=DEFAULT_DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_fcm_device_table(db_path=DEFAULT_DB_PATH):
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fcm_devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                installation_id TEXT NOT NULL UNIQUE,
                token TEXT NOT NULL UNIQUE,
                platform TEXT NOT NULL DEFAULT 'android',
                active INTEGER NOT NULL DEFAULT 1,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def register_device(
    installation_id,
    token,
    platform="android",
    db_path=DEFAULT_DB_PATH,
):
    if not installation_id or not installation_id.strip():
        raise ValueError("installation_id is required")

    if not token or not token.strip():
        raise ValueError("token is required")

    installation_id = installation_id.strip()
    token = token.strip()
    platform = platform.strip() if platform else "android"

    now = _now_iso()

    init_fcm_device_table(db_path)

    with _connect(db_path) as conn:
        conn.execute(
            """
            DELETE FROM fcm_devices
            WHERE token = ?
              AND installation_id != ?
            """,
            (token, installation_id),
        )

        conn.execute(
            """
            INSERT INTO fcm_devices (
                installation_id,
                token,
                platform,
                active,
                last_error,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, 1, NULL, ?, ?)
            ON CONFLICT(installation_id)
            DO UPDATE SET
                token = excluded.token,
                platform = excluded.platform,
                active = 1,
                last_error = NULL,
                updated_at = excluded.updated_at
            """,
            (
                installation_id,
                token,
                platform,
                now,
                now,
            ),
        )

        conn.commit()

        row = conn.execute(
            """
            SELECT *
            FROM fcm_devices
            WHERE installation_id = ?
            """,
            (installation_id,),
        ).fetchone()

    return dict(row)


def list_active_devices(db_path=DEFAULT_DB_PATH):
    init_fcm_device_table(db_path)

    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM fcm_devices
            WHERE active = 1
            ORDER BY id ASC
            """
        ).fetchall()

    return [dict(row) for row in rows]


def list_devices(db_path=DEFAULT_DB_PATH):
    init_fcm_device_table(db_path)

    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM fcm_devices
            ORDER BY id DESC
            """
        ).fetchall()

    return [dict(row) for row in rows]


def deactivate_token(
    token,
    error_message=None,
    db_path=DEFAULT_DB_PATH,
):
    now = _now_iso()

    with _connect(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE fcm_devices
            SET
                active = 0,
                last_error = ?,
                updated_at = ?
            WHERE token = ?
            """,
            (
                error_message,
                now,
                token,
            ),
        )

        conn.commit()

    return cursor.rowcount
