"""Uptime Kuma push heartbeat."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiohttp
from discord.ext import commands, tasks

if TYPE_CHECKING:
    from ..bot import FlyVikingBot

log = logging.getLogger(__name__)


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
        try:
            async with self.bot.http_session.get(self.url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    log.debug("Uptime Kuma: pushed heartbeat")
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
