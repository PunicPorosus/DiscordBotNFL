"""
Proactive Discord send pacing.

Discord allows 5 messages per 5 seconds per channel (≈ 1/sec).
Rather than hitting that wall and relying on discord.py's 429 retry,
we enforce a minimum interval between sends to the same destination,
keeping us comfortably under the limit.

Usage (drop-in for channel.send):
    from NFL_Locks.utils.rate_limiter import rate_limiter
    await rate_limiter.send(channel, "Hello!")
    await rate_limiter.send(channel, embed=some_embed)

Works for any discord.abc.Messageable: TextChannel, DMChannel, User, ctx.
"""

import asyncio
import logging
import time
from collections import defaultdict

logger = logging.getLogger("nfl_locks.rate_limiter")

# Discord limit: 5 per 5 s per channel = 1.00/s.
# We pace at 1.1 s — 10 % headroom — so we never reach the wall.
_SEND_INTERVAL = 1.1  # seconds between sends to the same channel


class ChannelRateLimiter:
    """Serialises and paces outgoing sends per Discord channel / DM."""

    def __init__(self, interval: float = _SEND_INTERVAL):
        self.interval = interval
        # monotonic timestamps of the last completed send per destination id
        self._last_send: dict[int, float] = defaultdict(float)
        # one asyncio.Lock per destination so concurrent callers queue up
        # rather than racing each other
        self._channel_locks: dict[int, asyncio.Lock] = {}

    def _get_lock(self, dest_id: int) -> asyncio.Lock:
        # Lazy creation is safe here: asyncio is single-threaded, so two
        # coroutines cannot both be inside this method simultaneously.
        if dest_id not in self._channel_locks:
            self._channel_locks[dest_id] = asyncio.Lock()
        return self._channel_locks[dest_id]

    async def send(self, destination, *args, **kwargs):
        """
        Rate-limited send.  Signature mirrors discord.abc.Messageable.send().

        destination — any Messageable (TextChannel, DMChannel, User, ctx, …)
        *args / **kwargs — forwarded verbatim to destination.send()
        """
        dest_id = destination.id
        lock = self._get_lock(dest_id)

        async with lock:
            now = time.monotonic()
            wait = self._last_send[dest_id] + self.interval - now
            if wait > 0:
                logger.debug(
                    f"Rate-limiter: sleeping {wait:.3f}s before send to {dest_id}"
                )
                await asyncio.sleep(wait)
            result = await destination.send(*args, **kwargs)
            self._last_send[dest_id] = time.monotonic()
            return result

    async def reply(self, message, *args, **kwargs):
        """
        Rate-limited message.reply().  Paces on the message's channel id.

        message — discord.Message
        """
        dest_id = message.channel.id
        lock = self._get_lock(dest_id)

        async with lock:
            now = time.monotonic()
            wait = self._last_send[dest_id] + self.interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            result = await message.reply(*args, **kwargs)
            self._last_send[dest_id] = time.monotonic()
            return result


# Process-wide singleton — import this everywhere.
rate_limiter = ChannelRateLimiter()
