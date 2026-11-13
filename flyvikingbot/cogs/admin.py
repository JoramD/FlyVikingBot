"""Staff utilities: /clear (staff only)."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..interactions import reply_ephemeral
from ..system_messages import staff_only

if TYPE_CHECKING:
    from ..bot import FlyVikingBot

TIME_ONLY_LIMIT = 200  # messages deleted at most when only `minutes` is given, so a busy channel isn't wiped


def _duration(minutes: int) -> str:
    """'30 minutes', '1 minute', '2 h', '1 h 30 min'."""
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def clear_summary(count: int, minutes: int | None) -> str:
    """The confirmation text, e.g. 'Deleted 14 messages from the last 30 minutes.'"""
    period = f" from the last {_duration(minutes)}" if minutes else ""
    if count == 0:
        return f"No messages to delete{period}."
    return f"Deleted {count} message{'s' if count != 1 else ''}{period}."


class Admin(commands.Cog):
    """Staff-only commands (Discord hides them from everyone else, and the bot checks again)."""
    def __init__(self, bot: FlyVikingBot) -> None:
        self.bot = bot

    @app_commands.command(name="clear", description="Delete the most recent messages in this channel")
    @app_commands.describe(
        amount="How many messages to delete (1-50)",
        minutes="Only delete messages from the last N minutes (1-1440)",
    )
    @staff_only()
    async def clear(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 50] | None = None,
        minutes: app_commands.Range[int, 1, 1440] | None = None,
    ) -> None:
        if amount is None and minutes is None:
            await reply_ephemeral(interaction, ":x: ERROR: Give an amount, minutes, or both.", delete_after=None)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            await reply_ephemeral(interaction, ":x: ERROR: Can't clear messages here.", delete_after=None)
            return
        after = discord.utils.utcnow() - timedelta(minutes=minutes) if minutes else None
        try:
            deleted = await channel.purge(
                limit=amount or TIME_ONLY_LIMIT, after=after, reason=f"/clear by {interaction.user}"
            )
        except discord.Forbidden:
            await reply_ephemeral(
                interaction, ":x: ERROR: I lack permission to delete messages here.", delete_after=None
            )
            return
        # Only the confirmation disappears; errors stay so they can be read
        await reply_ephemeral(interaction, f":white_check_mark: {clear_summary(len(deleted), minutes)}")


async def setup(bot: FlyVikingBot) -> None:
    await bot.add_cog(Admin(bot))
