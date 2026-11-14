"""Uptime Kuma push heartbeat.

Every minute the bot reports to its Kuma push monitor:
- up, with the Discord response time as `ping` (Kuma charts it) and "OK" as the message;
- up, with a message naming outside services that can't be reached (the bot itself still works);
- down, while disconnected from Discord, so Kuma alerts straight away.
If the bot isn't running at all, the pushes stop and Kuma marks it down after its heartbeat interval.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import aiohttp
from discord.ext import commands, tasks
from yarl import URL

if TYPE_CHECKING:
    from ..bot import FlyVikingBot

log = logging.getLogger(__name__)


def push_params(bot: Any) -> dict[str, str]:
    """The status, msg and ping parameters for one Uptime Kuma push."""
    if not bot.discord_connected:
        return {"status": "down", "msg": "Disconnected from Discord", "ping": ""}
    problems = [
        f"{h.name} unreachable since {datetime.fromtimestamp(h.failing_since, UTC):%H:%M} UTC"
        for h in bot.service_health()
        if h.failing
    ]
    latency = bot.latency_ms()
    return {"status": "up", "msg": "; ".join(problems) or "OK", "ping": "" if latency is None else str(latency)}


def push_url(base: str, params: dict[str, str]) -> URL:
    """The push URL with our parameters. Kuma's copied URL already carries ?status=up&msg=OK&ping=,
    so those are replaced rather than added twice."""
    return URL(base).update_query(params)


class Status(commands.Cog):
    def __init__(self, bot: FlyVikingBot, url: str) -> None:
        self.bot = bot
        self.url = url
        self.heartbeat.start()

    async def cog_unload(self) -> None:
        self.heartbeat.cancel()

    @tasks.loop(seconds=60)
    async def heartbeat(self) -> None:
        assert self.bot.http_session is not None
        params = push_params(self.bot)
        started = time.monotonic()
        try:
            async with self.bot.http_session.get(
                push_url(self.url, params), timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    log.debug("Uptime Kuma: pushed %s (%s) in %.0f ms", params["status"], params["msg"],
                              (time.monotonic() - started) * 1000)
                else:
                    log.warning("Uptime Kuma: push returned HTTP %s", resp.status)
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.warning("Uptime Kuma: push failed (%s)", type(exc).__name__)

    @heartbeat.before_loop
    async def before_heartbeat(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: FlyVikingBot) -> None:
    if not bot.settings.uptime_kuma_url:
        log.info("UPTIME_KUMA_URL not set; heartbeat disabled")
        return
    await bot.add_cog(Status(bot, bot.settings.uptime_kuma_url))
