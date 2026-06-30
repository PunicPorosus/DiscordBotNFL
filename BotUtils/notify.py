"""
Bot-wide notification utilities.

All admin alerts and operational messages should go through notify_admin().
Primary destination is the #bot-log Discord channel; falls back to a DM
if the channel is unavailable.

Usage:
    from BotUtils.notify import notify_admin
    await notify_admin(bot, "NFL sync failed this week.")
"""

import logging
from BotUtils.constants import OWNER_ID, BOT_LOG_CHANNEL_ID

logger = logging.getLogger("bot.notify")


async def notify_admin(bot, message: str) -> None:
    """
    Send an operational message to the #bot-log channel.
    Falls back to a DM to the owner if the channel cannot be reached.
    """
    await _send_to_log_channel(bot, message)


async def _send_to_log_channel(bot, message: str) -> None:
    """Send to the designated log channel, falling back to DM on failure."""
    try:
        channel = bot.get_channel(BOT_LOG_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(BOT_LOG_CHANNEL_ID)
        await channel.send(message)
    except Exception as e:
        logger.error(f"Could not send to log channel ({BOT_LOG_CHANNEL_ID}): {e}")
        await _dm_admin(bot, message)


async def _dm_admin(bot, message: str) -> None:
    """Last-resort fallback: DM the bot owner directly."""
    try:
        owner = await bot.fetch_user(OWNER_ID)
        await owner.send(message)
    except Exception as e:
        # Nothing left to try — write to log so at least the file captures it
        logger.error(f"Could not DM admin ({OWNER_ID}): {e}. Original message: {message}")
