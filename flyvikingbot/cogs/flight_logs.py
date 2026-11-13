"""Posts flights completed in FSAirlines to the flight log channel."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands, tasks

from ..constants import BRAND_COLOUR
from ..services.fsairlines import FlightLog
from ..services.http import ServiceError

if TYPE_CHECKING:
    from ..bot import FlyVikingBot

log = logging.getLogger(__name__)

FETCH_COUNT = 15  # most recent flights requested per poll
MAX_POSTS_PER_CYCLE = 10  # keeps API usage bounded after downtime
LAST_ID_KEY = "fsa_last_flight_id"


def format_hours(hours: float | None) -> str | None:
    if hours is None:
        return None
    total_minutes = round(hours * 60)
    return f"{total_minutes // 60}:{total_minutes % 60:02d}"


def flight_title(flight: FlightLog) -> str:
    route = f"{flight.dep or '????'} → {flight.arr or '????'}"
    number = flight.number
    if number and number.upper() == "CHARTER":
        number = "Charter"
    return f"{number} · {route}" if number else route


def flight_log_embed(flight: FlightLog, pilot_mention: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=flight_title(flight), colour=BRAND_COLOUR, timestamp=flight.completed_at)

    pilot = pilot_mention or flight.pilot_name or (f"Pilot {flight.pilot_id}" if flight.pilot_id else None)
    if pilot and flight.callsign:
        pilot = f"{pilot} ({flight.callsign})"
    fields: list[tuple[str, str | None]] = [
        ("Pilot", pilot),
        ("Aircraft", flight.aircraft),
        ("Block time", format_hours(flight.hours)),
        ("Distance", f"{flight.distance:,.0f} nm" if flight.distance is not None else None),
        ("Landing rate", f"{flight.landing_rate:+d} fpm" if flight.landing_rate is not None else None),
        ("Rating", f"{flight.rating:g}%" if flight.rating is not None else None),
        ("Passengers", f"{flight.pax:,}" if flight.pax else None),
        ("Cargo", f"{flight.cargo_kg:,} kg" if flight.cargo_kg else None),
        ("Packages", f"{flight.packages_kg:,} kg" if flight.packages_kg else None),
    ]
    for name, value in fields:
        if value:
            embed.add_field(name=name, value=value, inline=True)
    if flight.planned_arr and flight.arr and flight.planned_arr != flight.arr:
        embed.description = f"Diverted, planned arrival was {flight.planned_arr}"
    embed.set_footer(text=" · ".join(p for p in ("Completed flight", flight.sim) if p))
    return embed


def flight_log_message(flight: FlightLog, pilot_mention: str | None = None) -> dict[str, Any]:
    """The flight log post (channel.send() kwargs; see the RULE in tools/preview_posts.py).
    The pilot's mention is shown but never pings: it's in an embed, and mentions are off."""
    return {"embed": flight_log_embed(flight, pilot_mention), "allowed_mentions": discord.AllowedMentions.none()}


class FlightLogs(commands.Cog):
    def __init__(self, bot: FlyVikingBot, channel_id: int) -> None:
        self.bot = bot
        self.channel_id = channel_id
        self.poll.change_interval(seconds=bot.settings.fsa_poll_seconds)
        self.poll.start()

    async def cog_unload(self) -> None:
        self.poll.cancel()

    @tasks.loop(seconds=120)
    async def poll(self) -> None:
        try:
            await self.check_new_flights()
        except ServiceError as exc:
            log.warning("Flight log poll failed, retrying next cycle: %s", exc)
        except Exception:
            log.exception("Unexpected error in flight log poll")

    @poll.before_loop
    async def before_poll(self) -> None:
        await self.bot.wait_until_ready()

    async def check_new_flights(self) -> int:
        """Post flights newer than the last posted one. Returns how many were posted."""
        storage, fsa = self.bot.storage, self.bot.fsa
        flights = await fsa.flight_logs(count=FETCH_COUNT)
        if not flights:
            return 0

        last_raw = await storage.kv_get(LAST_ID_KEY)
        if last_raw is None:
            # First run: start from the newest flight instead of posting the backlog
            await storage.kv_set(LAST_ID_KEY, str(flights[-1].id))
            log.info("Flight log feed initialised at flight %s", flights[-1].id)
            return 0

        last_id = int(last_raw)
        new = [f for f in flights if f.id > last_id][:MAX_POSTS_PER_CYCLE]
        if not new:
            return 0

        channel = self.bot.get_channel(self.channel_id) or await self.bot.fetch_channel(self.channel_id)
        posted = 0
        for flight in new:
            try:
                if detail := await fsa.flight_detail(flight.id):
                    flight = flight.merged(detail)
            except ServiceError as exc:
                log.info("No detail for flight %s (%s); posting summary", flight.id, exc)

            mention = None
            if flight.pilot_id and (link := await storage.link_by_external("fsa", flight.pilot_id)):
                mention = f"<@{link.discord_id}>"

            # Raises on failure, so the stored ID isn't advanced and the flight is retried next cycle
            await channel.send(**flight_log_message(flight, mention))
            await storage.kv_set(LAST_ID_KEY, str(flight.id))
            posted += 1
        return posted


async def setup(bot: FlyVikingBot) -> None:
    if not bot.settings.channel_flight_logs:
        log.info("CHANNEL_FLIGHT_LOGS not set; flight log feed disabled")
        return
    await bot.add_cog(FlightLogs(bot, bot.settings.channel_flight_logs))
