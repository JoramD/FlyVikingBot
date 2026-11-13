"""Helpers for replying to slash commands."""

from __future__ import annotations

from typing import Any

import discord

# Ephemeral confirmations remove themselves after this many seconds; pass delete_after=None for errors
# so the user has time to read them
EPHEMERAL_SECONDS = 10.0


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
