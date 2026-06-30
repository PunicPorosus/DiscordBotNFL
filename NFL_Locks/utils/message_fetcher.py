"""
Optimized Message Fetcher - Bulk message fetching with caching

This module provides efficient message fetching for large-scale operations:
- Bulk parallel fetching from known channels
- In-memory message-channel mapping (source of truth is the DB tracked_messages table)
- Rate limit aware batching
"""

import asyncio
import discord
import logging
from typing import Dict, List, Optional
from NFL_Locks.utils.constants import emoji_to_team

logger = logging.getLogger('utils.message_fetcher')


class MessageFetcher:
    """Optimized message fetcher with caching and bulk operations."""

    def __init__(self, bot):
        self.bot = bot
        # In-memory cache only -- the DB's tracked_messages table is the
        # persistent source of truth.  No JSON file is written or read.
        self.message_channel_cache: Dict[str, int] = {}

    def add_to_cache(self, message_id: int, channel_id: int):
        """Add a message-channel mapping to the in-memory cache."""
        self.message_channel_cache[str(message_id)] = channel_id

    def get_from_cache(self, message_id: int) -> Optional[int]:
        """Get channel ID for a message from the in-memory cache."""
        return self.message_channel_cache.get(str(message_id))

    async def fetch_messages_bulk(
        self,
        messages_by_channel: Dict[int, List[int]],
        batch_size: int = 5,
        delay_between_batches: float = 2.0
    ) -> Dict[int, List[discord.Message]]:
        """Fetch multiple messages from multiple channels efficiently.

        Args:
            messages_by_channel: {channel_id: [message_ids]}
            batch_size: Number of messages to fetch in parallel per batch
            delay_between_batches: Seconds to wait between batches

        Returns:
            {channel_id: [Message objects]}
        """
        results = {}

        for channel_id, message_ids in messages_by_channel.items():
            channel = self.bot.get_channel(channel_id)
            if not channel:
                logger.warning(f"Channel {channel_id} not found")
                continue

            messages = []

            # Fetch in batches
            for i in range(0, len(message_ids), batch_size):
                batch = message_ids[i:i + batch_size]

                fetch_tasks = [channel.fetch_message(int(mid)) for mid in batch]
                batch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

                for msg_id, result in zip(batch, batch_results):
                    if isinstance(result, discord.Message):
                        messages.append(result)
                        # Update cache
                        self.add_to_cache(result.id, channel_id)
                    elif isinstance(result, Exception):
                        logger.debug(f"Could not fetch message {msg_id}: {result}")

                # Rate limit protection
                if i + batch_size < len(message_ids):
                    await asyncio.sleep(delay_between_batches)

            if messages:
                results[channel_id] = messages

        return results

    async def fetch_messages_with_cache(
        self,
        guild: discord.Guild,
        message_ids: List[int],
        batch_size: int = 5,
        delay_between_batches: float = 2.0
    ) -> Dict[int, List[discord.Message]]:
        """Fetch messages using cache to determine channels.

        Falls back to searching if not in cache.

        Returns:
            {channel_id: [Message objects]}
        """
        # Group messages by channel using cache
        messages_by_channel = {}
        uncached_messages = []

        for msg_id in message_ids:
            channel_id = self.get_from_cache(msg_id)
            if channel_id:
                if channel_id not in messages_by_channel:
                    messages_by_channel[channel_id] = []
                messages_by_channel[channel_id].append(msg_id)
            else:
                uncached_messages.append(msg_id)

        # Fetch cached messages
        results = await self.fetch_messages_bulk(
            messages_by_channel,
            batch_size,
            delay_between_batches
        )

        # Search for uncached messages
        if uncached_messages:
            logger.info(f"Searching for {len(uncached_messages)} uncached messages")
            found_messages = await self._search_for_messages(
                guild,
                uncached_messages
            )

            # Merge results
            for channel_id, messages in found_messages.items():
                if channel_id not in results:
                    results[channel_id] = []
                results[channel_id].extend(messages)

        return results

    async def _search_for_messages(
        self,
        guild: discord.Guild,
        message_ids: List[int]
    ) -> Dict[int, List[discord.Message]]:
        """Search through guild channels to find messages."""
        results = {}
        found_ids = set()

        for message_id in message_ids:
            if message_id in found_ids:
                continue

            for channel in guild.text_channels:
                if not channel.permissions_for(guild.me).read_message_history:
                    continue

                try:
                    message = await channel.fetch_message(message_id)

                    # Add to results
                    if channel.id not in results:
                        results[channel.id] = []
                    results[channel.id].append(message)

                    # Update cache
                    self.add_to_cache(message_id, channel.id)
                    found_ids.add(message_id)

                    break

                except discord.NotFound:
                    continue
                except discord.Forbidden:
                    continue
                except Exception as e:
                    logger.debug(f"Error searching for message {message_id}: {e}")
                    continue

        return results

    async def build_reaction_state(
        self,
        messages_by_channel: Dict[int, List[discord.Message]],
        delay_per_message: float = 0.5,
        matchup_map: Dict[int, tuple] | None = None,
    ) -> Dict[str, Dict]:
        """
        Build a snapshot of Discord reaction state without writing to the DB.

        Returns:
            {user_id: {'name': str, 'teams': set[str]}}

        Iterates reaction.users() per message with a small delay between messages
        to avoid bursting the reactions endpoint on high-traffic servers.
        Emoji->team resolution uses constants.emoji_to_team (O(1), no I/O).

        matchup_map: optional {message_id: (team_a, team_b)} -- when provided,
        enforces one-pick-per-matchup during state building. If a user has
        reacted to both teams on the same game message, only the first one
        encountered is kept. This mirrors the live reaction handler behaviour
        and prevents invalid pairs from being written during reconciliation.
        """
        state: Dict[str, Dict] = {}

        for channel_id, messages in messages_by_channel.items():
            for message in messages:
                matchup = matchup_map.get(message.id) if matchup_map else None
                # Tracks which matchup team each user has already been assigned
                # for this specific game message. Reset per message.
                matchup_claimed: Dict[str, str] = {}  # {user_id: team}

                for reaction in message.reactions:
                    team = emoji_to_team(reaction.emoji)
                    if not team:
                        continue
                    async for user in reaction.users():
                        if user.bot:
                            continue
                        uid = str(user.id)

                        # One-pick-per-matchup: if this message is a known game
                        # and the user already has a team from it, skip this one.
                        if matchup and team in matchup:
                            if uid in matchup_claimed:
                                logger.debug(
                                    f"[RECONCILE] Skipping duplicate matchup pick: "
                                    f"user {uid} already has {matchup_claimed[uid]} "
                                    f"on message {message.id}, ignoring {team}"
                                )
                                continue
                            matchup_claimed[uid] = team

                        if uid not in state:
                            state[uid] = {'name': user.name, 'teams': set()}
                        state[uid]['teams'].add(team)

                # Small delay per message to avoid bursting the reactions endpoint
                await asyncio.sleep(delay_per_message)

        return state


# Global instance (will be initialized by a cog)
_message_fetcher = None

def get_message_fetcher(bot) -> MessageFetcher:
    """Get or create the global message fetcher instance."""
    global _message_fetcher
    if _message_fetcher is None:
        _message_fetcher = MessageFetcher(bot)
    return _message_fetcher
