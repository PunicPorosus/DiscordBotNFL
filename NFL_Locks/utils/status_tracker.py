"""
Status Tracker - Centralized status management for bot operations

Thin async wrappers over NFLLocksDB.  All state lives in SQLite;
the old bot_status.json is automatically migrated on first boot
via database.connect() → _migrate_from_json().

API surface is intentionally kept identical to the old sync version
(same function names, same signatures) with two changes:
  1. Every function is now async.
  2. season is derived from get_current_season() internally so callers
     that only know the week number don't need to change.

Functions load_status / save_status are gone.  Any cog that still
imports them must be updated to remove those imports.
"""

import logging
from typing import Dict, List, Optional

from NFL_Locks.utils.database import get_db
from NFL_Locks.utils.schedule_utils import get_current_season

logger = logging.getLogger("nfl_locks.status_tracker")


def _season() -> int:
    """Return the current NFL season year."""
    return get_current_season()


# ═══════════════════════════════════════════════════════════════════════════
# WINNERS TRACKING
# ═══════════════════════════════════════════════════════════════════════════

async def mark_winners_fetched(week_number: int):
    """Mark that winning teams have been fetched for a week."""
    await get_db().mark_winners_fetched(_season(), week_number)
    logger.info(f"[STATUS] Winners fetched — Week {week_number}")


async def needs_winners(week_number: int) -> bool:
    """Return True if winning teams have not yet been fetched for this week."""
    return await get_db().needs_winners(_season(), week_number)


# ═══════════════════════════════════════════════════════════════════════════
# GAMES POSTING TRACKING
# ═══════════════════════════════════════════════════════════════════════════

async def mark_games_posted(week_number: int, guild_id: int):
    """Mark that the games list has been posted for a week in a guild."""
    await get_db().mark_games_posted(_season(), week_number, guild_id)
    logger.info(f"[STATUS] Games posted — Week {week_number}, Guild {guild_id}")


async def needs_games_posted(week_number: int, guild_id: int) -> bool:
    """Return True if the games list still needs to be posted for this guild/week."""
    return await get_db().needs_games_posted(_season(), week_number, guild_id)


async def get_guilds_needing_games(week_number: int) -> List[int]:
    """Return guild IDs that still need the games list posted this week."""
    db = get_db()
    season = _season()
    guilds = await db.get_all_configured_guilds()   # {guild_id: channel_id}
    result = []
    for guild_id in guilds:
        if await db.needs_games_posted(season, week_number, guild_id):
            result.append(guild_id)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# RESULTS POSTING TRACKING
# ═══════════════════════════════════════════════════════════════════════════

async def mark_results_posted(week_number: int, guild_id: int = None):
    """Mark that results have been posted.

    If guild_id is None, marks for every configured guild.
    """
    db = get_db()
    season = _season()
    if guild_id is None:
        guilds = await db.get_all_configured_guilds()
        for gid in guilds:
            await db.mark_results_posted(season, week_number, gid)
        logger.info(f"[STATUS] Results posted — Week {week_number} (all guilds)")
    else:
        await db.mark_results_posted(season, week_number, guild_id)
        logger.info(f"[STATUS] Results posted — Week {week_number}, Guild {guild_id}")


async def needs_results_posted(week_number: int, guild_id: int = None) -> bool:
    """Return True if results still need to be posted.

    If guild_id is None, returns True if ANY configured guild is missing results.
    """
    db = get_db()
    season = _season()
    if guild_id is None:
        guilds = await db.get_all_configured_guilds()
        for gid in guilds:
            if not await db.is_results_posted(season, week_number, gid):
                return True
        return False
    return not await db.is_results_posted(season, week_number, guild_id)


async def get_guilds_needing_results(week_number: int) -> List[int]:
    """Return guild IDs that still need results posted this week."""
    db = get_db()
    season = _season()
    guilds = await db.get_all_configured_guilds()
    result = []
    for guild_id in guilds:
        if not await db.is_results_posted(season, week_number, guild_id):
            result.append(guild_id)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# LOCKS POSTING TRACKING
# ═══════════════════════════════════════════════════════════════════════════

async def get_guilds_needing_locks(week_number: int) -> List[int]:
    """Return guild IDs that need lock summaries posted this week."""
    db = get_db()
    season = _season()
    # Only relevant after deadline — needs_locks_posted handles that guard
    guilds = await db.get_all_configured_guilds()
    result = []
    for guild_id in guilds:
        if await db.needs_locks_posted(season, week_number, guild_id):
            result.append(guild_id)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# DEADLINE & REACTIONS TRACKING
# ═══════════════════════════════════════════════════════════════════════════

async def mark_deadline_passed(week_number: int):
    """Mark that the submission deadline has passed for a week."""
    await get_db().mark_deadline_passed(_season(), week_number)
    logger.info(f"[STATUS] Deadline passed — Week {week_number}")


async def has_deadline_passed(week_number: int) -> bool:
    """Return True if the submission deadline has passed for this week."""
    return await get_db().has_deadline_passed(_season(), week_number)


async def mark_reactions_finalized(week_number: int):
    """Mark that reactions have been finalised for a week."""
    await get_db().mark_reactions_finalized(_season(), week_number)
    logger.info(f"[STATUS] Reactions finalised — Week {week_number}")


async def are_reactions_finalized(week_number: int) -> bool:
    """Return True if reactions are finalised for this week."""
    return await get_db().are_reactions_finalized(_season(), week_number)


# ═══════════════════════════════════════════════════════════════════════════
# GUILD CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════

async def set_guild_channel(guild_id: int, channel_id: int, guild_name: str | None = None):
    """Register (or update) the posting channel for a guild."""
    await get_db().set_guild_channel(guild_id, channel_id, guild_name)
    logger.info(f"[STATUS] Guild {guild_id} ({guild_name}) channel set to {channel_id}")


async def get_guild_channel(guild_id: int) -> Optional[int]:
    """Return the configured channel ID for a guild, or None."""
    return await get_db().get_guild_channel(guild_id)


async def get_active_guilds() -> List[int]:
    """Return guild IDs for all configured guilds."""
    guilds = await get_db().get_all_configured_guilds()
    return list(guilds.keys())


async def add_active_week_to_guild(guild_id: int, week_number: int):
    """No-op: week participation is recorded implicitly via mark_* calls."""
    pass


# ═══════════════════════════════════════════════════════════════════════════
# DIAGNOSTICS
# ═══════════════════════════════════════════════════════════════════════════

async def get_pending_work(week_number: int = None) -> dict:
    """
    Return a summary of pending work across all guilds.

    If week_number is provided, only that week is checked.
    Returns {week: {guilds_needing_games, guilds_needing_results,
                    guilds_needing_locks, needs_winners}}.
    """
    db = get_db()
    season = _season()
    guilds = await db.get_all_configured_guilds()

    # Determine weeks to check
    if week_number is not None:
        weeks_to_check = [week_number]
    else:
        # Check the latest week that has any DB row
        async with db._conn.execute(
            "SELECT DISTINCT week FROM week_status WHERE season=? ORDER BY week DESC LIMIT 5",
            (season,),
        ) as cur:
            rows = await cur.fetchall()
        weeks_to_check = [r["week"] for r in rows] if rows else []

    pending: dict = {}
    for wk in weeks_to_check:
        entry: dict = {
            "needs_winners": await db.needs_winners(season, wk),
            "deadline_passed": await db.has_deadline_passed(season, wk),
            "reactions_finalized": await db.are_reactions_finalized(season, wk),
            "guilds_needing_games": [],
            "guilds_needing_results": [],
            "guilds_needing_locks": [],
        }
        for gid in guilds:
            if await db.needs_games_posted(season, wk, gid):
                entry["guilds_needing_games"].append(gid)
            if not await db.is_results_posted(season, wk, gid):
                entry["guilds_needing_results"].append(gid)
            if await db.needs_locks_posted(season, wk, gid):
                entry["guilds_needing_locks"].append(gid)
        pending[wk] = entry

    return pending


async def rebuild_status_from_weeks():
    """
    Deprecated: status is now authoritative in SQLite.

    This function is a no-op kept for import compatibility while
    status_migration.py is being updated.
    """
    logger.warning(
        "rebuild_status_from_weeks() called but is a no-op — "
        "status lives in SQLite and does not need rebuilding."
    )
