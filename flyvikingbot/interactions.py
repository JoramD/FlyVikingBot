"""Helpers for replying to slash commands."""

from __future__ import annotations

from typing import Any

import discord

MESSAGE_LIMIT = 2000  # Discord's maximum message length

# Ephemeral confirmations remove themselves after this many seconds; pass delete_after=None for errors
# so the user has time to read them
EPHEMERAL_SECONDS = 10.0

# Shared error texts, so every command words the same failure the same way
FSA_UNREACHABLE = ":x: ERROR: Could not reach FSAirlines. Try again later."


async def reply_ephemeral(
    interaction: discord.Interaction, content: str | None = None, *, delete_after: float | None = EPHEMERAL_SECONDS,
    **kwargs: Any,
) -> None:
    """Reply only the user can see, removed after `delete_after` seconds (None keeps it).
    Works both before and after the interaction was deferred."""
    if not interaction.response.is_done():
        await interaction.response.send_message(content, ephemeral=True, delete_after=delete_after, **kwargs)
        return
    message = await interaction.followup.send(content, ephemeral=True, wait=True, **kwargs)
    if delete_after is not None:
        await message.delete(delay=delete_after)


def fit_lines(lines: list[str], footer: str = "", limit: int = MESSAGE_LIMIT) -> str:
    """Join lines into one message under Discord's length limit, ending with "...and N more" if they don't fit."""
    tail = f"\n{footer}" if footer else ""
    kept: list[str] = []
    for i, line in enumerate(lines):
        more = f"\n...and {len(lines) - i} more (see the bot log)"
        if len("\n".join([*kept, line])) + len(more) + len(tail) > limit:
            return "\n".join(kept) + more + tail
        kept.append(line)
    return "\n".join(kept) + tail
