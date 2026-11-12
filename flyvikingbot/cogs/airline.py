from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands

from ..constants import BRAND_COLOUR, EMOJI_FLYVIKING_V
from ..services.fsairlines import get_airline_stats
from ..services.http import ServiceError

if TYPE_CHECKING:
    from ..bot import FlyVikingBot

log = logging.getLogger(__name__)

# (field name, FSAirlines key, unit suffix)
STAT_FIELDS = (
    ("Flights", "flights", ""),
    ("Hours flown", "hours", ""),
    ("Distance Flown", "distance", "nm"),
    ("Fuel Used", "fuel_used", "kg"),
    ("Passengers Moved", "pax", ""),
    ("Generic Cargo Moved", "cargo_kg", "kg"),
    ("Packages Moved", "packages_kg", "kg"),
)


def _fmt(value: Any, suffix: str) -> str:
    try:
        return f"{int(float(value)):,}{suffix}"
    except (TypeError, ValueError):
        return "n/a"


def stats_embed(data: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(
        title=f"{EMOJI_FLYVIKING_V} Airline Statistics",
        description="Various airline-wide statistics",
        colour=BRAND_COLOUR,
    )
    for name, key, suffix in STAT_FIELDS:
        embed.add_field(name=name, value=_fmt(data.get(key), suffix), inline=True)
    return embed


class Airline(commands.Cog):
    def __init__(self, bot: FlyVikingBot) -> None:
        self.bot = bot

    @app_commands.command(name="airline_statistics", description="Gets various airline statistics from FSAirlines")
    async def airline_statistics(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        settings = self.bot.settings
        try:
            data = await get_airline_stats(self.bot.http_session, settings.fsa_va_id, settings.fsa_api_key)
        except ServiceError as exc:
            log.error("Could not retrieve data from FSAirlines: %s", exc)
            await interaction.delete_original_response()
            await interaction.followup.send(
                ":x: ERROR: Could not retrieve data from FSAirlines", ephemeral=True
            )
            return
        await interaction.followup.send(embed=stats_embed(data))


async def setup(bot: FlyVikingBot) -> None:
    await bot.add_cog(Airline(bot))
