"""Turns Invision Community webhook events into Discord posts."""

from __future__ import annotations

import io
import logging
import posixpath
from datetime import datetime
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
SITE_NAME = "flyviking.net"
NO_MENTIONS = discord.AllowedMentions.none()


def _filename(url: str) -> str:
    """IPS names files like 'large.Shot.png.<hash>.png'; keep the last path segment."""
    name = posixpath.basename(urlparse(url).path) or "image.png"
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[-100:]


def website_embed(
    *,
    title: str,
    url: str,
    author: ev.Author | None = None,
    description: str | None = None,
    image_url: str | None = None,
    category: str | None = None,
    date: datetime | None = None,
) -> discord.Embed:
    """The shared layout for website posts: author, linked title, excerpt, large image, category footer."""
    embed = discord.Embed(
        title=title[:256], url=url, description=description, colour=BRAND_COLOUR, timestamp=date
    )
    if author:
        embed.set_author(name=author.name[:256], url=author.profile_url, icon_url=author.avatar_url)
    if image_url:
        embed.set_image(url=image_url)
    embed.set_footer(text=" · ".join(filter(None, [category, SITE_NAME])))
    return embed


def announcement_embed(entry: ev.BlogEntry) -> discord.Embed:
    # No author: announcements speak for FlyViking, not for whoever wrote them
    return website_embed(
        title=entry.title,
        url=entry.url,
        description=entry.excerpt,
        image_url=entry.image_url,
        category=entry.category,
        date=entry.date,
    )


def file_embed(file: ev.DownloadsFile, *, new_version: bool) -> discord.Embed:
    embed = website_embed(
        title=file.title or "Untitled file",
        url=file.url,
        author=file.author,
        description=file.excerpt,
        image_url=file.image_url,
        category=file.category,
        date=file.date,
    )
    if file.version:
        embed.add_field(name="Version", value=file.version[:1024])
    if new_version and file.changelog:
        embed.add_field(name="Changes", value=file.changelog[:1024], inline=False)
    return embed


def author_line(author: ev.Author | None) -> str | None:
    """'Uploaded by <name>' with the profile linked. The <> stops Discord previewing the link, so the
    uploaded image stays full size."""
    if author is None:
        return None
    name = discord.utils.escape_markdown(author.name)
    return f"Uploaded by [{name}](<{author.profile_url}>)" if author.profile_url else f"Uploaded by {name}"


# --- complete messages ------------------------------------------------------------------------------------------------
# Each *_message function returns the keyword arguments for channel.send(). RULE: every kind of automatic post
# has one, and it is listed in tools/preview_posts.py (a test enforces this).


def announcement_message(entry: ev.BlogEntry) -> dict[str, Any]:
    return {
        "content": "@everyone",
        "embed": announcement_embed(entry),
        "allowed_mentions": discord.AllowedMentions(everyone=True, users=False, roles=False),
    }


def file_message(file: ev.DownloadsFile, *, new_version: bool) -> dict[str, Any]:
    return {
        "content": "New version available" if new_version else "New file available",
        "embed": file_embed(file, new_version=new_version),
        "allowed_mentions": NO_MENTIONS,
    }


def gallery_message(image: ev.GalleryImage, data: bytes | None) -> dict[str, Any]:
    """The uploaded image with an author line, or the image link if it couldn't be downloaded."""
    line = author_line(image.author)
    if data:
        file = discord.File(io.BytesIO(data), filename=_filename(image.image_url))
        return {"content": line, "file": file, "allowed_mentions": NO_MENTIONS}
    return {"content": f"{line}\n{image.image_url}" if line else image.image_url, "allowed_mentions": NO_MENTIONS}


class WebsiteEvents(commands.Cog):
    def __init__(self, bot: FlyVikingBot) -> None:
        self.bot = bot
        self.settings = bot.settings

    async def _channel(self, channel_id: int | None, setting: str) -> discord.abc.Messageable | None:
        """The target channel, or None (logged) if its setting is empty, which turns that post type off."""
        if not channel_id:
            log.info("%s not set; not posting this website event", setting)
            return None
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        return channel  # type: ignore[return-value]

    def _is_website_url(self, url: str) -> bool:
        """Only download from our own website. A webhook can name any URL, and fetching arbitrary URLs
        would let anyone holding the webhook secret make the bot read internal addresses (SSRF)."""
        target, site = urlparse(url), urlparse(self.settings.website_root)
        return target.scheme in ("http", "https") and target.hostname == site.hostname

    async def _fetch_image(self, url: str) -> bytes | None:
        """Download an image for re-upload. Returns None if not on our website, unavailable or too large."""
        session = self.bot.http_session
        if session is None:
            return None
        if not self._is_website_url(url):
            log.warning("Not downloading image from outside the website: %s", url)
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
        await self.bot.wait_until_ready()  # webhooks can arrive while the bot is still connecting
        try:
            await self.handle(event, payload)
        except discord.HTTPException:
            log.exception("Discord rejected the post for webhook %s", event)
        except Exception:
            log.exception("Error handling webhook %s", event)

    async def handle(self, event: str, payload: Any) -> None:
        if event == ev.GALLERY_IMAGE_CREATE:
            image = ev.parse_gallery_image(payload)
            channel = await self._channel(self.settings.channel_screenshots, "CHANNEL_SCREENSHOTS") if image else None
            if image and channel:
                # Upload the image ourselves: Discord can't always unfurl flyviking.net links
                await channel.send(**gallery_message(image, await self._fetch_image(image.image_url)))

        elif event == ev.BLOG_ENTRY_CREATE:
            entry = ev.parse_blog_entry(payload)
            if not entry:
                return
            wanted = self.settings.announcement_blog_id
            if wanted is not None and entry.blog_id != wanted:
                log.info("Ignoring blog entry %r from blog %s", entry.title, entry.blog_id)
                return
            channel = await self._channel(self.settings.channel_announcements, "CHANNEL_ANNOUNCEMENTS")
            if channel is None:
                return
            await channel.send(**announcement_message(entry))

        elif event in (ev.DOWNLOADS_FILE_CREATE, ev.DOWNLOADS_NEW_VERSION):
            new_version = event == ev.DOWNLOADS_NEW_VERSION
            if (file := ev.parse_downloads_file(payload, event)) and (
                channel := await self._channel(self.settings.channel_announcements, "CHANNEL_ANNOUNCEMENTS")
            ):
                await channel.send(**file_message(file, new_version=new_version))

        elif event not in ev.MEMBER_EVENTS:  # member events are handled by the pilots cog
            log.info("Ignoring unhandled webhook event %s", event)


async def setup(bot: FlyVikingBot) -> None:
    await bot.add_cog(WebsiteEvents(bot))
