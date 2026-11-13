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


# SayIntentions frequency types, in the order a pilot uses them; unknown types follow, then "Other"
FREQUENCY_TYPES = {
    "ATIS": "ATIS", "CLR": "Delivery", "DEL": "Delivery", "GND": "Ground", "RMP": "Apron", "TWR": "Tower",
    "DEP": "Departure", "APP": "Approach", "CTR": "Centre",
}
OTHER_FREQUENCIES = "Other"  # "AIR" (de-icing, airside) and anything without a type


def _mhz(freq: str) -> str:
    """'118.58' -> '118.580'."""
    try:
        return f"{float(freq):.3f}"
    except ValueError:
        return freq


def _callsign(text: str) -> str:
    """'KASTRUP FINAL' -> 'Kastrup Final', 'DE-ICING' -> 'De-icing'."""
    return " ".join(word.capitalize() for word in text.split())


def runways_text(weather: AirportWeather) -> tuple[str, str] | None:
    """(field name, value) for the runways in use, or None."""
    arriving, departing = weather.runways_arriving, weather.runways_departing
    if arriving and arriving == departing:
        return "Runway", arriving
    if not (arriving or departing):
        return None
    parts = [f"Arriving {arriving}" if arriving else "", f"Departing {departing}" if departing else ""]
    return "Runways", " · ".join(p for p in parts if p)


def _frequency_label(kind: str | None) -> str:
    kind = (kind or "").upper()
    return FREQUENCY_TYPES.get(kind) or (kind if kind and kind != "AIR" else OTHER_FREQUENCIES)


def frequency_lines(weather: AirportWeather) -> str | None:
    """One line per frequency type, e.g. '**Tower** 118.105 · 118.580 (Kastrup)', within one embed field."""
    groups: dict[str, dict[str | None, list[str]]] = {}
    for f in weather.frequencies:
        callsign = _callsign(f.callsign) if f.callsign else None
        groups.setdefault(_frequency_label(f.kind), {}).setdefault(callsign, []).append(_mhz(f.freq))
    order = list(dict.fromkeys(FREQUENCY_TYPES.values()))

    def rank(label: str) -> tuple[bool, int]:
        return label == OTHER_FREQUENCIES, order.index(label) if label in order else len(order)

    lines: list[str] = []
    for label in sorted(groups, key=rank):
        parts = [" · ".join(freqs) + (f" ({name})" if name else "") for name, freqs in groups[label].items()]
        line = f"**{label}** " + " · ".join(parts)
        if len("\n".join([*lines, line, "…"])) > FIELD_LIMIT:
            lines.append("…")
            break
        lines.append(line)
    return "\n".join(lines) or None


def weather_embed(weather: AirportWeather, frequencies: bool = False) -> discord.Embed:
    embed = discord.Embed(title=weather.icao, colour=BRAND_COLOUR, timestamp=discord.utils.utcnow())
    if weather.metar:
        embed.add_field(name="METAR", value=code_block(weather.metar), inline=False)
    if weather.taf:
        embed.add_field(name="TAF", value=code_block(weather.taf), inline=False)
    if runways := runways_text(weather):
        embed.add_field(name=runways[0], value=runways[1], inline=True)
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
