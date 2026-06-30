from discord.ext import commands
import discord
from datetime import datetime
from NFL_Locks.utils.constants import EASTERN
from NFL_Locks.utils.espn_api import fetch_nfl_winners
from NFL_Locks.utils.time_utils import thanksgiving_date, get_week_lock_time
from NFL_Locks.utils.data_utils import load_full_schedule

class Diagnostics(commands.Cog):
    """Troubleshooting and connectivity tests for the bot."""

    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="test")
    async def test_bot(self, ctx):
        """Run a quick system test (checks cogs, time logic, ESPN API, etc.)"""
        embed = discord.Embed(
            title="NFL Bot Diagnostics",
            description="Running internal checks...",
            color=discord.Color.gold()
        )

        # 1️Bot status
        embed.add_field(name="✅ Bot Status", value="Online and responding", inline=False)

        # 2️Cog load verification
        loaded_cogs = list(self.bot.cogs.keys())
        embed.add_field(name="Loaded Cogs", value=", ".join(loaded_cogs) or "None", inline=False)

        # 3️Time and timezone check
        now = datetime.now(EASTERN)
        embed.add_field(name="Time (ET)", value=now.strftime("%A %I:%M %p"), inline=False)

        # 4 Thanksgiving/Christmas logic
        tg = thanksgiving_date(now.year)
        week_start = now - (now - datetime(now.year, tg.month, tg.day)).days % 7 * (now - now)
        lock_time = get_week_lock_time(now)
        embed.add_field(name="Holiday Logic", value=f"Thanksgiving: {tg}\nLock Example: {lock_time.strftime('%b %d %I:%M %p')}", inline=False)

        # 5 ESPN API test (Week 1 only)
        try:
            winners = await fetch_nfl_winners(1)
            if winners:
                embed.add_field(name="ESPN API", value=f"Fetched {len(winners)} winners (Week 1)", inline=False)
            else:
                embed.add_field(name="ESPN API", value="No data fetched (may be offseason)", inline=False)
        except Exception as e:
            embed.add_field(name="ESPN API", value=f"❌ Error: {e}", inline=False)

        # 6️⃣ Schedule file test
        schedule = load_full_schedule()
        if schedule:
            embed.add_field(name="Schedule File", value="Loaded successfully", inline=False)
        else:
            embed.add_field(name="Schedule File", value="❌ Could not load `data/full_schedule.json`", inline=False)

        embed.set_footer(text="Use this to verify configuration and connectivity.")
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(Diagnostics(bot))
