"""Staff utilities: /clear (administrators only)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..interactions import reply_ephemeral

if TYPE_CHECKING:
    from ..bot import FlyVikingBot


class Admin(commands.Cog):
    """Administrator-only commands (Discord hides them from everyone else)."""
    def __init__(self, bot: FlyVikingBot) -> None:
        self.bot = bot

    @app_commands.command(name="clear", description="Delete the most recent messages in this channel")
    @app_commands.describe(amount="How many messages to delete (1-50)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 50]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            await reply_ephemeral(interaction, ":x: ERROR: Can't clear messages here.", delete_after=None)
            return
        try:
            deleted = await channel.purge(limit=amount, reason=f"/clear by {interaction.user}")
        except discord.Forbidden:
            await reply_ephemeral(
                interaction, ":x: ERROR: I lack permission to delete messages here.", delete_after=None
            )
            return
        # Only the confirmation disappears; errors stay so they can be read
        await reply_ephemeral(interaction, f":white_check_mark: Deleted {len(deleted)} messages.")


async def setup(bot: FlyVikingBot) -> None:
    await bot.add_cog(Admin(bot))
