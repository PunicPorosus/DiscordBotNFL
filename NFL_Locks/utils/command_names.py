"""
Central command name registry.

All Discord command names are defined here as string constants.
To rename a command, change it in this file only — no need to touch the cog.

Import pattern in cogs:
    from NFL_Locks.utils.command_names import CMD_FIX_PICK, CMD_SHOW_PICKS

Usage in decorator:
    @commands.command(name=CMD_FIX_PICK)
    async def fix_pick(self, ctx, ...):
        ...

Note: the function name (e.g. fix_pick) no longer matters for what users type —
only the name= value does. Keep function names matching constants for readability.
"""

# -- admin.py ------------------------------------------------------------------
CMD_SET_CHANNEL             = "set_channel"
CMD_CHECK_CHANNEL           = "check_channel"
CMD_LIST_SERVERS            = "list_servers"
CMD_FIX_PICK                = "fix_pick"
CMD_SHOW_PICKS              = "show_picks"
CMD_BOT_STATUS              = "botstatus"
CMD_SET_SCORING_SCHEME      = "set_scoring_scheme"
CMD_CHECK_SCORING_SCHEME    = "check_scoring_scheme"

# -- cache.py ------------------------------------------------------------------
CMD_REBUILD_CACHE           = "rebuild_cache"
CMD_CACHE_STATS             = "cache_stats"

# -- games.py ------------------------------------------------------------------
CMD_GAMES                   = "games"
CMD_FETCH_WINNERS_LEGACY    = "fetch_winners_legacy"
CMD_TEST_API                = "test_api"

# -- games_manager.py ----------------------------------------------------------
CMD_POST_GAMES              = "post_games"
CMD_REPOST_GAMES            = "repost_games"

# -- reaction_catchup.py -------------------------------------------------------
CMD_FORCE_REACTION_CATCHUP  = "force_reaction_catchup"

# -- reactions.py --------------------------------------------------------------
CMD_UPDATE_REACTIONS        = "update_reactions"
CMD_PROCESS_EXISTING        = "process_existing_reactions"

# -- results.py ----------------------------------------------------------------
CMD_SET_WINNERS             = "set_winners"
CMD_TALLY_SCORES            = "tally_scores"
CMD_WEEKLY_RESULTS          = "weekly_results"
CMD_SEASON_STANDINGS        = "season_standings"
CMD_GLOBAL_STANDINGS        = "global_standings"
CMD_CHECK_REACTIONS         = "check_reactions"

# -- results_manager.py --------------------------------------------------------
CMD_POST_RESULTS_MANUAL     = "post_results_manual"
CMD_SHOW_RESULTS            = "show_results"

# -- schedule_update.py --------------------------------------------------------
CMD_UPDATE_SCHEDULE_NOW     = "update_schedule_now"
CMD_CHECK_SCHEDULE_STATUS   = "check_schedule_status"

# -- season_management.py ------------------------------------------------------
CMD_REBUILD_LEADERBOARD     = "rebuild_leaderboard"
CMD_CURRENT_SEASON          = "current_season"
CMD_ARCHIVE_SEASON          = "archive_season"
CMD_LIST_SEASONS            = "list_seasons"

# -- season_wrapup.py ----------------------------------------------------------
CMD_POST_WRAPUP             = "post_wrapup"

# -- server_management.py ------------------------------------------------------
CMD_START_LOCKS             = "start_locks"
CMD_END_LOCKS               = "end_locks"
CMD_SERVER_STATUS           = "server_status"

# -- startup_coordinator.py ----------------------------------------------------
CMD_STARTUP_STATUS          = "startup_status"
CMD_RERUN_STARTUP           = "rerun_startup"
CMD_FORCE_RECONNECT_CATCHUP = "force_reconnect_catchup"

# -- status_migration.py -------------------------------------------------------
CMD_BUILD_STATUS            = "build_status"
CMD_STATUS_INFO             = "status_info"
CMD_CLEAR_PENDING_WORK      = "clear_pending_work"

# -- user_points.py ------------------------------------------------------------
CMD_MYPOINTS                = "mypoints"
CMD_RESET_COOLDOWNS         = "reset_cooldowns"

# -- winners.py ----------------------------------------------------------------
CMD_FETCH_WINNERS           = "fetch_winners"
CMD_SHOW_WINNERS            = "show_winners"

# -- survivor.py ---------------------------------------------------------------
CMD_SURVIVOR_SETUP          = "survivor_setup"
CMD_SURVIVOR_START          = "survivor_start"
CMD_SURVIVOR_STANDINGS      = "survivor_standings"
CMD_SURVIVOR_MYPICKS        = "survivor_mypicks"
CMD_SURVIVOR_FIX_PICK       = "survivor_fix_pick"
CMD_SURVIVOR_ELIMINATE      = "survivor_eliminate"
CMD_SURVIVOR_PROCESS        = "survivor_process"
CMD_SURVIVOR_STATUS         = "survivor_status"
