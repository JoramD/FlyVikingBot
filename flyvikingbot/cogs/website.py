from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from ..constants import BRAND_COLOUR
from ..services.http import ServiceError

if TYPE_CHECKING:
    from ..bot import FlyVikingBot

log = logging.getLogger(__name__)


def file_embed(data: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(title=data.get("title"), url=data.get("url"), colour=BRAND_COLOUR)
    author = data.get("author") or {}
    if author.get("name"):
        icon = None if author.get("photoUrlIsDefault") else author.get("photoUrl")
        embed.set_author(name=author["name"], url=author.get("profileUrl"), icon_url=icon)
    screenshot = data.get("primaryScreenshot")
    if isinstance(screenshot, dict) and screenshot.get("url"):
        embed.set_image(url=screenshot["url"])
    return embed


class Website(commands.Cog):
    def __init__(self, bot: FlyVikingBot) -> None:
        self.bot = bot

    async def _error(self, interaction: discord.Interaction, message: str) -> None:
        # Replace the public "thinking..." placeholder with a private error
        await interaction.delete_original_response()
        await interaction.followup.send(f":x: ERROR: {message}", ephemeral=True)

    @app_commands.command(name="paint_lookup", description="Lookup aircraft paint on the website")
    @app_commands.describe(query="Search term")
    async def paint_lookup(self, interaction: discord.Interaction, query: str) -> None:
        assert self.bot.website is not None
        await interaction.response.defer(thinking=True)
        try:
            result = await self.bot.website.search_files(query)
        except ServiceError as exc:
            log.error("Could not retrieve data from website: %s", exc)
            await self._error(interaction, "Could not retrieve data from website")
            return

        if result.file is not None:
            await interaction.followup.send(embed=file_embed(result.file))
        elif result.total > 1:
            await self._error(interaction, "Too many matches")
        else:
            await self._error(interaction, "No matches")


async def setup(bot: FlyVikingBot) -> None:
    await bot.add_cog(Website(bot))
