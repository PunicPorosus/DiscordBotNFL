"""
Shared formatting for pick listings.

Lives in utils rather than in a cog because both the automatic lock summary
(cogs/locks.py) and the on-demand !check_reactions (cogs/results.py) render the
same thing. Importing one cog from another is what produced B2, where
server_management imported functions that had been deleted from admin and the
command died at "Step 1/4" on every invocation.
"""


def group_picks_by_player(picks: "dict[str, list[str]]") -> "dict[str, list[str]]":
    """Pivot {team: [user_name, ...]} into {user_name: [team, ...]}.

    get_picks_for_week returns picks keyed by team, which answers "who took the
    Bears". Players want the opposite: "what did I take". Teams come back sorted
    so a player's row is stable between calls.
    """
    by_player: "dict[str, list[str]]" = {}
    for team, users in picks.items():
        for user in users:
            by_player.setdefault(user, []).append(team)
    for user in by_player:
        by_player[user].sort()
    return by_player


def format_picks_by_player(
    picks: "dict[str, list[str]]",
    header: str,
    show_counts: bool = True,
) -> "list[str]":
    """Render picks grouped by player, one line each, header first.

    Returns a list of lines rather than a string so the caller can chunk it.
    A player holding every game of a week is 16 team codes on one line, so a
    busy server passes Discord's 2000-character message limit easily.
    """
    by_player = group_picks_by_player(picks)
    lines = [header]
    if not by_player:
        return lines

    for user in sorted(by_player, key=str.lower):
        teams = by_player[user]
        count = f"  ({len(teams)})" if show_counts else ""
        lines.append(f"**{user}**{count}: {', '.join(teams)}")
    return lines


async def send_chunked(send, lines: "list[str]", limit: int = 1900) -> None:
    """Send lines through `send`, splitting on Discord's message limit.

    `send` is an async callable taking one string, so callers can pass
    ctx.send directly or wrap rate_limiter.send for the automated paths.
    The first line is treated as a header and always sent alone when splitting,
    so every chunk after it is whole rows.
    """
    if not lines:
        return

    message = "\n".join(lines)
    if len(message) <= limit + 100:
        await send(message)
        return

    await send(lines[0])
    chunk: "list[str]" = []
    chunk_len = 0
    for line in lines[1:]:
        if chunk_len + len(line) + 1 > limit:
            await send("\n".join(chunk))
            chunk, chunk_len = [], 0
        chunk.append(line)
        chunk_len += len(line) + 1
    if chunk:
        await send("\n".join(chunk))
