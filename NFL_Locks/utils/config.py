"""
NFL Locks Bot — Central Configuration
======================================
All magic numbers and tunable constants live here.
Change a value once and it takes effect across the entire bot.

NOTE: DEFAULT_MAX_WEEK is the *known* baseline season length used only as an
ultimate fallback when no schedule file is present, and for the startup sanity
check. The live week count always comes from get_max_week() (schedule_utils.py),
which reads the loaded schedule metadata. If the NFL expands the season, the bot
will log a warning at startup prompting you to update DEFAULT_MAX_WEEK here.
"""

# -- Season / Week -------------------------------------------------------------
# Update this if the NFL expands the regular season (e.g. to 19 weeks).
DEFAULT_MAX_WEEK = 18

SEASON_END_GRACE_DAYS = 7         # Days after final week to stay "in-season"
                                   # Allows Tuesday results/standings for last week
                                   # even though the bot is technically post-season
WEEK_POSTING_LOOKBACK_DAYS = 30   # Max days back when checking for unposted results

# -- Scheduling Times (all Eastern) -------------------------------------------
TUESDAY = 1                        # datetime.weekday() value for Tuesday

RESULTS_POST_HOUR = 8              # Hour (ET) the Tuesday auto-post task fires
RESULTS_POST_WINDOW_START = 7      # Earliest hour catchup will post results
RESULTS_POST_WINDOW_END = 10       # Latest hour catchup will post results

SCHEDULE_UPDATE_HOUR = 9           # Hour (ET) the schedule-update check runs
SCHEDULE_UPDATE_MONTH = 8          # Month the schedule auto-update triggers (August)
SCHEDULE_UPDATE_DAY = 15           # Day of month the schedule auto-update triggers
SCHEDULE_RESET_MONTH = 1           # Month the "schedule updated" flag resets (January)
SCHEDULE_RESET_DAY = 1             # Day the flag resets (January 1)

SEASON_ARCHIVE_DAY = 1             # Day of SCHEDULE_UPDATE_MONTH to auto-archive last season's DB
                                   # Aug 1 → archive, Aug 15 → schedule pull (two clean windows)

WRAPUP_POST_WINDOW_DAYS = 8        # Days after final week during which wrapup may post
LATE_NIGHT_THRESHOLD_HOUR = 22     # Hour (ET) after which games count as "late night"

# -- Background Task Intervals -------------------------------------------------
LOCK_CHECK_INTERVAL_MIN = 5        # How often the lock-time checker runs (minutes)
REACTION_CATCHUP_INTERVAL_MIN = 5  # How often the reaction catchup task runs (minutes)
AUTO_TASKS_INTERVAL_HOURS = 1      # How often the dynamic weekly task checker runs
SEASON_END_CHECK_INTERVAL_HOURS = 1  # How often the season-end checker runs

# -- Game / Lock Timing --------------------------------------------------------
GAME_DURATION_HOURS = 4            # Assumed max game duration for window checks
LOCK_WINDOW_SECONDS = 300          # Seconds after lock time still treated as "just locked"

PRE_DEADLINE_SYNC_MIN_LOW = 10     # Lower bound of pre-deadline sync window (min before)
PRE_DEADLINE_SYNC_MIN_HIGH = 20    # Upper bound of pre-deadline sync window (min before)

# -- Startup / Initialization --------------------------------------------------
STARTUP_WAIT_SECONDS = 5           # Delay before startup sequence begins
INITIAL_CATCHUP_WAIT_SECONDS = 10  # Delay before initial reaction catchup runs

# -- Rate Limiting / Batching --------------------------------------------------
MESSAGE_FETCH_BATCH_SIZE = 5       # Messages fetched per batch (standard operations)
MESSAGE_FETCH_DELAY = 2.0          # Seconds between fetch batches (standard)
CATCHUP_BATCH_SIZE = 3             # Messages fetched per batch (reaction rebuild)
CATCHUP_BATCH_DELAY = 3.0          # Seconds between fetch batches (reaction rebuild)
MESSAGE_POST_DELAY = 0.5           # Seconds between posting individual game messages

# -- Cooldowns -----------------------------------------------------------------
REACTION_COOLDOWN_SECONDS = 30     # Min seconds between reaction confirmation messages
DM_COOLDOWN_SECONDS = 60           # Min seconds between DMs to the same user
TEMP_MESSAGE_DELETE_SECONDS = 5.0  # Seconds before temporary bot messages self-delete

# -- API / Process Timeouts ----------------------------------------------------
ESPN_API_TIMEOUT_SECONDS = 15      # aiohttp total timeout for ESPN API calls
FETCH_WINNERS_TIMEOUT_SECONDS = 8  # Timeout for winner-fetch subprocess
SCHEDULE_UPDATE_TIMEOUT_SECONDS = 300  # Timeout for schedule update subprocess

# -- Discord / Message Limits --------------------------------------------------
MAX_ERROR_MESSAGE_CHARS = 500      # Truncation limit for error text sent to Discord
