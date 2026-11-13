"""/file_lookup: find a file in the website's downloads."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..constants import BRAND_COLOUR
from ..services.http import ServiceError
from ..services.website import FileSearchResult
from ..webhooks import ips_events as ev
from .website_events import file_embed

if TYPE_CHECKING:
    from ..bot import FlyVikingBot

log = logging.getLogger(__name__)


def _link_text(text: str) -> str:
    """Text that is safe inside a Markdown link label."""
    return discord.utils.escape_markdown(text).replace("[", "(").replace("]", ")")


def matches_embed(query: str, result: FileSearchResult) -> discord.Embed:
    """The first few files of a search with several matches, each as a link."""
    lines = []
    for data in result.matches:
        if (file := ev.parse_downloads_file(data)) is None:
            continue
        parts = [f"[{_link_text(file.title or 'Untitled file')}]({file.url})"]
        if file.category:
            parts.append(file.category)
        if file.author:
            parts.append(f"by {discord.utils.escape_markdown(file.author.name)}")
        lines.append(" · ".join(parts))
    embed = discord.Embed(
        title=f'{result.total} files match "{query[:100]}"', description="\n".join(lines), colour=BRAND_COLOUR
    )
    if result.total > len(lines):
        embed.set_footer(text=f"Showing {len(lines)} of {result.total}; add words to narrow it down")
    return embed


class Website(commands.Cog):
    def __init__(self, bot: FlyVikingBot) -> None:
        self.bot = bot

    async def _error(self, interaction: discord.Interaction, message: str) -> None:
        # Replace the public "thinking..." placeholder with a private error (errors stay visible)
        await interaction.delete_original_response()
        await interaction.followup.send(f":x: ERROR: {message}", ephemeral=True)

    @app_commands.command(name="file_lookup", description="Find a file in the website's downloads")
    @app_commands.describe(query="Words from the file's title, e.g. A320 livery")
    async def file_lookup(self, interaction: discord.Interaction, query: str) -> None:
        assert self.bot.website is not None
        await interaction.response.defer(thinking=True)
        try:
            result = await self.bot.website.search_files(query)
        except ServiceError as exc:
            log.error("Website file search failed: %s", exc)
            await self._error(interaction, "Could not reach the website. Try again later.")
            return

        # The REST file object has the same shape as the webhook payload: reuse the "New file" post layout
        file = ev.parse_downloads_file(result.file) if result.file is not None else None
        if file is not None:
            await interaction.followup.send(embed=file_embed(file, new_version=False))
        elif result.matches:
            # A list to pick from, not an error; private so the channel only gets the file that's picked
            await interaction.delete_original_response()
            await interaction.followup.send(embed=matches_embed(query, result), ephemeral=True)
        elif result.total > 1:
            await self._error(interaction, f"{result.total} files match; try more specific words.")
        else:
            await self._error(interaction, "No files match.")


async def setup(bot: FlyVikingBot) -> None:
    await bot.add_cog(Website(bot))
