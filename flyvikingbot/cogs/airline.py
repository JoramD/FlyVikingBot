"""/airline_statistics: airline-wide FSAirlines statistics."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from ..constants import BRAND_COLOUR, EMOJI_FLYVIKING_V
from ..interactions import FSA_UNREACHABLE, reply_ephemeral
from ..services.http import ServiceError

if TYPE_CHECKING:
    from ..bot import FlyVikingBot

log = logging.getLogger(__name__)

# (field name, FSAirlines key, unit suffix)
STAT_FIELDS = (
    ("Flights", "flights", ""),
    ("Hours flown", "hours", ""),
    ("Distance flown", "distance", " nm"),
    ("Fuel used", "fuel_used", " kg"),
    ("Passengers moved", "pax", ""),
    ("Generic cargo moved", "cargo_kg", " kg"),
    ("Packages moved", "packages_kg", " kg"),
)


def _fmt(value: Any, suffix: str) -> str:
    try:
        return f"{round(float(value)):,}{suffix}"
    except (TypeError, ValueError):
        return "n/a"


def stats_embed(data: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=f"{EMOJI_FLYVIKING_V} Airline Statistics", colour=BRAND_COLOUR, timestamp=discord.utils.utcnow()
    )
    for name, key, suffix in STAT_FIELDS:
        embed.add_field(name=name, value=_fmt(data.get(key), suffix), inline=True)
    embed.set_footer(text="FSAirlines")
    return embed


class Airline(commands.Cog):
    def __init__(self, bot: FlyVikingBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="airline_statistics", description="Show FlyViking's airline-wide statistics from FSAirlines"
    )
    async def airline_statistics(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        try:
            data = await self.bot.fsa.airline_stats()
        except ServiceError as exc:
            log.error("Could not retrieve data from FSAirlines: %s", exc)
            await interaction.delete_original_response()
            await reply_ephemeral(interaction, FSA_UNREACHABLE, delete_after=None)
            return
        await interaction.followup.send(embed=stats_embed(data))


async def setup(bot: FlyVikingBot) -> None:
    await bot.add_cog(Airline(bot))
