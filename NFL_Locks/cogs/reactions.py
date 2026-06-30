from discord.ext import commands
import discord
import logging
import asyncio
from NFL_Locks.utils.database import get_db
from NFL_Locks.utils.constants import NFL_TEAMS, EASTERN, emoji_to_team
from NFL_Locks.utils.time_utils import is_deadline_passed, get_week_deadline
from NFL_Locks.utils.schedule_utils import get_max_week, get_current_season
from datetime import datetime, timedelta
from NFL_Locks.utils.command_names import CMD_UPDATE_REACTIONS, CMD_PROCESS_EXISTING

logger = logging.getLogger(__name__)

class Reactions(commands.Cog):
    """Handles reaction tracking for game picks."""

    def __init__(self, bot):
        self.bot = bot
        self.message_cooldowns = {}  # Track message cooldowns per user
        self.dm_cooldowns = {}       # Track DM cooldowns per user
        self.processing_reactions = set()  # Prevent reaction loops

        # -- Reconciliation gate -----------------------------------------------
        # When reconciliation_active is True, live reaction events are buffered
        # instead of written to DB. reaction_catchup flushes the buffer per guild
        # after its API rebuild completes, then clears the flag when all guilds
        # are done. Deadline-blocked reactions bypass the buffer and are rejected
        # immediately so post-deadline picks are never sneaked through.
        self.reconciliation_active: bool = False
        self.pending_reactions: list[dict] = []

    # -- Cooldown helpers ------------------------------------------------------

    def _check_message_cooldown(self, user_id):
        """Check if user is on cooldown for channel messages (30 seconds).

        Lazily prunes the entry on lookup when the TTL has expired, keeping
        the dict bounded to only genuinely active cooldowns.
        """
        current_time = datetime.now(EASTERN)
        last_time = self.message_cooldowns.get(user_id)
        if last_time is not None and (current_time - last_time).total_seconds() > 30:
            del self.message_cooldowns[user_id]
            last_time = None
        if last_time is None:
            self.message_cooldowns[user_id] = current_time
            return True
        return False

    def _check_dm_cooldown(self, user_id):
        """Check if user is on cooldown for DMs (60 seconds).

        Lazily prunes the entry on lookup when the TTL has expired, keeping
        the dict bounded to only genuinely active cooldowns.
        """
        current_time = datetime.now(EASTERN)
        last_time = self.dm_cooldowns.get(user_id)
        if last_time is not None and (current_time - last_time).total_seconds() > 60:
            del self.dm_cooldowns[user_id]
            last_time = None
        if last_time is None:
            self.dm_cooldowns[user_id] = current_time
            return True
        return False

    # -- Reaction listeners ----------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """
        Track user picks when they react to game messages.

        Uses the raw gateway event rather than the high-level on_reaction_add
        so that reactions on messages that are no longer in discord.py's
        internal cache are still captured. This matters after a bot restart:
        old weekly game messages are not cached and on_reaction_add silently
        drops events on them.
        """
        # Ignore DMs and bot reactions
        if payload.guild_id is None:
            return
        if payload.member and payload.member.bot:
            return

        reaction_key = (payload.message_id, payload.user_id, str(payload.emoji))
        if reaction_key in self.processing_reactions:
            return

        cache_cog = self.bot.get_cog('Cache')
        if not cache_cog:
            logger.error("Cache cog not found - cannot process reactions")
            return

        week_num = cache_cog.get_week_from_cache(payload.message_id)

        if not week_num:
            week_num = await self.find_week_for_message(payload.message_id)
            if week_num:
                cache_cog.add_to_cache(payload.message_id, week_num)
            else:
                return

        member_name = payload.member.name if payload.member else str(payload.user_id)
        logger.info(f"[REACTION] {member_name} added {payload.emoji} on Week {week_num} message")

        if is_deadline_passed(week_num):
            deadline = get_week_deadline(week_num)
            logger.warning(f"[DEADLINE] Blocking reaction from {member_name} - deadline passed")

            self.processing_reactions.add(reaction_key)
            try:
                channel = self.bot.get_channel(payload.channel_id)
                if channel:
                    try:
                        message = await channel.fetch_message(payload.message_id)
                        await message.remove_reaction(payload.emoji, payload.member)
                        logger.debug(f"Successfully removed reaction from {member_name}")
                    except discord.Forbidden:
                        logger.warning("Missing permissions to remove reaction")
                        if self._check_message_cooldown(payload.user_id):
                            try:
                                await channel.send(
                                    f"<@{payload.user_id}> Submissions for Week {week_num} are closed. "
                                    f"This message will be deleted in 5 seconds.",
                                    delete_after=5.0
                                )
                            except Exception as msg_error:
                                logger.error(f"Error sending temporary message: {msg_error}")
                    except discord.NotFound:
                        logger.warning(f"Message {payload.message_id} not found when removing reaction")
                    except Exception as e:
                        logger.error(f"Error removing reaction: {e}")

                if self._check_dm_cooldown(payload.user_id):
                    try:
                        if payload.member:
                            deadline_str = deadline.strftime("%A, %B %d at %I:%M %p ET")
                            await payload.member.send(
                                f"❌ Sorry, submissions for Week {week_num} closed at {deadline_str}."
                            )
                    except discord.Forbidden:
                        pass
                    except Exception as e:
                        logger.error(f"Error sending DM: {e}")

            finally:
                await self._clear_processing_key(reaction_key, delay=2)

            return

        # Buffer during reconciliation rather than writing directly to DB.
        # Week_num is already resolved above so no extra DB call needed at flush.
        if self.reconciliation_active:
            self.pending_reactions.append({
                'type': 'add',
                'guild_id': str(payload.guild_id),
                'user_id': str(payload.user_id),
                'user_name': payload.member.name if payload.member else str(payload.user_id),
                'emoji': payload.emoji,
                'week_num': week_num,
                'message_id': payload.message_id,
                'channel_id': payload.channel_id,
                'member': payload.member,
            })
            logger.debug(
                f"[BUFFER] Queued add for user {payload.user_id} "
                f"Week {week_num} (reconciliation active)"
            )
            return

        await self._process_reaction_internal(
            guild_id=str(payload.guild_id),
            user_id=str(payload.user_id),
            user_name=payload.member.name if payload.member else str(payload.user_id),
            emoji=payload.emoji,
            week_num=week_num,
            message_id=payload.message_id,
            channel_id=payload.channel_id,
            member=payload.member,
        )

    async def _clear_processing_key(self, key, delay=2):
        """Clear a processing key after a delay to prevent loops."""
        await asyncio.sleep(delay)
        self.processing_reactions.discard(key)

    async def _process_reaction_internal(
        self,
        guild_id: str,
        user_id: str,
        user_name: str,
        emoji,
        week_num: int,
        message_id: int | str | None = None,
        channel_id: int | None = None,
        member=None,
    ):
        """
        Record a pick in SQLite and enforce one-pick-per-matchup.

        Accepts raw components rather than discord.py objects so it can be
        called from both the raw gateway listener and the admin rebuild commands.

        When message_id and channel_id are provided, the one-pick-per-matchup
        rule is enforced: if the user already has the opposing team picked for
        this game, that pick is removed from the DB and its Discord reaction is
        stripped. message_id/channel_id are omitted by the reconciliation path,
        which handles deduplication separately.
        """
        team = emoji_to_team(emoji)
        if not team:
            logger.debug(f"Could not map emoji {emoji} to team")
            return

        logger.info(f"[REACTION] Mapped to team: {team}")

        season = get_current_season()
        db = get_db()

        inserted = await db.add_pick(
            season=season,
            week=week_num,
            guild_id=guild_id,
            user_id=user_id,
            user_name=user_name,
            team=team,
        )
        if inserted:
            logger.info(f"[SUCCESS] {user_name} picked {team} for Week {week_num}")
        else:
            logger.debug(f"{user_name} already has {team} pick")
            return

        # -- One-pick-per-matchup enforcement ----------------------------------
        # Only runs on the live reaction path (message_id present). The
        # reconciliation path skips this and handles deduplication separately.
        if not message_id:
            return

        matchup = await db.get_matchup_teams(message_id)
        if not matchup:
            return  # Non-matchup message (header/footer) or pre-schema message

        team_a, team_b = matchup
        opponent = team_b if team == team_a else team_a

        prior_pick = await db.get_user_pick_for_matchup(
            season, week_num, guild_id, user_id, team_a, team_b
        )
        # prior_pick will be `team` itself (just inserted) unless the opponent
        # was already there — so only act when the conflict is the opponent.
        if prior_pick != opponent:
            return

        # Remove the conflicting pick from the DB.
        removed = await db.remove_pick(
            season=season,
            week=week_num,
            guild_id=guild_id,
            user_id=user_id,
            team=opponent,
        )
        if not removed:
            return

        logger.info(
            f"[MATCHUP] Removed conflicting {opponent} pick for {user_name} "
            f"(switched to {team}, Week {week_num})"
        )

        # Remove the opponent emoji reaction from Discord.
        if not channel_id:
            return

        opponent_emoji = NFL_TEAMS.get(opponent)
        if not opponent_emoji:
            return

        try:
            channel = self.bot.get_channel(channel_id)
            if not channel:
                return
            message = await channel.fetch_message(int(message_id))
            # member may be None on some paths — fetch if needed
            target = member
            if target is None:
                try:
                    target = await self.bot.fetch_user(int(user_id))
                except Exception:
                    logger.warning(
                        f"[MATCHUP] Could not fetch user {user_id} to remove reaction"
                    )
                    return
            await message.remove_reaction(opponent_emoji, target)
            logger.debug(
                f"[MATCHUP] Stripped {opponent} reaction from {user_name} on message {message_id}"
            )
        except discord.Forbidden:
            logger.warning(
                f"[MATCHUP] Missing permissions to remove {opponent} reaction "
                f"for {user_name} on message {message_id}"
            )
        except discord.NotFound:
            logger.warning(
                f"[MATCHUP] Message {message_id} not found when removing conflicting reaction"
            )
        except Exception as e:
            logger.error(f"[MATCHUP] Error removing conflicting reaction: {e}")

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        """
        Remove user picks when they un-react.

        Uses the raw gateway event for the same cache-independence reason as
        on_raw_reaction_add. Note: Discord does NOT populate payload.member
        for remove events, so the user must be fetched explicitly when a DM
        is needed (deadline-blocked path).
        """
        if payload.guild_id is None:
            return

        reaction_key = (payload.message_id, payload.user_id, str(payload.emoji))
        if reaction_key in self.processing_reactions:
            return

        cache_cog = self.bot.get_cog('Cache')
        if not cache_cog:
            return

        week_num = cache_cog.get_week_from_cache(payload.message_id)

        if not week_num:
            week_num = await self.find_week_for_message(payload.message_id)
            if not week_num:
                return

        logger.info(
            f"[REACTION] user {payload.user_id} removed {payload.emoji} on Week {week_num} message"
        )

        if is_deadline_passed(week_num):
            deadline = get_week_deadline(week_num)
            logger.info(
                f"[DEADLINE] Preventing removal for user {payload.user_id} - deadline passed"
            )

            if self._check_dm_cooldown(payload.user_id):
                try:
                    # payload.member is None on remove events — fetch the user explicitly
                    user = await self.bot.fetch_user(payload.user_id)
                    deadline_str = deadline.strftime("%A, %B %d at %I:%M %p ET")
                    await user.send(
                        f"❌ Sorry, you cannot change picks after {deadline_str}. "
                        f"Your pick for Week {week_num} remains locked."
                    )
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    pass
                except Exception as e:
                    logger.error(f"Error sending DM: {e}")

            # Do NOT restore the reaction — just prevent the data removal
            return

        team = emoji_to_team(payload.emoji)
        if not team:
            return

        # Buffer during reconciliation. Team is resolved before buffering so
        # flush_pending_reactions can call remove_pick directly without re-resolving.
        if self.reconciliation_active:
            self.pending_reactions.append({
                'type': 'remove',
                'guild_id': str(payload.guild_id),
                'user_id': str(payload.user_id),
                'team': team,
                'week_num': week_num,
            })
            logger.debug(
                f"[BUFFER] Queued remove for user {payload.user_id} "
                f"Week {week_num} (reconciliation active)"
            )
            return

        season = get_current_season()
        db = get_db()

        # Resolve display name for logging — best-effort from guild member cache
        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id) if guild else None
        user_name = member.name if member else str(payload.user_id)

        deleted = await db.remove_pick(
            season=season,
            week=week_num,
            guild_id=str(payload.guild_id),
            user_id=str(payload.user_id),
            team=team,
        )
        if deleted:
            logger.info(f"[SUCCESS] {user_name} removed pick for {team} Week {week_num}")

    # -- Emoji helper ----------------------------------------------------------

    # -- Message → week lookup -------------------------------------------------

    async def find_week_for_message(self, message_id):
        """
        Look up which week a message belongs to via SQLite.

        O(1) indexed lookup. Returns None if the message is not tracked in DB.
        """
        db = get_db()
        return await db.get_week_for_message(message_id)

    # -- Admin commands --------------------------------------------------------

    @commands.command(name=CMD_UPDATE_REACTIONS)
    @commands.has_permissions(administrator=True)
    async def update_reactions(self, ctx, week_num: int = None):
        """
        Rebuild picks for a week from live Discord reactions.

        Clears all existing picks for the calling guild and re-inserts from
        current reaction state. If week_num is omitted the current week is used.
        """
        if week_num is None:
            from NFL_Locks.utils.data_utils import load_full_schedule

            today = datetime.now(EASTERN)
            schedule = load_full_schedule()

            for wk in range(1, get_max_week() + 1):
                week_games = schedule.get(str(wk))
                if not week_games:
                    continue

                first_game = datetime.fromisoformat(week_games[0]["date"]).replace(tzinfo=EASTERN)
                days_since_tuesday = (first_game.weekday() - 1) % 7
                week_start = first_game - timedelta(days=days_since_tuesday)
                week_end = week_start + timedelta(days=6, hours=23, minutes=59)

                if week_start <= today <= week_end:
                    week_num = wk
                    break

            if week_num is None:
                await ctx.send("❌ Could not determine current week.")
                return

        if not (1 <= week_num <= get_max_week()):
            await ctx.send(f"❌ Week number must be between 1 and {get_max_week()}.")
            return

        await ctx.send(f"Updating reactions for Week {week_num}...")

        guild_id = str(ctx.guild.id)
        season = get_current_season()
        db = get_db()

        messages_by_channel = await db.get_messages_for_week(season, week_num, guild_id)

        if not messages_by_channel:
            await ctx.send("❌ No tracked messages found for this server.")
            return

        await db.clear_picks_for_week(season, week_num, guild_id)

        emoji_to_team = self._build_emoji_map()
        updated_count = 0

        for channel_id_str, message_ids in messages_by_channel.items():
            channel = self.bot.get_channel(int(channel_id_str))
            if not channel:
                logger.warning(f"Channel {channel_id_str} not found — skipping")
                continue

            for msg_id in message_ids:
                try:
                    message = await channel.fetch_message(msg_id)
                except (discord.NotFound, discord.Forbidden):
                    logger.warning(f"Message {msg_id} not accessible in channel {channel_id_str}")
                    continue
                except Exception as e:
                    logger.error(f"Error fetching message {msg_id}: {e}")
                    continue

                for reaction in message.reactions:
                    team = self._reaction_str_to_team(str(reaction.emoji), emoji_to_team)
                    if not team:
                        continue

                    async for user in reaction.users():
                        if user.bot:
                            continue
                        inserted = await db.add_pick(
                            season=season,
                            week=week_num,
                            guild_id=guild_id,
                            user_id=str(user.id),
                            user_name=user.name,
                            team=team,
                        )
                        if inserted:
                            updated_count += 1

        await ctx.send(
            f"✅ Updated Week {week_num} reactions!\n"
            f"Total picks recorded: {updated_count}"
        )

    @commands.command(name=CMD_PROCESS_EXISTING)
    @commands.has_permissions(administrator=True)
    async def process_existing_reactions(self, ctx, week_num: int):
        """
        Additively process reactions on tracked messages for a week.

        Unlike !update_reactions this does NOT clear first — it only adds
        picks that are missing. Useful for catching up after a brief outage.
        """
        await ctx.send(f"Processing existing reactions for Week {week_num}...")

        guild_id = str(ctx.guild.id)
        season = get_current_season()
        db = get_db()

        messages_by_channel = await db.get_messages_for_week(season, week_num, guild_id)

        if not messages_by_channel:
            await ctx.send("No tracked messages found for this server.")
            return

        processed = 0
        not_found = 0

        for channel_id_str, message_ids in messages_by_channel.items():
            channel = self.bot.get_channel(int(channel_id_str))
            if not channel:
                logger.warning(f"Channel {channel_id_str} not found — skipping")
                not_found += len(message_ids)
                continue

            for message_id in message_ids:
                try:
                    message = await channel.fetch_message(message_id)
                except (discord.NotFound, discord.Forbidden):
                    not_found += 1
                    continue
                except Exception as e:
                    logger.error(f"Error fetching message {message_id}: {e}")
                    not_found += 1
                    continue

                for reaction in message.reactions:
                    async for user in reaction.users():
                        if user.bot:
                            continue
                        await self._process_reaction_internal(
                            guild_id=guild_id,
                            user_id=str(user.id),
                            user_name=user.name,
                            emoji=reaction.emoji,
                            week_num=week_num,
                        )
                        processed += 1

        result_msg = f"Processed {processed} existing reactions"
        if not_found > 0:
            result_msg += f"\n{not_found} messages not found"
        await ctx.send(result_msg)

    # -- Reconciliation buffer helpers -----------------------------------------

    async def flush_pending_reactions(self, guild_id: str):
        """
        Drain buffered reactions for a specific guild and write them to DB.

        Called by reaction_catchup after the API rebuild for a guild completes.
        Since add_pick is INSERT OR IGNORE, any add that was already captured
        by the API rebuild is a harmless no-op. Remove events apply cleanly
        against the freshly rebuilt picks.
        """
        entries = [e for e in self.pending_reactions if e['guild_id'] == guild_id]
        self.pending_reactions = [e for e in self.pending_reactions if e['guild_id'] != guild_id]

        if not entries:
            return

        db = get_db()
        season = get_current_season()
        flushed = 0

        for entry in entries:
            try:
                if entry['type'] == 'add':
                    await self._process_reaction_internal(
                        guild_id=entry['guild_id'],
                        user_id=entry['user_id'],
                        user_name=entry['user_name'],
                        emoji=entry['emoji'],
                        week_num=entry['week_num'],
                        message_id=entry.get('message_id'),
                        channel_id=entry.get('channel_id'),
                        member=entry.get('member'),
                    )
                    flushed += 1
                elif entry['type'] == 'remove':
                    await db.remove_pick(
                        season=season,
                        week=entry['week_num'],
                        guild_id=entry['guild_id'],
                        user_id=entry['user_id'],
                        team=entry['team'],
                    )
                    flushed += 1
            except Exception as e:
                logger.error(f"[BUFFER] Error flushing entry for guild {guild_id}: {e}")

        logger.info(f"[BUFFER] Flushed {flushed} buffered reactions for guild {guild_id}")

    def clear_pending_for_guild(self, guild_id: str):
        """
        Discard buffered reactions for a guild without writing them.

        Called when reconciliation fails for a guild — DB state is undefined
        so the buffer should not be applied.
        """
        before = len(self.pending_reactions)
        self.pending_reactions = [e for e in self.pending_reactions if e['guild_id'] != guild_id]
        discarded = before - len(self.pending_reactions)
        if discarded:
            logger.warning(
                f"[BUFFER] Discarded {discarded} buffered reactions for failed guild {guild_id}"
            )

    # -- Private helpers -------------------------------------------------------

    def _build_emoji_map(self) -> dict:
        """Return a mapping from emoji string/ID to team abbreviation."""
        emoji_to_team = {}
        for abbr, emoji in NFL_TEAMS.items():
            if emoji.startswith('<:') and emoji.endswith('>'):
                parts = emoji.split(':')
                if len(parts) >= 3:
                    emoji_id = parts[2].rstrip('>')
                    emoji_to_team[emoji_id] = abbr
            else:
                emoji_to_team[emoji] = abbr
        return emoji_to_team

    def _reaction_str_to_team(self, reaction_str: str, emoji_map: dict) -> str | None:
        """Resolve a reaction string to a team abbreviation using a pre-built map."""
        if reaction_str.startswith('<:') and reaction_str.endswith('>'):
            parts = reaction_str.split(':')
            if len(parts) >= 3:
                return emoji_map.get(parts[2].rstrip('>'))
        return emoji_map.get(reaction_str)


async def setup(bot):
    await bot.add_cog(Reactions(bot))
