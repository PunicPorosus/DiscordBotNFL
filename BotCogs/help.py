from discord.ext import commands
import discord


class Help(commands.Cog):
    """Bot-wide help command."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="help")
    async def help_command(self, ctx, category: str = None):
        """Show help by category. Use !help locks or !help trade."""

        # -- No argument: show category landing page ---------------------------
        if category is None:
            embed = discord.Embed(
                title="NFL Bot Help",
                description="Choose a category to see its commands:",
                color=discord.Color.blue(),
            )
            embed.add_field(
                name="Picks Bot",
                value="`!help locks` — Games, standings, results, admin tools & automation",
                inline=False,
            )
            embed.add_field(
                name="Trade Evaluator",
                value="`!help trade` — Draft pick trade evaluation & chart commands\n"
                      "`!help trade.setup` — Profile setup & pick cache management\n"
                      "`!help trade.admin` — Admin sync and cache tools",
                inline=False,
            )
            embed.add_field(
                name="Survivor",
                value="`!help survivor` — Weekly survivor pool commands & rules",
                inline=False,
            )
            embed.set_footer(text="Tip: !help <command> shows detailed info on any specific command")
            await ctx.send(embed=embed)
            return

        cat = category.lower()

        # -- !help locks -------------------------------------------------------
        if cat == "locks":
            embed = discord.Embed(
                title="Picks Bot Commands",
                description="React to team emojis to make your weekly picks!",
                color=discord.Color.blue(),
            )

            embed.add_field(
                name="Getting Started",
                value=(
                    "`!start_locks` — Enable picks for this server (admin)\n"
                    "`!server_status` — Show server configuration & status\n"
                    "`!help <command>` — Get help on a specific command"
                ),
                inline=False,
            )

            embed.add_field(
                name="Games & Schedule",
                value=(
                    "`!games` — Post current NFL week matchups\n"
                    "`!post_games <week>` — Post specific week matchups\n"
                    "   Example: `!post_games 15`\n"
                    "`!update_schedule_now` — Force update NFL schedule (owner only)\n"
                    "`!check_schedule_status` — Check schedule update status (owner only)"
                ),
                inline=False,
            )

            embed.add_field(
                name="Results & Standings",
                value=(
                    "`!weekly_results <week>` — Post weekly results for THIS SERVER\n"
                    "`!season_standings [week]` — Show season standings for THIS SERVER\n"
                    "`!global_standings [week]` — Show combined standings from ALL SERVERS\n"
                    "`!post_wrapup` — Post end-of-season statistics (admin)\n"
                    "`!check_reactions <week>` — View current picks for THIS SERVER"
                ),
                inline=False,
            )

            embed.add_field(
                name="Admin — Server Management",
                value=(
                    "`!start_locks` — Enable picks for this server\n"
                    "`!end_locks` — Disable picks for this server\n"
                    "`!server_status` — Show server configuration & status\n"
                    "`!set_channel` — Set this channel for auto-posting\n"
                    "`!check_channel` — Show configured channel"
                ),
                inline=False,
            )

            embed.add_field(
                name="Admin — Winners & Scoring",
                value=(
                    "`!fetch_winners <week>` — Fetch winners from ESPN\n"
                    "`!set_winners <week> <teams>` — Set winning teams manually\n"
                    "   Example: `!set_winners 10 KC BUF SF`\n"
                    "`!tally_scores <week>` — Calculate scores for THIS SERVER\n"
                    "`!test_api <week>` — Test ESPN API connection"
                ),
                inline=False,
            )

            embed.add_field(
                name="Admin — System Tools",
                value=(
                    "`!force_reaction_catchup <week>` — Manually sync reactions\n"
                    "`!rebuild_cache` — Rebuild message cache\n"
                    "`!cache_stats` — Show cache statistics\n"
                    "`!rerun_startup` — Rerun startup sequence (owner)\n"
                    "`!status_info` — Show bot status & pending work (owner)\n"
                    "`!list_servers` — List all configured servers (owner)\n"
                    "`!reload <cog>` — Reload a specific cog (owner)"
                ),
                inline=False,
            )

            embed.add_field(
                name="Automation",
                value=(
                    "Tuesday 7 AM ET: Fetch winners from ESPN\n"
                    "Tuesday 8 AM ET: Post results & new week games\n"
                    "Game Kickoff: Reactions lock automatically\n"
                    "After Week 18: Season wrap-up posts\n"
                    "August 15th: Schedule auto-updates for new season\n"
                    "Every 30 min: Background reaction sync"
                ),
                inline=False,
            )

            embed.add_field(
                name="Parameter Guide",
                value=(
                    "`<week>` = Required week number (1-18)\n"
                    "`[week]` = Optional week number (defaults to current)\n"
                    "`<teams>` = Space-separated team codes (e.g., KC BUF SF)\n"
                    "`<cog>` = Cog name (e.g., reactions, games, results)"
                ),
                inline=False,
            )

            embed.set_footer(text="Tip: !help <command> for detailed info on any command")
            await ctx.send(embed=embed)
            return

        # -- !help survivor ----------------------------------------------------
        if cat == "survivor":
            embed = discord.Embed(
                title="Survivor Pool — Commands",
                description=(
                    "Pick one team per week. Win 17 consecutive picks or be the last one standing.\n"
                    "You can't reuse a team. Miss a week or pick a loser and you're out."
                ),
                color=discord.Color.red(),
            )

            embed.add_field(
                name="Player Commands",
                value=(
                    "`!survivor_standings` — Show who's alive, eliminated, and winning streaks\n"
                    "`!survivor_mypicks` — Show your pick history and teams used this season"
                ),
                inline=False,
            )

            embed.add_field(
                name="How to Play",
                value=(
                    "React to a team emoji in the survivor channel to make your weekly pick.\n"
                    "One pick per week — reacting to a second team swaps your pick.\n"
                    "Picks lock at the first kickoff of the week.\n"
                    "You're auto-enrolled on your first reaction during the start week.\n"
                    "Enrollment closes after the start week — late joiners cannot enter."
                ),
                inline=False,
            )

            embed.add_field(
                name="Win Conditions",
                value=(
                    "17 consecutive correct picks → winner\n"
                    "Last player(s) standing → winner\n"
                    "If all remaining players are eliminated in the same week, all are declared winners."
                ),
                inline=False,
            )

            embed.add_field(
                name="Admin — Setup",
                value=(
                    "`!survivor_setup` — Set current channel as the survivor channel\n"
                    "`!survivor_start [week]` — Post this week's matchups and open picks\n"
                    "`!survivor_status` — Show survivor config, enrollment count, alive count"
                ),
                inline=False,
            )

            embed.add_field(
                name="Admin — Management",
                value=(
                    "`!survivor_process <week>` — Manually process results for a completed week\n"
                    "`!survivor_fix_pick <add|remove> <user> <team> <week>` — Override a pick\n"
                    "   Example: `!survivor_fix_pick add porosus KC 15`\n"
                    "`!survivor_eliminate <user> <week>` — Manually eliminate a player"
                ),
                inline=False,
            )

            embed.add_field(
                name="Automation",
                value=(
                    "Results post automatically Tuesday/Monday morning alongside locks results.\n"
                    "Pre-deadline sync covers survivor picks along with locks picks.\n"
                    "Startup catchup rebuilds survivor picks from Discord reactions on bot restart."
                ),
                inline=False,
            )

            embed.set_footer(text="Survivor runs in its own channel — separate from the locks channel")
            await ctx.send(embed=embed)
            return

        # -- !help trade -------------------------------------------------------
        if cat == "trade":
            embed = discord.Embed(
                title="Trade Evaluator — Commands",
                description="Evaluate NFL draft pick trades across four value charts.",
                color=discord.Color.blue(),
            )

            embed.add_field(
                name="Evaluate a Trade",
                value=(
                    "`!trade <picks> for <picks>` — Compare two sides of a trade\n"
                    "   `!trade 14 for 28 59`\n"
                    "   `!trade 33 178 for 41 186 27R2`\n"
                    "   `!trade Side A: 14, 28 for Side B: 59, 92`"
                ),
                inline=False,
            )

            embed.add_field(
                name="Find Trade Scenarios (Exact Pick)",
                value=(
                    "`!find.trade.down 33 for 98` — Give 33, already getting 98 — find what else\n"
                    "`!find.trade.down 33 for 98 114` — Give 33, getting 98+114 — balanced?\n"
                    "`!find.trade.up 10 with 33` — Want 10, giving 33 — find what else to add\n"
                    "`!find.trade.up 10 with 33 50` — Want 10, giving 33+50 — balanced?\n"
                    "Reports per-chart: balanced ✅, overpaying ⚠️, or suggests the missing pick(s)\n"
                    "Supports `.johnson`, `.hill`, `.fitz`, `.stuart`, and `.best` suffixes"
                ),
                inline=False,
            )

            embed.add_field(
                name="Find Trade Scenarios (Position-Based)",
                value=(
                    "`!find.trade.down 33 + early 3rd` — Trade down from 33, gain early 3rd\n"
                    "`!find.trade.up 10 with late 2nd` — Trade up to 10, give late 2nd\n"
                    "Positions: `early`, `middle`, `late` — splits each round into thirds\n"
                    "No suffix = all 4 charts | add `.johnson`, `.hill`, `.fitz`, `.stuart` for one chart\n"
                    "Add `.best` for tighter search: 2% tolerance, max 5 picks (vs 5%/3 standard)"
                ),
                inline=False,
            )

            embed.add_field(
                name="Find Trade Scenarios (Team-Based)",
                value=(
                    "`!find.trade.down.team 18 for Ravens` — What can the Ravens offer for pick 18?\n"
                    "`!find.trade.up.team 10 with Chargers` — What do you give the Chargers for pick 10?\n"
                    "Both `for` and `with` are accepted in either command.\n"
                    "Searches only that team's actual picks. Requires your team to be set — see `!help trade.setup`."
                ),
                inline=False,
            )

            embed.add_field(
                name="Pick Input Formats",
                value=(
                    "`59` — Overall pick number\n"
                    "`27R2` — Future pick (2027 Round 2, discounted one round per year)\n"
                    "`2027 2nd` — Future pick (year + ordinal)\n"
                    "`1(16)` or `1.16` — Round/overall formats\n"
                    "Descriptive text is ignored — only pick tokens are parsed"
                ),
                inline=False,
            )

            embed.add_field(
                name="Trade Charts",
                value=(
                    "**Johnson** — Classic Jimmy Johnson chart\n"
                    "**Hill** — Rich Hill chart (DraftTek)\n"
                    "**Fitz-Spiel** — Fitzgerald-Spielberger (Over The Cap)\n"
                    "**Stuart** — Chase Stuart (Football Perspective)\n"
                    "Green embed = balanced trade | Orange = lopsided"
                ),
                inline=False,
            )

            embed.set_footer(text="!help trade.setup — profile & picks cache | !help trade.admin — admin tools")
            await ctx.send(embed=embed)
            return

        # -- !help trade.setup -------------------------------------------------
        if cat == "trade.setup":
            embed = discord.Embed(
                title="Trade Evaluator — Profile & Pick Cache",
                description="Set up your team profile to use team-based trade finding.",
                color=discord.Color.blue(),
            )

            embed.add_field(
                name="Your Profile",
                value=(
                    "`!trade.mode mock` — Use mock offseason picks (Google Sheets)\n"
                    "`!trade.mode nfl` — Use manually-entered NFL draft order\n"
                    "`!trade.mode show` — Show your current mode and team"
                ),
                inline=False,
            )

            embed.add_field(
                name="Your Team",
                value=(
                    "`!trade.picks.load mine <team>` — Set your team\n"
                    "   Example: `!trade.picks.load mine Vikings`\n"
                    "   Fuzzy matching — partial names work (e.g. 'vikings', 'min')\n"
                    "`!trade.picks.show` — Show your team's current draft picks"
                ),
                inline=False,
            )

            embed.add_field(
                name="Modes Explained",
                value=(
                    "**mock** — Picks from your league's Google Sheet (offseason mock draft order)\n"
                    "**nfl** — Picks manually entered via `!trade.picks.set nfl` (see trade.admin)\n"
                    "Each user has their own mode and team stored independently.\n"
                    "**locks** data feeds automatically into the local pick cache and is used\n"
                    "by team-based trade commands when your mode is set to `locks` — but\n"
                    "active trades should use `mock` or `nfl`."
                ),
                inline=False,
            )

            embed.set_footer(text="Once your team is set, use !find.trade.down.team or !find.trade.up.team")
            await ctx.send(embed=embed)
            return

        # -- !help trade.admin -------------------------------------------------
        if cat == "trade.admin":
            embed = discord.Embed(
                title="Trade Evaluator — Admin Tools",
                description="Cache management and sync commands (owner only).",
                color=discord.Color.blue(),
            )

            embed.add_field(
                name="Sync Commands",
                value=(
                    "`!trade.sync mock` — Pull picks from Google Sheets (all 7 rounds)\n"
                    "`!trade.sync locks` — Recompute projected order from NFL Locks game results\n"
                    "`!trade.sync all` — Sync both mock + locks"
                ),
                inline=False,
            )

            embed.add_field(
                name="Cache Status",
                value=(
                    "`!trade.cache.status` — Show last sync time, staleness, team counts, and failure history for both modes"
                ),
                inline=False,
            )

            embed.add_field(
                name="Manual Pick Entry",
                value=(
                    "`!trade.picks.set <mode> <team> <picks...>` — Set a team's picks directly\n"
                    "   `!trade.picks.set nfl \"Kansas City Chiefs\" 29 61 93 125 157 189 221`\n"
                    "   `!trade.picks.set mock Vikings 18 50 82 114 146 178 210`\n"
                    "   Modes: `mock` or `nfl` — replaces existing picks for that team.\n"
                    "   Also resets the sync failure counter.\n"
                    "   Fuzzy team name matching — partial names work if the team is already in cache."
                ),
                inline=False,
            )

            embed.add_field(
                name="Failure Counter Reset",
                value=(
                    "`!trade.execute mock reset` — Re-arm failure alerts after fixing a sheet issue\n"
                    "`!trade.execute nfl reset` — Re-arm failure alerts after manual NFL pick entry\n"
                    "   Full pick-swap execution (trading picks between teams) coming in a later update."
                ),
                inline=False,
            )

            embed.add_field(
                name="Draft Year",
                value=(
                    "`!trade.year.update <year>` — Update the current draft year in config\n"
                    "   Example: `!trade.year.update 2027`\n"
                    "   Run `!reload` after updating to apply the change."
                ),
                inline=False,
            )

            embed.add_field(
                name="Auto-Sync Schedule",
                value=(
                    "Runs hourly, fires at 3 AM ET:\n"
                    "Mock (Feb-Mar): daily sync from Google Sheets\n"
                    "Locks (Sep-Jan): nightly recompute from NFL Locks game data\n"
                    "NFL: manual entry only via `!trade.picks.set nfl`\n"
                    "Admin is notified on mock failure; escalating alert after 2 consecutive failures."
                ),
                inline=False,
            )

            embed.set_footer(text="All trade.admin commands are owner-only")
            await ctx.send(embed=embed)
            return

        # -- Specific command lookup -------------------------------------------
        cmd = self.bot.get_command(cat)
        if cmd:
            embed = discord.Embed(
                title=f"Help: `!{cmd.name}`",
                description=cmd.help or "No detailed help available.",
                color=discord.Color.blue(),
            )
            await ctx.send(embed=embed)
            return

        await ctx.send(
            f"Unknown category or command `{category}`.\n"
            f"Try `!help locks`, `!help trade`, `!help trade.setup`, `!help trade.admin`, or `!help <command>`."
        )


async def setup(bot):
    await bot.add_cog(Help(bot))
