"""
Lock a channel to everyone but the bot while it posts a batch of matchups.

Players typing mid-post split the matchup list up. This denies Send Messages to
@everyone for the length of the post and puts the channel back exactly how it
was afterwards. Add Reactions is left alone, so picks on matchups that are
already up keep working the whole time.

What gets changed and restored:
  - @everyone's channel overwrite for Send Messages is set to deny, then put
    back to whatever it was before (allow, deny or unset), not simply allowed.
    Blindly allowing would override a category or role deny the server set.
  - The bot's own member overwrite gets Send Messages and Add Reactions
    allowed. A channel @everyone deny beats a server-level role allow, so
    without this the bot would lock itself out. Skipped if the bot is an
    Administrator, since that bypasses overwrites anyway.

Limits:
  - Only @everyone is locked. A role with its own channel allow for Send
    Messages (a mod role, say) can still post, and so can admins.
  - Needs Manage Permissions on the channel. Without it the post goes ahead
    unlocked and the log says why, once per channel per run.
  - If the channel is already denied for @everyone (an admin locked it, or an
    outer lock is active) nothing is touched.

The previous state is written to bot_meta before anything changes, so a crash
or restart mid-post cannot leave a channel locked for good:
restore_stale_locks() runs at startup and puts back anything left over.
"""

import json
import logging
from contextlib import asynccontextmanager

from NFL_Locks.utils.database import get_db

logger = logging.getLogger("utils.channel_lock")

_META_PREFIX = "channel_lock:"
_LOCK_REASON = "Posting matchups"
_UNLOCK_REASON = "Matchups posted"

_warned_no_perms: set[int] = set()


def _is_empty(overwrite) -> bool:
    allow, deny = overwrite.pair()
    return allow.value == 0 and deny.value == 0


async def _restore(bot, channel, state: dict, key: str) -> bool:
    """Put the channel's overwrites back to state. Keeps the bot_meta record on failure."""
    everyone = channel.guild.default_role
    me = channel.guild.me
    try:
        # @everyone first: if the second call fails the channel is at least open.
        ev = channel.overwrites_for(everyone)
        ev.send_messages = state["everyone_send"]
        await channel.set_permissions(
            everyone, overwrite=None if _is_empty(ev) else ev, reason=_UNLOCK_REASON
        )

        if state.get("bot_overwrite"):
            mo = channel.overwrites_for(me)
            mo.send_messages = state["bot_send"]
            mo.add_reactions = state["bot_react"]
            await channel.set_permissions(
                me, overwrite=None if _is_empty(mo) else mo, reason=_UNLOCK_REASON
            )
    except Exception as e:
        logger.error(f"Could not unlock #{channel.name} ({channel.id}): {e!r}")
        try:
            from BotUtils.notify import notify_admin
            await notify_admin(
                bot,
                f"**Could not unlock #{channel.name} after posting matchups.** "
                f"Players may be unable to send messages there. The bot retries on "
                f"its next restart, or fix Send Messages for @everyone by hand. "
                f"Error: {e!r}"
            )
        except Exception:
            pass
        return False

    await get_db().delete_bot_meta(key)
    return True


@asynccontextmanager
async def locked_channel(bot, channel):
    """Deny @everyone Send Messages in channel for the duration of the block."""
    me = channel.guild.me
    my_perms = channel.permissions_for(me)

    if not my_perms.manage_roles:
        if channel.id not in _warned_no_perms:
            _warned_no_perms.add(channel.id)
            logger.warning(
                f"No Manage Permissions in #{channel.name} ({channel.guild.name}), "
                f"posting without locking the channel"
            )
        yield
        return

    everyone = channel.guild.default_role
    ev = channel.overwrites_for(everyone)
    if ev.send_messages is False:
        yield
        return

    needs_bot_overwrite = not my_perms.administrator
    mo = channel.overwrites_for(me)
    state = {
        "everyone_send": ev.send_messages,
        "bot_overwrite": needs_bot_overwrite,
        "bot_send": mo.send_messages,
        "bot_react": mo.add_reactions,
    }
    key = f"{_META_PREFIX}{channel.id}"
    await get_db().set_bot_meta(key, json.dumps(state))

    locked = True
    try:
        # Bot first, so there is never a moment where it has locked itself out.
        if needs_bot_overwrite:
            mo.send_messages = True
            mo.add_reactions = True
            await channel.set_permissions(me, overwrite=mo, reason=_LOCK_REASON)
        ev.send_messages = False
        await channel.set_permissions(everyone, overwrite=ev, reason=_LOCK_REASON)
        logger.info(f"Locked #{channel.name} ({channel.guild.name}) for posting")
    except Exception as e:
        locked = False
        logger.error(f"Could not lock #{channel.name}, posting unlocked: {e!r}")
        await _restore(bot, channel, state, key)

    if not locked:
        yield
        return

    try:
        yield
    finally:
        if await _restore(bot, channel, state, key):
            logger.info(f"Unlocked #{channel.name} ({channel.guild.name})")


async def restore_stale_locks(bot) -> None:
    """Unlock any channel a crash or restart left locked mid-post."""
    db = get_db()
    for key, raw in (await db.get_bot_meta_by_prefix(_META_PREFIX)).items():
        try:
            channel_id = int(key[len(_META_PREFIX):])
            state = json.loads(raw)
        except (ValueError, TypeError):
            logger.error(f"Bad channel lock record {key!r}, dropping it")
            await db.delete_bot_meta(key)
            continue

        channel = bot.get_channel(channel_id)
        if channel is None:
            logger.warning(f"Channel {channel_id} from a stale lock no longer exists, dropping it")
            await db.delete_bot_meta(key)
            continue

        if await _restore(bot, channel, state, key):
            logger.warning(f"Unlocked #{channel.name}, left locked by an interrupted post")
