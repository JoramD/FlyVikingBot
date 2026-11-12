"""Turns Invision Community webhook events into Discord posts."""

from __future__ import annotations

import io
import logging
import posixpath
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import aiohttp
import discord
from discord.ext import commands

from ..constants import BRAND_COLOUR
from ..webhooks import ips_events as ev

if TYPE_CHECKING:
    from ..bot import FlyVikingBot

log = logging.getLogger(__name__)

# Discord's upload limit for bots in non-boosted servers is 10 MiB; leave headroom
MAX_UPLOAD_BYTES = 9 * 1024 * 1024


def _filename(url: str) -> str:
    """IPS names files like 'large.Shot.png.<hash>.png'; keep the last path segment."""
    name = posixpath.basename(urlparse(url).path) or "image.png"
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[-100:]


def announcement_embed(entry: ev.BlogEntry) -> discord.Embed:
    embed = discord.Embed(title=entry.title, url=entry.url, colour=BRAND_COLOUR)
    if entry.image_url:
        embed.set_thumbnail(url=entry.image_url)
    return embed


class WebsiteEvents(commands.Cog):
    def __init__(self, bot: FlyVikingBot) -> None:
        self.bot = bot
        self.settings = bot.settings

    async def _channel(self, channel_id: int) -> discord.abc.Messageable:
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        return channel  # type: ignore[return-value]

    async def _fetch_image(self, url: str) -> bytes | None:
        """Download an image for re-upload. Returns None if unavailable or too large."""
        session = self.bot.http_session
        if session is None:
            return None
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    log.warning("Image download returned HTTP %s: %s", resp.status, url)
                    return None
                if (resp.content_length or 0) > MAX_UPLOAD_BYTES:
                    log.info("Image too large to upload (%s bytes), posting link: %s", resp.content_length, url)
                    return None
                data = await resp.content.read(MAX_UPLOAD_BYTES + 1)
                if len(data) > MAX_UPLOAD_BYTES:
                    log.info("Image too large to upload, posting link: %s", url)
                    return None
                return data
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.warning("Image download failed (%s): %s", type(exc).__name__, url)
            return None

    @commands.Cog.listener()
    async def on_ips_webhook(self, event: str, payload: Any) -> None:
        try:
            await self.handle(event, payload)
        except discord.HTTPException:
            log.exception("Discord rejected the post for webhook %s", event)
        except Exception:
            log.exception("Error handling webhook %s", event)

    async def handle(self, event: str, payload: Any) -> None:
        if event == ev.GALLERY_IMAGE_CREATE:
            if image := ev.parse_gallery_image(payload):
                channel = await self._channel(self.settings.channel_screenshots)
                # Upload the image ourselves: Discord can't always unfurl flyviking.net links
                data = await self._fetch_image(image.image_url)
                if data:
                    await channel.send(file=discord.File(io.BytesIO(data), filename=_filename(image.image_url)))
                else:
                    await channel.send(image.image_url)

        elif event == ev.BLOG_ENTRY_CREATE:
            entry = ev.parse_blog_entry(payload)
            if not entry:
                return
            wanted = self.settings.announcement_blog_id
            if wanted is not None and entry.blog_id != wanted:
                log.info("Ignoring blog entry %r from blog %s", entry.title, entry.blog_id)
                return
            channel = await self._channel(self.settings.channel_announcements)
            await channel.send(
                "@everyone",
                embed=announcement_embed(entry),
                allowed_mentions=discord.AllowedMentions(everyone=True, users=False, roles=False),
            )

        elif event == ev.DOWNLOADS_FILE_CREATE:
            if file := ev.parse_downloads_file(payload, event):
                channel = await self._channel(self.settings.channel_announcements)
                await channel.send(f"## [New file available!]({file.url})")

        elif event == ev.DOWNLOADS_NEW_VERSION:
            if file := ev.parse_downloads_file(payload, event):
                channel = await self._channel(self.settings.channel_announcements)
                await channel.send(f"## [New file version available!]({file.url})")

        else:
            log.info("Ignoring unhandled webhook event %s", event)


async def setup(bot: FlyVikingBot) -> None:
    await bot.add_cog(WebsiteEvents(bot))
