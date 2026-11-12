from __future__ import annotations

import logging
from typing import Any

import aiohttp
import discord
from discord.ext import commands

from .config import Settings
from .services.website import WebsiteClient
from .webhooks.server import WebhookServer

log = logging.getLogger(__name__)

EXTENSIONS = (
    "flyvikingbot.cogs.website_events",
    "flyvikingbot.cogs.status",
    "flyvikingbot.cogs.airline",
    "flyvikingbot.cogs.website",
    "flyvikingbot.cogs.admin",
)


class FlyVikingBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,  # slash commands only; no message content needed
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        self.http_session: aiohttp.ClientSession | None = None
        self.website: WebsiteClient | None = None
        self.webhook_server = WebhookServer(
            secret=settings.webhook_secret,
            on_event=self._on_webhook,
            capture_dir=settings.data_dir / "webhook_samples" if settings.webhook_capture else None,
            capture_keep=settings.webhook_capture_keep,
        )

    def _on_webhook(self, event: str, payload: Any) -> None:
        # Listeners use `on_ips_webhook(event, payload)`
        self.dispatch("ips_webhook", event, payload)

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession(headers={"User-Agent": "FlyVikingBot"})
        self.website = WebsiteClient(self.http_session, self.settings.website_base_url, self.settings.website_api_key)

        for ext in EXTENSIONS:
            await self.load_extension(ext)

        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("Synced %d application commands", len(synced))

        await self.webhook_server.start(self.settings.webhook_host, self.settings.webhook_port)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))

    async def close(self) -> None:
        await self.webhook_server.stop()
        if self.http_session:
            await self.http_session.close()
        await super().close()
