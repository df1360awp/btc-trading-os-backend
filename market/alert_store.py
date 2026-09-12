import sqlite3


class AlertStore:
    def __init__(self, db_path):
        self.db_path = db_path
        self._init_db()


    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


    def _init_db(self):
        conn = self._connect()
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS price_alert_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 1,
                reference_price REAL,
                upper_distance REAL NOT NULL DEFAULT 400,
                lower_distance REAL NOT NULL DEFAULT 400,
                repeat_seconds INTEGER NOT NULL DEFAULT 120
            )
        """)

        cursor.execute("""
            INSERT OR IGNORE INTO price_alert_config (
                id,
                enabled,
                reference_price,
                upper_distance,
                lower_distance,
                repeat_seconds
            )
            VALUES (
                1,
                1,
                NULL,
                400,
                400,
                120
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT NOT NULL,
                direction TEXT,
                reason TEXT,
                current_price REAL,
                message TEXT,
                created_at INTEGER NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0,
                acknowledged_at INTEGER
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_alert_events_created
            ON alert_events(created_at)
        """)

        conn.commit()
        conn.close()


    def get_price_config(self):
        conn = self._connect()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                enabled,
                reference_price,
                upper_distance,
                lower_distance,
                repeat_seconds
            FROM price_alert_config
            WHERE id = 1
        """)

        row = cursor.fetchone()
        conn.close()

        if row is None:
            return None

        return {
            "enabled": bool(row["enabled"]),
            "reference_price": row["reference_price"],
            "upper_distance": float(
                row["upper_distance"]
            ),
            "lower_distance": float(
                row["lower_distance"]
            ),
            "repeat_seconds": int(
                row["repeat_seconds"]
            ),
        }


    def update_price_config(
        self,
        reference_price=None,
        upper_distance=None,
        lower_distance=None,
        repeat_seconds=None,
        enabled=None,
    ):
        current = self.get_price_config()

        if current is None:
            raise RuntimeError(
                "Price alert config does not exist"
            )

        new_reference_price = (
            float(reference_price)
            if reference_price is not None
            else current["reference_price"]
        )

        new_upper_distance = (
            float(upper_distance)
            if upper_distance is not None
            else current["upper_distance"]
        )

        new_lower_distance = (
            float(lower_distance)
            if lower_distance is not None
            else current["lower_distance"]
        )

        new_repeat_seconds = (
            int(repeat_seconds)
            if repeat_seconds is not None
            else current["repeat_seconds"]
        )

        new_enabled = (
            bool(enabled)
            if enabled is not None
            else current["enabled"]
        )

        if new_upper_distance <= 0:
            raise ValueError(
                "upper_distance must be greater than 0"
            )

        if new_lower_distance <= 0:
            raise ValueError(
                "lower_distance must be greater than 0"
            )

        if new_repeat_seconds <= 0:
            raise ValueError(
                "repeat_seconds must be greater than 0"
            )

        conn = self._connect()
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE price_alert_config
            SET
                enabled = ?,
                reference_price = ?,
                upper_distance = ?,
                lower_distance = ?,
                repeat_seconds = ?
            WHERE id = 1
        """, (
            1 if new_enabled else 0,
            new_reference_price,
            new_upper_distance,
            new_lower_distance,
            new_repeat_seconds,
        ))

        conn.commit()
        conn.close()

        return self.get_price_config()


    def save_alert_event(
        self,
        alert_type,
        direction,
        reason,
        current_price,
        message,
        created_at,
    ):
        conn = self._connect()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO alert_events (
                alert_type,
                direction,
                reason,
                current_price,
                message,
                created_at,
                acknowledged
            )
            VALUES (?, ?, ?, ?, ?, ?, 0)
        """, (
            alert_type,
            direction,
            reason,
            current_price,
            message,
            created_at,
        ))

        event_id = cursor.lastrowid

        conn.commit()
        conn.close()

        return event_id


    def list_alert_events(
        self,
        limit=50,
    ):
        conn = self._connect()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                id,
                alert_type,
                direction,
                reason,
                current_price,
                message,
                created_at,
                acknowledged,
                acknowledged_at
            FROM alert_events
            ORDER BY id DESC
            LIMIT ?
        """, (
            int(limit),
        ))

        rows = cursor.fetchall()
        conn.close()

        events = []

        for row in rows:
            events.append({
                "id": row["id"],
                "alert_type": row["alert_type"],
                "direction": row["direction"],
                "reason": row["reason"],
                "current_price": row["current_price"],
                "message": row["message"],
                "created_at": row["created_at"],
                "acknowledged": bool(
                    row["acknowledged"]
                ),
                "acknowledged_at": (
                    row["acknowledged_at"]
                ),
            })

        return events


    def acknowledge_event(
        self,
        event_id,
        acknowledged_at=None,
    ):
        conn = self._connect()
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE alert_events
            SET
                acknowledged = 1,
                acknowledged_at = ?
            WHERE id = ?
        """, (
            acknowledged_at,
            int(event_id),
        ))

        changed = cursor.rowcount

        conn.commit()
        conn.close()

        return changed > 0


    def acknowledge_current_price_episode(
        self,
        acknowledged_at,
    ):
        """
        将当前这一轮价格报警全部标记为已知晓。

        一轮报警定义为：
        最近一次 PRICE/CROSS 开始，
        到当前为止产生的所有 PRICE 事件。

        因此 CROSS 和后续 REPEAT 会一起确认，
        上一轮历史报警不会被误改。
        """

        conn = self._connect()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT MAX(id)
            FROM alert_events
            WHERE
                alert_type = 'PRICE'
                AND reason = 'CROSS'
        """)

        row = cursor.fetchone()

        if row is None or row[0] is None:
            conn.close()
            return 0

        latest_cross_id = int(row[0])

        cursor.execute("""
            UPDATE alert_events
            SET
                acknowledged = 1,
                acknowledged_at = ?
            WHERE
                alert_type = 'PRICE'
                AND id >= ?
                AND acknowledged = 0
        """, (
            int(acknowledged_at),
            latest_cross_id,
        ))

        changed = cursor.rowcount

        conn.commit()
        conn.close()

        return changed
