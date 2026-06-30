"""
Database layer for Trade_Eval.

Uses aiosqlite so all operations are non-blocking and safe to await inside
Discord command handlers. Call await db.connect() before use (handled by
TradeEval.cog_load) and await db.close() on teardown (handled by cog_unload).
"""

import json
import logging
import aiosqlite
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger("trade_eval.database")


class Database:

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        # Ensure the data directory exists before SQLite tries to open the file
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None

    # -- Lifecycle ----------------------------------------------------------

    async def connect(self):
        """Open the connection and initialise the schema. Call once on cog load."""
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._init_schema()
        logger.info(f"Database connected: {self.db_path}")

    async def close(self):
        """Close the connection gracefully. Call on cog unload."""
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("Database connection closed")

    async def _init_schema(self):
        """Create tables if they do not already exist."""
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_contexts (
                user_id       INTEGER PRIMARY KEY,
                draft_mode    TEXT    DEFAULT 'mock',
                my_team_name  TEXT,
                updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS draft_cache (
                draft_mode   TEXT,
                team_name    TEXT,
                picks        TEXT,
                last_synced  DATETIME,
                PRIMARY KEY (draft_mode, team_name)
            );

            CREATE TABLE IF NOT EXISTS sync_status (
                draft_mode           TEXT PRIMARY KEY,
                sheet_url            TEXT,
                last_sync            DATETIME,
                sync_status          TEXT,
                error_message        TEXT,
                consecutive_failures INTEGER DEFAULT 0
            );
        """)
        await self._conn.commit()

        # Migration: add column to databases created before this field existed.
        # ALTER TABLE fails silently if the column is already present.
        try:
            await self._conn.execute(
                "ALTER TABLE sync_status ADD COLUMN consecutive_failures INTEGER DEFAULT 0"
            )
            await self._conn.commit()
        except Exception:
            pass  # Column already exists — expected on all runs after the first

    # -- Draft cache --------------------------------------------------------

    async def save_team_picks(self, mode: str, team_name: str, picks: list[int]):
        """
        Insert or update a team's pick list in the cache.
        picks is a list of overall pick numbers e.g. [18, 50, 82, 114, 146, 178, 210].
        """
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute("""
            INSERT INTO draft_cache (draft_mode, team_name, picks, last_synced)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(draft_mode, team_name) DO UPDATE SET
                picks       = excluded.picks,
                last_synced = excluded.last_synced
        """, (mode, team_name, json.dumps(picks), now))
        await self._conn.commit()

    async def get_team_picks(self, mode: str, team_name: str) -> list[int] | None:
        """Return a team's picks for a given mode, or None if not cached."""
        async with self._conn.execute(
            "SELECT picks FROM draft_cache WHERE draft_mode = ? AND team_name = ?",
            (mode, team_name)
        ) as cursor:
            row = await cursor.fetchone()
            return json.loads(row["picks"]) if row else None

    async def get_all_teams(self, mode: str) -> dict[str, list[int]]:
        """
        Return the full cached draft order for a mode as {team_name: [picks]}.
        Used on startup to load everything into memory at once.
        """
        async with self._conn.execute(
            "SELECT team_name, picks FROM draft_cache WHERE draft_mode = ?",
            (mode,)
        ) as cursor:
            rows = await cursor.fetchall()
            return {row["team_name"]: json.loads(row["picks"]) for row in rows}

    async def get_last_sync(self, mode: str) -> datetime | None:
        """
        Return the datetime of the last successful sync for a mode, or None.
        Datetime is returned as UTC-aware.
        """
        async with self._conn.execute(
            "SELECT last_sync FROM sync_status WHERE draft_mode = ?",
            (mode,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row or not row["last_sync"]:
                return None
            dt = datetime.fromisoformat(row["last_sync"])
            # Ensure UTC-aware even if stored without tzinfo
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

    async def update_sync_status(
        self,
        mode: str,
        status: str,               # 'success' | 'failed' | 'partial'
        url: str = None,
        error: str = None
    ):
        """
        Record the outcome of a sync attempt.
        COALESCE on url means a failed sync won't blank out a previously stored URL.
        """
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute("""
            INSERT INTO sync_status (draft_mode, sheet_url, last_sync, sync_status, error_message)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(draft_mode) DO UPDATE SET
                sheet_url     = COALESCE(excluded.sheet_url, sheet_url),
                last_sync     = excluded.last_sync,
                sync_status   = excluded.sync_status,
                error_message = excluded.error_message
        """, (mode, url, now, status, error))
        await self._conn.commit()

    # -- User contexts ------------------------------------------------------

    async def save_user_context(self, user_id: int, mode: str, my_team: str):
        """
        Insert or update a user's draft mode and team.
        updated_at is explicitly set on both insert and update so it always
        reflects the most recent change (DEFAULT only fires on fresh INSERTs).
        """
        await self._conn.execute("""
            INSERT INTO user_contexts (user_id, draft_mode, my_team_name, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                draft_mode   = excluded.draft_mode,
                my_team_name = excluded.my_team_name,
                updated_at   = CURRENT_TIMESTAMP
        """, (user_id, mode, my_team))
        await self._conn.commit()

    async def get_user_context(self, user_id: int) -> dict | None:
        """Return {"mode": str, "my_team": str} for a user, or None if not registered."""
        async with self._conn.execute(
            "SELECT draft_mode, my_team_name FROM user_contexts WHERE user_id = ?",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return {"mode": row["draft_mode"], "my_team": row["my_team_name"]} if row else None

    # -- Consecutive failure tracking ---------------------------------------

    async def get_consecutive_failures(self, mode: str) -> int:
        """Return the current consecutive failure count for a mode."""
        async with self._conn.execute(
            "SELECT consecutive_failures FROM sync_status WHERE draft_mode = ?",
            (mode,)
        ) as cursor:
            row = await cursor.fetchone()
            return row["consecutive_failures"] if row else 0

    async def increment_consecutive_failures(self, mode: str) -> int:
        """
        Increment the failure counter for a mode and return the new count.
        Creates the row if it does not yet exist.
        """
        await self._conn.execute("""
            INSERT INTO sync_status (draft_mode, consecutive_failures)
            VALUES (?, 1)
            ON CONFLICT(draft_mode) DO UPDATE SET
                consecutive_failures = consecutive_failures + 1
        """, (mode,))
        await self._conn.commit()
        return await self.get_consecutive_failures(mode)

    async def reset_consecutive_failures(self, mode: str):
        """Reset the failure counter to 0 after a successful sync or manual override."""
        await self._conn.execute("""
            INSERT INTO sync_status (draft_mode, consecutive_failures)
            VALUES (?, 0)
            ON CONFLICT(draft_mode) DO UPDATE SET
                consecutive_failures = 0
        """, (mode,))
        await self._conn.commit()
