"""/weather: METAR, TAF, ATIS and frequencies from SayIntentions.AI.

Only loaded when SAYINTENTIONS_APP_KEY and SAYINTENTIONS_ACCOUNT_KEY are set, so the command
doesn't appear otherwise."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..constants import BRAND_COLOUR
from ..interactions import reply_ephemeral
from ..services.http import ServiceError
from ..services.sayintentions import AirportWeather

if TYPE_CHECKING:
    from ..bot import FlyVikingBot

log = logging.getLogger(__name__)

MAX_AIRPORTS = 3
MAX_FREQUENCIES = 10
FIELD_LIMIT = 1024  # Discord's embed field value limit
_ICAO = re.compile(r"^[A-Z0-9]{4}$")
_SEPARATORS = re.compile(r"[\s,;]+")


class InvalidAirports(ValueError):
    """The `airports` option can't be used; the message is shown to the user."""


def parse_airports(text: str) -> list[str]:
    """'ekch, engm' -> ['EKCH', 'ENGM'] (duplicates removed, order kept). Raises InvalidAirports."""
    codes = list(dict.fromkeys(c for c in _SEPARATORS.split(text.strip().upper()) if c))
    if not codes:
        raise InvalidAirports("Give at least one ICAO code, e.g. EKCH.")
    if bad := next((c for c in codes if not _ICAO.match(c)), None):
        raise InvalidAirports(f'"{bad}" is not a valid ICAO code.')
    if len(codes) > MAX_AIRPORTS:
        raise InvalidAirports(f"Give at most {MAX_AIRPORTS} airports at a time.")
    return codes


def code_block(text: str, limit: int = FIELD_LIMIT) -> str:
    """Wrap text in a code block that fits an embed field, cutting it with '…' if needed."""
    text = text.replace("```", "'''")
    room = limit - len("```\n\n```")
    if len(text) > room:
        text = text[: room - 1].rstrip() + "…"
    return f"```\n{text}\n```"


def frequency_lines(weather: AirportWeather) -> str | None:
    lines = []
    for f in weather.frequencies[:MAX_FREQUENCIES]:
        line = f"{f.kind} {f.freq}" if f.kind else f.freq
        lines.append(f"{line} ({f.callsign})" if f.callsign else line)
    text = "\n".join(lines)
    return text[:FIELD_LIMIT] or None


def weather_embed(weather: AirportWeather, frequencies: bool = False) -> discord.Embed:
    embed = discord.Embed(title=weather.icao, colour=BRAND_COLOUR, timestamp=discord.utils.utcnow())
    if weather.metar:
        embed.add_field(name="METAR", value=code_block(weather.metar), inline=False)
    if weather.taf:
        embed.add_field(name="TAF", value=code_block(weather.taf), inline=False)
    if weather.active_runway:
        embed.add_field(name="Active runway", value=weather.active_runway, inline=True)
    if weather.wind:
        embed.add_field(name="Wind", value=weather.wind, inline=True)
    if weather.atis:
        embed.add_field(name="ATIS", value=code_block(weather.atis), inline=False)
    if frequencies and (lines := frequency_lines(weather)):
        embed.add_field(name="Frequencies", value=lines, inline=False)
    embed.set_footer(text="SayIntentions")
    return embed


def missing_note(missing: list[str]) -> str:
    return f"No weather found for {', '.join(missing)}."


class Weather(commands.Cog):
    def __init__(self, bot: FlyVikingBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="weather", description="Current METAR, TAF and ATIS for up to 3 airports (from SayIntentions)"
    )
    @app_commands.describe(
        airports="One to three ICAO codes, e.g. EKCH ENGM",
        frequencies="Also list the airport frequencies",
    )
    async def weather(self, interaction: discord.Interaction, airports: str, frequencies: bool = False) -> None:
        try:
            icaos = parse_airports(airports)
        except InvalidAirports as exc:
            await reply_ephemeral(interaction, f":x: ERROR: {exc}", delete_after=None)
            return
        await interaction.response.defer(thinking=True)
        try:
            found = await self.bot.sayintentions.weather(icaos)
        except ServiceError as exc:
            log.error("Could not retrieve weather from SayIntentions: %s", exc)
            await interaction.delete_original_response()
            await reply_ephemeral(
                interaction,
                ":x: ERROR: Could not get weather from SayIntentions. Check the ICAO codes or try again later.",
                delete_after=None,
            )
            return
        missing = [i for i in icaos if i not in found]
        if not found:
            await interaction.delete_original_response()
            await reply_ephemeral(interaction, f":x: ERROR: {missing_note(missing)}", delete_after=None)
            return
        embeds = [weather_embed(found[i], frequencies) for i in icaos if i in found]
        await interaction.followup.send(f"-# {missing_note(missing)}" if missing else None, embeds=embeds)


async def setup(bot: FlyVikingBot) -> None:
    if bot.sayintentions is None:
        log.info("/weather is off: SAYINTENTIONS_APP_KEY and SAYINTENTIONS_ACCOUNT_KEY are not both set")
        return
    await bot.add_cog(Weather(bot))
