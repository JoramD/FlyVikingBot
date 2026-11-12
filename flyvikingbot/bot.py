from __future__ import annotations

import logging
from typing import Any

import aiohttp
import discord
from discord.ext import commands

from .config import Settings
from .services.fsairlines import FsaClient
from .services.website import WebsiteClient
from .storage import Storage
from .webhooks.server import WebhookServer

log = logging.getLogger(__name__)

EXTENSIONS = (
    "flyvikingbot.cogs.website_events",
    "flyvikingbot.cogs.status",
    "flyvikingbot.cogs.airline",
    "flyvikingbot.cogs.website",
    "flyvikingbot.cogs.admin",
    "flyvikingbot.cogs.flight_logs",
    "flyvikingbot.cogs.pilots",
)


class FlyVikingBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.members = True  # privileged: find members by username + manage rank roles
        super().__init__(
            command_prefix=commands.when_mentioned,  # slash commands only; no message content needed
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        self.http_session: aiohttp.ClientSession | None = None
        self.website: WebsiteClient | None = None
        self.fsa: FsaClient | None = None
        self.storage = Storage(settings.data_dir / "flyviking.db")
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
        self.fsa = FsaClient(self.http_session, self.settings.fsa_va_id, self.settings.fsa_api_key)
        await self.storage.init()

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

    def primary_guild(self) -> discord.Guild | None:
        """The FlyViking server: GUILD_ID if set, otherwise the only guild the bot is in."""
        if self.settings.guild_id:
            return self.get_guild(self.settings.guild_id)
        return self.guilds[0] if len(self.guilds) == 1 else None

    async def post_system_message(
        self, content: str, *, action_required: bool, **kwargs: Any
    ) -> discord.Message | None:
        """Post to #system-messages (staff). Logs instead if the channel isn't configured.

        RULE: every post must say whether staff need to act. Posts that need action ping the
        staff role (SYSTEM_MESSAGES_ROLE_ID, @Board of Directors); purely informational posts
        don't. The argument is deliberately required so new features can't skip the decision.
        Only that role can be pinged; user mentions in the text never notify anyone.
        """
        channel_id = self.settings.channel_system_messages
        if not channel_id:
            log.warning("System message (CHANNEL_SYSTEM_MESSAGES not set): %s", content)
            return None
        role_id = self.settings.system_messages_role_id
        if action_required and role_id:
            content = f"<@&{role_id}> {content}"
            mentions = discord.AllowedMentions(everyone=False, users=False, roles=[discord.Object(id=role_id)])
        else:
            mentions = discord.AllowedMentions.none()
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            return await channel.send(content, allowed_mentions=mentions, **kwargs)
        except discord.HTTPException:
            log.exception("Failed to post system message: %s", content)
            return None

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))

    async def close(self) -> None:
        await self.webhook_server.stop()
        if self.http_session:
            await self.http_session.close()
        await super().close()
