"""
NFL Locks database layer.

Uses aiosqlite for non-blocking I/O inside Discord's async event loop.
All Discord user IDs and guild IDs are stored as TEXT to avoid 64-bit integer
overflow — SQLite integers are signed 64-bit and Discord snowflakes can exceed
the signed range on some platforms.

Migration note
--------------
The current reaction system stores user.name (mutable) rather than user.id.
During initial migration from JSON, user_id is populated with the username
string (same value as user_name). This preserves all historical data while
keeping the UNIQUE constraint functional.

Once reactions.py is updated to pass user.id, set user_id = str(user.id)
on new inserts. Old rows keyed by username and new rows keyed by snowflake
will coexist without conflict until a second migration deduplicates them.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

logger = logging.getLogger("nfl_locks.database")

DB_PATH = Path(__file__).parent.parent / "data" / "nfl_locks.db"

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS picks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    season      INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    guild_id    TEXT    NOT NULL,
    user_id     TEXT    NOT NULL,
    user_name   TEXT    NOT NULL,
    team        TEXT    NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (season, week, guild_id, user_id, team)
);

CREATE INDEX IF NOT EXISTS idx_picks_week
    ON picks (season, week, guild_id);

CREATE INDEX IF NOT EXISTS idx_picks_user
    ON picks (season, guild_id, user_id);

CREATE TABLE IF NOT EXISTS week_results (
    season          INTEGER NOT NULL,
    week            INTEGER NOT NULL,
    winning_teams   TEXT    NOT NULL,  -- JSON array of team abbreviations
    PRIMARY KEY (season, week)
);

CREATE TABLE IF NOT EXISTS results_posted (
    season      INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    guild_id    TEXT    NOT NULL,
    posted_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (season, week, guild_id)
);

CREATE TABLE IF NOT EXISTS tracked_messages (
    message_id  TEXT    PRIMARY KEY,
    guild_id    TEXT    NOT NULL,
    channel_id  TEXT    NOT NULL,
    season      INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    team_a      TEXT,
    team_b      TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_messages_week
    ON tracked_messages (season, week, guild_id);

CREATE TABLE IF NOT EXISTS bot_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guild_config (
    guild_id       TEXT PRIMARY KEY,
    channel_id     TEXT NOT NULL,
    guild_name     TEXT,
    scoring_scheme TEXT NOT NULL DEFAULT 'all_or_nothing'
);

CREATE TABLE IF NOT EXISTS week_status (
    season              INTEGER NOT NULL,
    week                INTEGER NOT NULL,
    winners_fetched     INTEGER NOT NULL DEFAULT 0,
    deadline_passed     INTEGER NOT NULL DEFAULT 0,
    reactions_finalized INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (season, week)
);

CREATE TABLE IF NOT EXISTS week_guild_status (
    season                INTEGER NOT NULL,
    week                  INTEGER NOT NULL,
    guild_id              TEXT    NOT NULL,
    games_posted          INTEGER NOT NULL DEFAULT 0,
    locks_posted          INTEGER NOT NULL DEFAULT 0,
    reconciliation_failed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (season, week, guild_id)
);

-- -- Survivor game mode --------------------------------------------------------

CREATE TABLE IF NOT EXISTS survivor_config (
    guild_id    TEXT    PRIMARY KEY,
    channel_id  TEXT    NOT NULL,
    start_week  INTEGER NOT NULL,
    season      INTEGER NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS survivor_picks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    season      INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    guild_id    TEXT    NOT NULL,
    user_id     TEXT    NOT NULL,
    user_name   TEXT    NOT NULL,
    team        TEXT    NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (season, week, guild_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_survivor_picks_week
    ON survivor_picks (season, week, guild_id);

CREATE INDEX IF NOT EXISTS idx_survivor_picks_user
    ON survivor_picks (season, guild_id, user_id);

CREATE TABLE IF NOT EXISTS survivor_status (
    season          INTEGER NOT NULL,
    guild_id        TEXT    NOT NULL,
    user_id         TEXT    NOT NULL,
    user_name       TEXT    NOT NULL,
    eliminated      INTEGER NOT NULL DEFAULT 0,
    eliminated_week INTEGER,
    correct_streak  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (season, guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS survivor_messages (
    message_id  TEXT    PRIMARY KEY,
    guild_id    TEXT    NOT NULL,
    channel_id  TEXT    NOT NULL,
    season      INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    team_a      TEXT,
    team_b      TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_survivor_messages_week
    ON survivor_messages (season, week, guild_id);

CREATE TABLE IF NOT EXISTS survivor_results_posted (
    season      INTEGER NOT NULL,
    week        INTEGER NOT NULL,
    guild_id    TEXT    NOT NULL,
    posted_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (season, week, guild_id)
);
"""


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------

class NFLLocksDB:
    """Async SQLite wrapper for NFL Locks pick data."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None

    # -- Lifecycle ----------------------------------------------------------

    async def connect(self):
        """Open the connection and initialise schema. Call once on cog load."""
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        # Enable WAL mode: readers don't block writers and vice versa.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        # Add columns that were introduced after initial schema (one-time migrations)
        for _col_sql in (
            "ALTER TABLE guild_config ADD COLUMN guild_name TEXT",
            "ALTER TABLE guild_config ADD COLUMN scoring_scheme TEXT NOT NULL DEFAULT 'all_or_nothing'",
            "ALTER TABLE week_guild_status ADD COLUMN reconciliation_failed INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                await self._conn.execute(_col_sql)
                await self._conn.commit()
            except Exception:
                pass  # column already exists
        await self._migrate_from_json()
        logger.info(f"NFL Locks DB connected: {self.db_path}")

    async def close(self):
        """Close the connection. Call on cog unload."""
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("NFL Locks DB connection closed")

    # -- Picks --------------------------------------------------------------

    async def add_pick(
        self,
        season: int,
        week: int,
        guild_id: int | str,
        user_id: int | str,
        user_name: str,
        team: str,
    ) -> bool:
        """
        Record a pick. Silently ignores duplicates (INSERT OR IGNORE).
        Returns True if the row was inserted, False if it already existed.
        """
        async with self._conn.execute(
            """INSERT OR IGNORE INTO picks (season, week, guild_id, user_id, user_name, team)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (season, week, str(guild_id), str(user_id), user_name, team),
        ) as cur:
            inserted = cur.rowcount > 0
        await self._conn.commit()
        return inserted

    async def remove_pick(
        self,
        season: int,
        week: int,
        guild_id: int | str,
        user_id: int | str,
        team: str,
    ) -> bool:
        """
        Delete a specific pick.
        Returns True if a row was deleted.
        """
        async with self._conn.execute(
            """DELETE FROM picks
               WHERE season=? AND week=? AND guild_id=? AND user_id=? AND team=?""",
            (season, week, str(guild_id), str(user_id), team),
        ) as cur:
            deleted = cur.rowcount > 0
        await self._conn.commit()
        return deleted

    async def clear_picks_for_week(self, season: int, week: int, guild_id: int | str):
        """
        Delete ALL picks for a week/guild. Used by reaction_catchup before
        a full reaction rebuild so the slate is clean before re-inserting.
        """
        await self._conn.execute(
            "DELETE FROM picks WHERE season=? AND week=? AND guild_id=?",
            (season, week, str(guild_id)),
        )
        await self._conn.commit()

    async def get_picks_for_week(
        self, season: int, week: int, guild_id: int | str
    ) -> dict[str, list[str]]:
        """
        Return all picks for a week as {team: [user_name, ...]}.
        Mirrors the structure currently read from JSON week files.
        """
        async with self._conn.execute(
            """SELECT team, user_name FROM picks
               WHERE season=? AND week=? AND guild_id=?""",
            (season, week, str(guild_id)),
        ) as cur:
            rows = await cur.fetchall()

        result: dict[str, list[str]] = {}
        for row in rows:
            result.setdefault(row["team"], []).append(row["user_name"])
        return result

    async def get_picks_by_user_id(
        self, season: int, week: int, guild_id: int | str
    ) -> dict[str, set[str]]:
        """
        Return all picks for a week as {user_id: {team, ...}}.

        Used by the diff-based reconciliation to compare DB state against
        live Discord reaction state without a full wipe-and-rebuild.
        """
        async with self._conn.execute(
            """SELECT user_id, team FROM picks
               WHERE season=? AND week=? AND guild_id=?""",
            (season, week, str(guild_id)),
        ) as cur:
            rows = await cur.fetchall()

        result: dict[str, set[str]] = {}
        for row in rows:
            result.setdefault(row["user_id"], set()).add(row["team"])
        return result

    async def get_user_picks_for_week(
        self,
        season: int,
        week: int,
        guild_id: int | str,
        user_id: int | str,
    ) -> list[str]:
        """Return the teams a user picked for a single week."""
        async with self._conn.execute(
            """SELECT team FROM picks
               WHERE season=? AND week=? AND guild_id=? AND user_id=?""",
            (season, week, str(guild_id), str(user_id)),
        ) as cur:
            rows = await cur.fetchall()
        return [r["team"] for r in rows]

    async def user_has_pick(
        self,
        season: int,
        week: int,
        guild_id: int | str,
        user_id: int | str,
        team: str,
    ) -> bool:
        async with self._conn.execute(
            """SELECT 1 FROM picks
               WHERE season=? AND week=? AND guild_id=? AND user_id=? AND team=?""",
            (season, week, str(guild_id), str(user_id), team),
        ) as cur:
            return await cur.fetchone() is not None

    # -- Admin pick fix -----------------------------------------------------

    async def admin_add_pick(
        self,
        season: int,
        week: int,
        guild_id: int | str,
        user_name: str,
        team: str,
    ) -> tuple[bool, str]:
        """
        Admin override: add a pick by username.
        user_id is set to the username (legacy style) so it coexists with
        normal picks without colliding on the UNIQUE constraint.
        Returns (success, message).
        """
        async with self._conn.execute(
            """INSERT OR IGNORE INTO picks (season, week, guild_id, user_id, user_name, team)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (season, week, str(guild_id), user_name, user_name, team),
        ) as cur:
            inserted = cur.rowcount > 0
        await self._conn.commit()

        if inserted:
            return True, f"Added pick: **{user_name}** → `{team}` (Week {week})"
        return False, f"**{user_name}** already has `{team}` picked for Week {week}."

    async def admin_remove_pick(
        self,
        season: int,
        week: int,
        guild_id: int | str,
        user_name: str,
        team: str,
    ) -> tuple[bool, str]:
        """
        Admin override: remove a pick by username.
        Matches on user_name so it works regardless of how user_id was stored.
        Returns (success, message).
        """
        async with self._conn.execute(
            """DELETE FROM picks
               WHERE season=? AND week=? AND guild_id=? AND user_name=? AND team=?""",
            (season, week, str(guild_id), user_name, team),
        ) as cur:
            deleted = cur.rowcount > 0
        await self._conn.commit()

        if deleted:
            return True, f"Removed pick: **{user_name}** → `{team}` (Week {week})"
        return False, f"No pick found for **{user_name}** on `{team}` in Week {week}."

    async def get_user_picks_by_name(
        self,
        season: int,
        week: int,
        guild_id: int | str,
        user_name: str,
    ) -> list[str]:
        """All teams a user picked for a week, looked up by display name."""
        async with self._conn.execute(
            """SELECT team FROM picks
               WHERE season=? AND week=? AND guild_id=? AND user_name=?""",
            (season, week, str(guild_id), user_name),
        ) as cur:
            rows = await cur.fetchall()
        return [r["team"] for r in rows]

    # -- Points calculation -------------------------------------------------

    async def calculate_user_points(
        self,
        season: int,
        guild_id: int | str,
        user_id: int | str,
        scheme: str = "all_or_nothing",
    ) -> tuple[dict[int, int], int]:
        """
        Season points for a user under the given scoring scheme.

        Delegates per-week scoring to scoring.compute_user_week_score so the
        logic stays in one place.

        Returns ({week: points}, total_points).

        Under all_or_nothing, only weeks with > 0 points appear in the dict.
        Under additive, all weeks where the user made picks appear (including
        zero and negative weeks) so the breakdown shows full participation.
        """
        from NFL_Locks.utils.scoring import compute_user_week_score, SCHEME_ALL_OR_NOTHING

        async with self._conn.execute(
            "SELECT week, winning_teams FROM week_results WHERE season=?",
            (season,),
        ) as cur:
            result_rows = await cur.fetchall()

        weekly_points: dict[int, int] = {}

        for row in result_rows:
            week = row["week"]
            winners: set[str] = set(json.loads(row["winning_teams"]))

            user_teams = set(
                await self.get_user_picks_for_week(season, week, guild_id, user_id)
            )
            if not user_teams:
                continue

            pts = compute_user_week_score(user_teams, winners, scheme)

            # All-or-nothing: skip weeks where the user scored 0 (lost or no picks).
            # Additive: include all participated weeks so the breakdown is complete.
            if scheme == SCHEME_ALL_OR_NOTHING and pts == 0:
                continue

            weekly_points[week] = pts

        total = sum(weekly_points.values())
        return weekly_points, total

    # -- Winners / Results --------------------------------------------------

    async def set_winners(self, season: int, week: int, winning_teams: list[str]):
        """Store (or replace) the winning teams for a week."""
        await self._conn.execute(
            """INSERT OR REPLACE INTO week_results (season, week, winning_teams)
               VALUES (?, ?, ?)""",
            (season, week, json.dumps(winning_teams)),
        )
        await self._conn.commit()

    async def get_winners(self, season: int, week: int) -> list[str] | None:
        """Winning teams for a week, or None if not yet stored."""
        async with self._conn.execute(
            "SELECT winning_teams FROM week_results WHERE season=? AND week=?",
            (season, week),
        ) as cur:
            row = await cur.fetchone()
        return json.loads(row["winning_teams"]) if row else None

    async def has_winners(self, season: int, week: int) -> bool:
        async with self._conn.execute(
            "SELECT 1 FROM week_results WHERE season=? AND week=?",
            (season, week),
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_results_posted(
        self, season: int, week: int, guild_id: int | str
    ):
        await self._conn.execute(
            """INSERT OR IGNORE INTO results_posted (season, week, guild_id)
               VALUES (?, ?, ?)""",
            (season, week, str(guild_id)),
        )
        await self._conn.commit()

    async def is_results_posted(
        self, season: int, week: int, guild_id: int | str
    ) -> bool:
        async with self._conn.execute(
            "SELECT 1 FROM results_posted WHERE season=? AND week=? AND guild_id=?",
            (season, week, str(guild_id)),
        ) as cur:
            return await cur.fetchone() is not None

    # -- Tracked messages ---------------------------------------------------

    async def add_tracked_message(
        self,
        message_id: int | str,
        guild_id: int | str,
        channel_id: int | str,
        season: int,
        week: int,
        team_a: str | None = None,
        team_b: str | None = None,
    ):
        """Record a posted game message so reaction lookups can find its week.

        team_a and team_b are the two competing teams for this matchup message.
        Stored so the one-pick-per-matchup rule can identify the opponent team
        at reaction time without re-parsing the schedule.
        """
        await self._conn.execute(
            """INSERT OR IGNORE INTO tracked_messages
               (message_id, guild_id, channel_id, season, week, team_a, team_b)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(message_id), str(guild_id), str(channel_id), season, week, team_a, team_b),
        )
        await self._conn.commit()

    async def get_matchup_teams(
        self, message_id: int | str
    ) -> tuple[str, str] | None:
        """Return (team_a, team_b) for a matchup message, or None if unknown.

        Returns None for messages posted before team columns were added,
        or for non-matchup messages (e.g. the week header post).
        """
        async with self._conn.execute(
            "SELECT team_a, team_b FROM tracked_messages WHERE message_id=?",
            (str(message_id),),
        ) as cur:
            row = await cur.fetchone()
        if not row or not row["team_a"] or not row["team_b"]:
            return None
        return row["team_a"], row["team_b"]

    async def get_user_pick_for_matchup(
        self,
        season: int,
        week: int,
        guild_id: int | str,
        user_id: str,
        team_a: str,
        team_b: str,
    ) -> str | None:
        """Return which team (team_a or team_b) a user has already picked, or None.

        Used to detect conflicting picks before writing a new one, so the
        caller can remove the old pick first.
        """
        async with self._conn.execute(
            """SELECT team FROM picks
               WHERE season=? AND week=? AND guild_id=? AND user_id=?
               AND team IN (?, ?)""",
            (season, week, str(guild_id), user_id, team_a, team_b),
        ) as cur:
            row = await cur.fetchone()
        return row["team"] if row else None

    async def get_matchup_map_for_week(
        self,
        season: int,
        week: int,
        guild_id: int | str,
    ) -> dict[int, tuple[str, str]]:
        """Return {message_id: (team_a, team_b)} for all matchup messages in a week.

        Only includes rows where both team columns are populated (i.e. actual
        game messages, not header/footer posts). Used by the reconciliation sync
        to enforce one-pick-per-matchup without N separate DB queries.
        """
        async with self._conn.execute(
            """SELECT message_id, team_a, team_b FROM tracked_messages
               WHERE season=? AND week=? AND guild_id=?
               AND team_a IS NOT NULL AND team_b IS NOT NULL""",
            (season, week, str(guild_id)),
        ) as cur:
            rows = await cur.fetchall()
        return {int(row["message_id"]): (row["team_a"], row["team_b"]) for row in rows}

    async def get_all_tracked_messages(self, season: int) -> dict[int, int]:
        """
        Return all tracked messages for a season as {message_id: week}.
        Used to populate the in-memory message→week cache on startup.
        """
        async with self._conn.execute(
            "SELECT message_id, week FROM tracked_messages WHERE season=?",
            (season,),
        ) as cur:
            rows = await cur.fetchall()
        return {int(row["message_id"]): row["week"] for row in rows}

    async def clear_tracked_messages_for_week(
        self, season: int, week: int, guild_id: int | str
    ):
        """
        Delete all tracked message rows for a week/guild.
        Called before re-posting games so stale IDs don't accumulate.
        """
        await self._conn.execute(
            "DELETE FROM tracked_messages WHERE season=? AND week=? AND guild_id=?",
            (season, week, str(guild_id)),
        )
        await self._conn.commit()

    async def get_week_for_message(self, message_id: int | str) -> int | None:
        """Look up which week a message belongs to. Returns None if unknown."""
        async with self._conn.execute(
            "SELECT week FROM tracked_messages WHERE message_id=?",
            (str(message_id),),
        ) as cur:
            row = await cur.fetchone()
        return row["week"] if row else None

    async def get_messages_for_week(
        self, season: int, week: int, guild_id: int | str
    ) -> dict[str, list[int]]:
        """
        All tracked messages for a week/guild.
        Returns {channel_id: [message_id, ...]}.
        """
        async with self._conn.execute(
            """SELECT channel_id, message_id FROM tracked_messages
               WHERE season=? AND week=? AND guild_id=?""",
            (season, week, str(guild_id)),
        ) as cur:
            rows = await cur.fetchall()

        result: dict[str, list[int]] = {}
        for row in rows:
            result.setdefault(row["channel_id"], []).append(int(row["message_id"]))
        return result

    async def get_all_picks_for_week(
        self, season: int, week: int
    ) -> dict[str, list[str]]:
        """
        All picks for a week across ALL guilds combined.
        Returns {team: [user_name, ...]}.
        Used for global standings where guild boundaries are intentionally ignored.
        """
        async with self._conn.execute(
            "SELECT team, user_name FROM picks WHERE season=? AND week=?",
            (season, week),
        ) as cur:
            rows = await cur.fetchall()
        result: dict[str, list[str]] = {}
        for row in rows:
            result.setdefault(row["team"], []).append(row["user_name"])
        return result

    async def get_latest_week_with_winners(self, season: int) -> int | None:
        """
        Return the highest week number that has winners recorded, or None.
        Replaces the backwards JSON file scan used to find the most recently
        completed week.
        """
        async with self._conn.execute(
            "SELECT MAX(week) FROM week_results WHERE season=?",
            (season,),
        ) as cur:
            row = await cur.fetchone()
        val = row[0] if row else None
        return int(val) if val is not None else None

    # -- Bot meta --------------------------------------------------------------

    async def get_bot_meta(self, key: str) -> "str | None":
        """Return a value from bot_meta by key, or None if not set."""
        async with self._conn.execute(
            "SELECT value FROM bot_meta WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_bot_meta(self, key: str, value: str) -> None:
        """Insert or replace a key/value pair in bot_meta."""
        await self._conn.execute(
            "INSERT OR REPLACE INTO bot_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        await self._conn.commit()

    # -- Heartbeat -------------------------------------------------------------

    async def set_heartbeat(self):
        """Record the current UTC time as the last known healthy timestamp."""
        ts = datetime.now(timezone.utc).isoformat()
        await self._conn.execute(
            "INSERT OR REPLACE INTO bot_meta (key, value) VALUES ('last_heartbeat', ?)",
            (ts,),
        )
        await self._conn.commit()

    async def get_last_heartbeat(self) -> datetime | None:
        """
        Return the last heartbeat timestamp as a UTC-aware datetime, or None
        if no heartbeat has ever been recorded.
        """
        async with self._conn.execute(
            "SELECT value FROM bot_meta WHERE key='last_heartbeat'",
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return datetime.fromisoformat(row["value"])


    # -- Guild config ----------------------------------------------------------

    async def set_guild_channel(
        self,
        guild_id: int | str,
        channel_id: int | str,
        guild_name: str | None = None,
    ):
        """Register (or update) a guild's posting channel and optional display name."""
        await self._conn.execute(
            """INSERT INTO guild_config (guild_id, channel_id, guild_name)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE
               SET channel_id=excluded.channel_id,
                   guild_name=COALESCE(excluded.guild_name, guild_name)""",
            (str(guild_id), str(channel_id), guild_name),
        )
        await self._conn.commit()

    async def get_guild_channel(self, guild_id: int | str) -> int | None:
        """Return the configured channel ID for a guild, or None."""
        async with self._conn.execute(
            "SELECT channel_id FROM guild_config WHERE guild_id=?",
            (str(guild_id),),
        ) as cur:
            row = await cur.fetchone()
        return int(row["channel_id"]) if row else None

    async def get_all_configured_guilds(self) -> dict[int, int]:
        """Return {guild_id: channel_id} for every configured guild."""
        async with self._conn.execute(
            "SELECT guild_id, channel_id FROM guild_config"
        ) as cur:
            rows = await cur.fetchall()
        return {int(r["guild_id"]): int(r["channel_id"]) for r in rows}

    async def get_all_configured_guilds_with_names(self) -> list[dict]:
        """Return [{guild_id, channel_id, guild_name}] for every configured guild."""
        async with self._conn.execute(
            "SELECT guild_id, channel_id, guild_name FROM guild_config"
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "guild_id": int(r["guild_id"]),
                "channel_id": int(r["channel_id"]),
                "guild_name": r["guild_name"],
            }
            for r in rows
        ]

    async def get_scoring_scheme(self, guild_id: int | str) -> str:
        """
        Return the scoring scheme for a guild.
        Falls back to 'all_or_nothing' if the guild has no config row yet.
        """
        from NFL_Locks.utils.scoring import SCHEME_ALL_OR_NOTHING
        async with self._conn.execute(
            "SELECT scoring_scheme FROM guild_config WHERE guild_id=?",
            (str(guild_id),),
        ) as cur:
            row = await cur.fetchone()
        return row["scoring_scheme"] if row else SCHEME_ALL_OR_NOTHING

    async def set_scoring_scheme(self, guild_id: int | str, scheme: str):
        """Update the scoring scheme for a guild. Guild config row must exist."""
        await self._conn.execute(
            "UPDATE guild_config SET scoring_scheme=? WHERE guild_id=?",
            (scheme, str(guild_id)),
        )
        await self._conn.commit()

    async def get_user_season_pick_count(
        self, season: int, guild_id: int | str, user_id: int | str
    ) -> int:
        """Return the total number of individual picks a user has made this season."""
        async with self._conn.execute(
            "SELECT COUNT(*) FROM picks WHERE season=? AND guild_id=? AND user_id=?",
            (season, str(guild_id), str(user_id)),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    async def refresh_guild_names(self, guild_map: dict[int, str]):
        """Batch-update guild display names from live Discord data.

        guild_map — {guild_id: guild_name} for every guild the bot can see.
        Only updates rows that already exist in guild_config; does not
        create new rows.
        """
        for guild_id, name in guild_map.items():
            await self._conn.execute(
                "UPDATE guild_config SET guild_name=? WHERE guild_id=?",
                (name, str(guild_id)),
            )
        await self._conn.commit()

    # -- Week-level status flags -----------------------------------------------

    async def _touch_week(self, season: int, week: int):
        """Ensure a row exists in week_status so UPDATE doesn't silently no-op."""
        await self._conn.execute(
            "INSERT OR IGNORE INTO week_status (season, week) VALUES (?, ?)",
            (season, week),
        )

    async def mark_winners_fetched(self, season: int, week: int):
        await self._touch_week(season, week)
        await self._conn.execute(
            "UPDATE week_status SET winners_fetched=1 WHERE season=? AND week=?",
            (season, week),
        )
        await self._conn.commit()

    async def needs_winners(self, season: int, week: int) -> bool:
        async with self._conn.execute(
            "SELECT winners_fetched FROM week_status WHERE season=? AND week=?",
            (season, week),
        ) as cur:
            row = await cur.fetchone()
        return (not row) or (not row["winners_fetched"])

    async def mark_deadline_passed(self, season: int, week: int):
        await self._touch_week(season, week)
        await self._conn.execute(
            "UPDATE week_status SET deadline_passed=1 WHERE season=? AND week=?",
            (season, week),
        )
        await self._conn.commit()

    async def has_deadline_passed(self, season: int, week: int) -> bool:
        async with self._conn.execute(
            "SELECT deadline_passed FROM week_status WHERE season=? AND week=?",
            (season, week),
        ) as cur:
            row = await cur.fetchone()
        return bool(row and row["deadline_passed"])

    async def mark_reactions_finalized(self, season: int, week: int):
        await self._touch_week(season, week)
        await self._conn.execute(
            "UPDATE week_status SET reactions_finalized=1 WHERE season=? AND week=?",
            (season, week),
        )
        await self._conn.commit()

    async def are_reactions_finalized(self, season: int, week: int) -> bool:
        async with self._conn.execute(
            "SELECT reactions_finalized FROM week_status WHERE season=? AND week=?",
            (season, week),
        ) as cur:
            row = await cur.fetchone()
        return bool(row and row["reactions_finalized"])

    # -- Per-guild, per-week status flags -------------------------------------

    async def _touch_week_guild(self, season: int, week: int, guild_id: int | str):
        await self._conn.execute(
            """INSERT OR IGNORE INTO week_guild_status (season, week, guild_id)
               VALUES (?, ?, ?)""",
            (season, week, str(guild_id)),
        )

    async def mark_games_posted(self, season: int, week: int, guild_id: int | str):
        await self._touch_week_guild(season, week, guild_id)
        await self._conn.execute(
            """UPDATE week_guild_status SET games_posted=1
               WHERE season=? AND week=? AND guild_id=?""",
            (season, week, str(guild_id)),
        )
        await self._conn.commit()

    async def needs_games_posted(
        self, season: int, week: int, guild_id: int | str
    ) -> bool:
        async with self._conn.execute(
            """SELECT games_posted FROM week_guild_status
               WHERE season=? AND week=? AND guild_id=?""",
            (season, week, str(guild_id)),
        ) as cur:
            row = await cur.fetchone()
        return (not row) or (not row["games_posted"])

    async def mark_locks_posted(self, season: int, week: int, guild_id: int | str):
        await self._touch_week_guild(season, week, guild_id)
        await self._conn.execute(
            """UPDATE week_guild_status SET locks_posted=1
               WHERE season=? AND week=? AND guild_id=?""",
            (season, week, str(guild_id)),
        )
        await self._conn.commit()

    async def needs_locks_posted(
        self, season: int, week: int, guild_id: int | str
    ) -> bool:
        """True if deadline has passed and locks haven't been posted for this guild."""
        if not await self.has_deadline_passed(season, week):
            return False
        async with self._conn.execute(
            """SELECT locks_posted FROM week_guild_status
               WHERE season=? AND week=? AND guild_id=?""",
            (season, week, str(guild_id)),
        ) as cur:
            row = await cur.fetchone()
        return (not row) or (not row["locks_posted"])

    async def mark_reconciliation_failed(
        self, season: int, week: int, guild_id: int | str
    ):
        """Flag a guild/week as having failed reaction reconciliation."""
        await self._touch_week_guild(season, week, guild_id)
        await self._conn.execute(
            """UPDATE week_guild_status SET reconciliation_failed=1
               WHERE season=? AND week=? AND guild_id=?""",
            (season, week, str(guild_id)),
        )
        await self._conn.commit()

    async def is_reconciliation_failed(
        self, season: int, week: int, guild_id: int | str
    ) -> bool:
        """True if reconciliation was flagged as failed for this guild/week."""
        async with self._conn.execute(
            """SELECT reconciliation_failed FROM week_guild_status
               WHERE season=? AND week=? AND guild_id=?""",
            (season, week, str(guild_id)),
        ) as cur:
            row = await cur.fetchone()
        return bool(row and row["reconciliation_failed"])

    async def clear_reconciliation_failed(
        self, season: int, week: int, guild_id: int | str
    ):
        """Reset the reconciliation_failed flag (e.g. after a manual fix)."""
        await self._conn.execute(
            """UPDATE week_guild_status SET reconciliation_failed=0
               WHERE season=? AND week=? AND guild_id=?""",
            (season, week, str(guild_id)),
        )
        await self._conn.commit()

    # -- One-time JSON→DB migration --------------------------------------------

    async def _migrate_from_json(self):
        """
        If bot_status.json exists from the old status_tracker, import its data
        into the new DB tables and rename the file so this only runs once.
        """
        import json as _json
        from pathlib import Path as _Path

        status_file = _Path(__file__).parent.parent / "data" / "bot_status.json"
        if not status_file.exists():
            return

        try:
            status = _json.loads(status_file.read_text())
        except Exception as e:
            logger.warning(f"Could not read bot_status.json for migration: {e}")
            return

        logger.info("Migrating bot_status.json → SQLite status tables...")

        # Guild config
        for guild_id_str, guild_data in status.get("guilds", {}).items():
            ch = guild_data.get("configured_channel")
            if ch:
                await self._conn.execute(
                    "INSERT OR IGNORE INTO guild_config (guild_id, channel_id) VALUES (?, ?)",
                    (guild_id_str, str(ch)),
                )

        # Week status and week_guild_status
        # Use season 2025 as a reasonable default — this migration only runs once
        # on the first boot after upgrading, so the season is always the current one.
        # We use 2025 as a safe fallback; adjust if your season differs.
        try:
            from NFL_Locks.utils.schedule_utils import get_current_season as _gcs
            season = _gcs()
        except Exception:
            season = 2025

        for week_str, week_data in status.get("weeks", {}).items():
            try:
                week = int(week_str)
            except ValueError:
                continue

            wf = 1 if week_data.get("winners_fetched") else 0
            dp = 1 if week_data.get("deadline_passed") else 0
            rf = 1 if week_data.get("reactions_finalized") else 0

            await self._conn.execute(
                """INSERT OR IGNORE INTO week_status
                   (season, week, winners_fetched, deadline_passed, reactions_finalized)
                   VALUES (?, ?, ?, ?, ?)""",
                (season, week, wf, dp, rf),
            )

            completion = week_data.get("completion", {})
            for guild_id_str in completion.get("games_posted", []):
                await self._conn.execute(
                    """INSERT OR IGNORE INTO week_guild_status
                       (season, week, guild_id, games_posted) VALUES (?, ?, ?, 1)
                       ON CONFLICT(season, week, guild_id)
                       DO UPDATE SET games_posted=1""",
                    (season, week, guild_id_str),
                )
            for guild_id_str in completion.get("locks_posted", []):
                await self._conn.execute(
                    """INSERT OR IGNORE INTO week_guild_status
                       (season, week, guild_id, locks_posted) VALUES (?, ?, ?, 1)
                       ON CONFLICT(season, week, guild_id)
                       DO UPDATE SET locks_posted=1""",
                    (season, week, guild_id_str),
                )
            for guild_id_str in completion.get("results_posted", []):
                await self._conn.execute(
                    """INSERT OR IGNORE INTO results_posted (season, week, guild_id)
                       VALUES (?, ?, ?)""",
                    (season, week, guild_id_str),
                )

        await self._conn.commit()

        # Rename so migration doesn't run again
        status_file.rename(status_file.with_suffix(".json.migrated"))
        logger.info("bot_status.json migration complete → renamed to .json.migrated")

    # -- Diagnostics -----------------------------------------------------------

    async def get_pick_count(self, season: int, week: int, guild_id: int | str) -> int:
        """Return the total number of picks recorded for a week/guild."""
        async with self._conn.execute(
            "SELECT COUNT(*) FROM picks WHERE season=? AND week=? AND guild_id=?",
            (season, week, str(guild_id)),
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0

    # -- Season archival -------------------------------------------------------

    async def archive_and_purge_season(
        self, season: int, archive_dir: "Path"
    ) -> "dict[str, int]":
        """
        Export all rows for ``season`` to CSV files in ``archive_dir``, then
        delete them from the live DB.

        Only season-tagged tables are touched.  ``guild_config`` and
        ``bot_meta`` are intentionally left intact — they contain
        configuration state that spans seasons, not pick data.

        Returns ``{table_name: rows_deleted}`` for logging/notification.
        """
        import json

        archive_dir.mkdir(parents=True, exist_ok=True)

        season_tables = [
            "picks",
            "week_results",
            "results_posted",
            "tracked_messages",
            "week_status",
            "week_guild_status",
        ]

        counts: dict[str, int] = {}
        for table in season_tables:
            # Export rows to JSON before deleting.
            # dict(row) preserves column names and native types (int, str, etc.)
            # so the archive is self-describing and re-importable without coercion.
            async with self._conn.execute(
                f"SELECT * FROM {table} WHERE season = ?", (season,)
            ) as cur:
                rows = [dict(row) async for row in cur]

            json_path = archive_dir / f"{table}.json"
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(rows, f, indent=2, default=str)

            # Delete from live DB
            async with self._conn.execute(
                f"DELETE FROM {table} WHERE season = ?", (season,)
            ) as cur:
                counts[table] = cur.rowcount

        await self._conn.commit()
        logger.info(
            f"Season {season} archived and purged: "
            + ", ".join(f"{t}={n}" for t, n in counts.items())
        )
        return counts


# ---------------------------------------------------------------------------
# Survivor game mode
# ---------------------------------------------------------------------------

    # -- Survivor config -------------------------------------------------------

    async def set_survivor_config(
        self,
        guild_id: int | str,
        channel_id: int | str,
        start_week: int,
        season: int,
    ) -> None:
        """Create or replace the survivor config for a guild."""
        await self._conn.execute(
            """INSERT OR REPLACE INTO survivor_config
               (guild_id, channel_id, start_week, season, active)
               VALUES (?, ?, ?, ?, 1)""",
            (str(guild_id), str(channel_id), start_week, season),
        )
        await self._conn.commit()

    async def get_survivor_config(self, guild_id: int | str) -> "dict | None":
        """Return the survivor config for a guild, or None if not configured."""
        async with self._conn.execute(
            "SELECT * FROM survivor_config WHERE guild_id=?",
            (str(guild_id),),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "guild_id": row["guild_id"],
            "channel_id": int(row["channel_id"]),
            "start_week": row["start_week"],
            "season": row["season"],
            "active": bool(row["active"]),
        }

    async def get_all_survivor_configs(self) -> "list[dict]":
        """Return all guild survivor configs."""
        async with self._conn.execute(
            "SELECT * FROM survivor_config WHERE active=1"
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "guild_id": r["guild_id"],
                "channel_id": int(r["channel_id"]),
                "start_week": r["start_week"],
                "season": r["season"],
            }
            for r in rows
        ]

    async def deactivate_survivor(self, guild_id: int | str) -> None:
        """Mark a guild's survivor game as inactive (season ended)."""
        await self._conn.execute(
            "UPDATE survivor_config SET active=0 WHERE guild_id=?",
            (str(guild_id),),
        )
        await self._conn.commit()

    # -- Survivor messages -----------------------------------------------------

    async def add_survivor_message(
        self,
        message_id: int | str,
        guild_id: int | str,
        channel_id: int | str,
        season: int,
        week: int,
        team_a: "str | None" = None,
        team_b: "str | None" = None,
    ) -> None:
        await self._conn.execute(
            """INSERT OR IGNORE INTO survivor_messages
               (message_id, guild_id, channel_id, season, week, team_a, team_b)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(message_id), str(guild_id), str(channel_id), season, week, team_a, team_b),
        )
        await self._conn.commit()

    async def get_survivor_matchup_teams(
        self, message_id: int | str
    ) -> "tuple[str, str] | None":
        """Return (team_a, team_b) for a survivor message, or None."""
        async with self._conn.execute(
            "SELECT team_a, team_b FROM survivor_messages WHERE message_id=?",
            (str(message_id),),
        ) as cur:
            row = await cur.fetchone()
        if not row or not row["team_a"] or not row["team_b"]:
            return None
        return row["team_a"], row["team_b"]

    async def get_week_for_survivor_message(self, message_id: int | str) -> "int | None":
        async with self._conn.execute(
            "SELECT week FROM survivor_messages WHERE message_id=?",
            (str(message_id),),
        ) as cur:
            row = await cur.fetchone()
        return row["week"] if row else None

    async def get_survivor_messages_for_week(
        self, season: int, week: int, guild_id: int | str
    ) -> "dict[str, list[int]]":
        """Return {channel_id: [message_id, ...]} for a week's survivor messages."""
        async with self._conn.execute(
            """SELECT channel_id, message_id FROM survivor_messages
               WHERE season=? AND week=? AND guild_id=?""",
            (season, week, str(guild_id)),
        ) as cur:
            rows = await cur.fetchall()
        result: dict[str, list[int]] = {}
        for row in rows:
            result.setdefault(row["channel_id"], []).append(int(row["message_id"]))
        return result

    async def clear_survivor_messages_for_week(
        self, season: int, week: int, guild_id: int | str
    ) -> None:
        await self._conn.execute(
            "DELETE FROM survivor_messages WHERE season=? AND week=? AND guild_id=?",
            (season, week, str(guild_id)),
        )
        await self._conn.commit()

    async def get_all_survivor_messages(self, season: int) -> "dict[int, int]":
        """Return {message_id: week} for all survivor messages this season."""
        async with self._conn.execute(
            "SELECT message_id, week FROM survivor_messages WHERE season=?",
            (season,),
        ) as cur:
            rows = await cur.fetchall()
        return {int(r["message_id"]): r["week"] for r in rows}

    # -- Survivor picks --------------------------------------------------------

    async def set_survivor_pick(
        self,
        season: int,
        week: int,
        guild_id: int | str,
        user_id: int | str,
        user_name: str,
        team: str,
    ) -> None:
        """Insert or replace a survivor pick (enforces one pick per week per user)."""
        await self._conn.execute(
            """INSERT OR REPLACE INTO survivor_picks
               (season, week, guild_id, user_id, user_name, team)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (season, week, str(guild_id), str(user_id), user_name, team),
        )
        await self._conn.commit()

    async def remove_survivor_pick(
        self, season: int, week: int, guild_id: int | str, user_id: int | str
    ) -> "str | None":
        """Delete a survivor pick for a user. Returns the team that was removed, or None."""
        async with self._conn.execute(
            """SELECT team FROM survivor_picks
               WHERE season=? AND week=? AND guild_id=? AND user_id=?""",
            (season, week, str(guild_id), str(user_id)),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        await self._conn.execute(
            """DELETE FROM survivor_picks
               WHERE season=? AND week=? AND guild_id=? AND user_id=?""",
            (season, week, str(guild_id), str(user_id)),
        )
        await self._conn.commit()
        return row["team"]

    async def get_survivor_pick(
        self, season: int, week: int, guild_id: int | str, user_id: int | str
    ) -> "str | None":
        """Return the team a user picked for a survivor week, or None."""
        async with self._conn.execute(
            """SELECT team FROM survivor_picks
               WHERE season=? AND week=? AND guild_id=? AND user_id=?""",
            (season, week, str(guild_id), str(user_id)),
        ) as cur:
            row = await cur.fetchone()
        return row["team"] if row else None

    async def get_teams_used_in_survivor(
        self, season: int, guild_id: int | str, user_id: int | str
    ) -> "set[str]":
        """Return all teams a user has already picked in survivor this season."""
        async with self._conn.execute(
            """SELECT team FROM survivor_picks
               WHERE season=? AND guild_id=? AND user_id=?""",
            (season, str(guild_id), str(user_id)),
        ) as cur:
            rows = await cur.fetchall()
        return {r["team"] for r in rows}

    async def get_survivor_picks_for_week(
        self, season: int, week: int, guild_id: int | str
    ) -> "list[dict]":
        """Return all survivor picks for a week as [{user_id, user_name, team}]."""
        async with self._conn.execute(
            """SELECT user_id, user_name, team FROM survivor_picks
               WHERE season=? AND week=? AND guild_id=?""",
            (season, week, str(guild_id)),
        ) as cur:
            rows = await cur.fetchall()
        return [{"user_id": r["user_id"], "user_name": r["user_name"], "team": r["team"]}
                for r in rows]

    # -- Survivor player status ------------------------------------------------

    async def enroll_survivor_player(
        self,
        season: int,
        guild_id: int | str,
        user_id: int | str,
        user_name: str,
    ) -> bool:
        """
        Add a player to survivor. Silently ignores if already enrolled.
        Returns True if newly enrolled, False if already enrolled.
        """
        async with self._conn.execute(
            """INSERT OR IGNORE INTO survivor_status
               (season, guild_id, user_id, user_name)
               VALUES (?, ?, ?, ?)""",
            (season, str(guild_id), str(user_id), user_name),
        ) as cur:
            enrolled = cur.rowcount > 0
        await self._conn.commit()
        return enrolled

    async def is_survivor_enrolled(
        self, season: int, guild_id: int | str, user_id: int | str
    ) -> bool:
        async with self._conn.execute(
            """SELECT 1 FROM survivor_status
               WHERE season=? AND guild_id=? AND user_id=?""",
            (season, str(guild_id), str(user_id)),
        ) as cur:
            return await cur.fetchone() is not None

    async def get_survivor_player(
        self, season: int, guild_id: int | str, user_id: int | str
    ) -> "dict | None":
        """Return a player's full survivor status, or None if not enrolled."""
        async with self._conn.execute(
            """SELECT * FROM survivor_status
               WHERE season=? AND guild_id=? AND user_id=?""",
            (season, str(guild_id), str(user_id)),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "user_id": row["user_id"],
            "user_name": row["user_name"],
            "eliminated": bool(row["eliminated"]),
            "eliminated_week": row["eliminated_week"],
            "correct_streak": row["correct_streak"],
        }

    async def get_alive_survivor_players(
        self, season: int, guild_id: int | str
    ) -> "list[dict]":
        """Return all non-eliminated survivor players for a guild."""
        async with self._conn.execute(
            """SELECT * FROM survivor_status
               WHERE season=? AND guild_id=? AND eliminated=0
               ORDER BY correct_streak DESC""",
            (season, str(guild_id)),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "user_id": r["user_id"],
                "user_name": r["user_name"],
                "eliminated": False,
                "eliminated_week": None,
                "correct_streak": r["correct_streak"],
            }
            for r in rows
        ]

    async def get_all_survivor_players(
        self, season: int, guild_id: int | str
    ) -> "list[dict]":
        """Return all survivor players (alive and eliminated) for a guild."""
        async with self._conn.execute(
            """SELECT * FROM survivor_status
               WHERE season=? AND guild_id=?
               ORDER BY eliminated ASC, correct_streak DESC, eliminated_week DESC""",
            (season, str(guild_id)),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "user_id": r["user_id"],
                "user_name": r["user_name"],
                "eliminated": bool(r["eliminated"]),
                "eliminated_week": r["eliminated_week"],
                "correct_streak": r["correct_streak"],
            }
            for r in rows
        ]

    async def eliminate_survivor_player(
        self, season: int, guild_id: int | str, user_id: int | str, week: int
    ) -> None:
        await self._conn.execute(
            """UPDATE survivor_status SET eliminated=1, eliminated_week=?
               WHERE season=? AND guild_id=? AND user_id=?""",
            (week, season, str(guild_id), str(user_id)),
        )
        await self._conn.commit()

    async def increment_survivor_streak(
        self, season: int, guild_id: int | str, user_id: int | str
    ) -> int:
        """Increment a player's correct streak by 1. Returns the new streak value."""
        await self._conn.execute(
            """UPDATE survivor_status SET correct_streak = correct_streak + 1
               WHERE season=? AND guild_id=? AND user_id=?""",
            (season, str(guild_id), str(user_id)),
        )
        await self._conn.commit()
        async with self._conn.execute(
            """SELECT correct_streak FROM survivor_status
               WHERE season=? AND guild_id=? AND user_id=?""",
            (season, str(guild_id), str(user_id)),
        ) as cur:
            row = await cur.fetchone()
        return row["correct_streak"] if row else 0

    # -- Survivor results posted -----------------------------------------------

    async def mark_survivor_results_posted(
        self, season: int, week: int, guild_id: int | str
    ) -> None:
        await self._conn.execute(
            """INSERT OR IGNORE INTO survivor_results_posted (season, week, guild_id)
               VALUES (?, ?, ?)""",
            (season, week, str(guild_id)),
        )
        await self._conn.commit()

    async def is_survivor_results_posted(
        self, season: int, week: int, guild_id: int | str
    ) -> bool:
        async with self._conn.execute(
            """SELECT 1 FROM survivor_results_posted
               WHERE season=? AND week=? AND guild_id=?""",
            (season, week, str(guild_id)),
        ) as cur:
            return await cur.fetchone() is not None

# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_db_instance: "NFLLocksDB | None" = None


def get_db() -> "NFLLocksDB":
    """
    Return the process-wide NFLLocksDB singleton.

    ``await get_db().connect()`` must be called once before any queries —
    bot.py does this in ``main()`` before loading any extensions, so all cogs
    receive a live connection from the moment they load.

    Calling this before connect() is fine (main() does exactly that).
    Attempting a query before connect() will raise AttributeError on _conn.
    """
    global _db_instance
    if _db_instance is None:
        _db_instance = NFLLocksDB()
    return _db_instance
