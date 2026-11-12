"""Staff utilities: /clear (administrators only)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from ..bot import FlyVikingBot


class Admin(commands.Cog):
    """Administrator-only commands (Discord hides them from everyone else)."""
    def __init__(self, bot: FlyVikingBot) -> None:
        self.bot = bot

    @app_commands.command(name="clear", description="Clears multiple messages")
    @app_commands.describe(amount="Amount of messages to clear")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 50]) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            await interaction.followup.send(":x: ERROR: Can't clear messages here.", ephemeral=True)
            return
        try:
            deleted = await channel.purge(limit=amount, reason=f"/clear by {interaction.user}")
        except discord.Forbidden:
            await interaction.followup.send(":x: ERROR: I lack permission to delete messages here.", ephemeral=True)
            return
        await interaction.followup.send(f":white_check_mark: Deleted {len(deleted)} messages.", ephemeral=True)


async def setup(bot: FlyVikingBot) -> None:
    await bot.add_cog(Admin(bot))
