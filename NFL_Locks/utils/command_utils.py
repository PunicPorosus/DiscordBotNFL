"""
Shared command helpers used across multiple cogs.
"""

from NFL_Locks.utils.database import get_db
from NFL_Locks.utils.schedule_utils import get_current_season


async def off_season_reply(ctx) -> bool:
    """
    Send an off-season notice and return True if the bot is currently in
    off-season mode (data archived, new season not yet loaded).

    Callers should ``return`` immediately when this returns True::

        if await off_season_reply(ctx):
            return
    """
    if await get_db().get_bot_meta("off_season") == "true":
        season = get_current_season()
        await ctx.send(
            f"The {season} season data has been archived. "
            f"Check `data/archives/season_{season}/` for historical records."
        )
        return True
    return False
