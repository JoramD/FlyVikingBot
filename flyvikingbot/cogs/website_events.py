"""Turns Invision Community webhook events into Discord posts."""

from __future__ import annotations

import dataclasses
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
from ..services.http import ServiceError
from ..services.website import category_path
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


def author_line(author: ev.Author | None, mention: str | None = None) -> str | None:
    """'Uploaded by @member' when the author is linked to a Discord member (mentions never ping these
    posts), otherwise their website name linked to their profile. The <> stops Discord previewing the
    link, so the uploaded image stays full size."""
    if author is None:
        return None
    if mention:
        return f"Uploaded by {mention}"
    name = discord.utils.escape_markdown(author.name)
    return f"Uploaded by [{name}](<{author.profile_url}>)" if author.profile_url else f"Uploaded by {name}"


async def with_category_path(website: Any, file: ev.DownloadsFile) -> ev.DownloadsFile:
    """The file with its category shown as the full path ('Paints › FlyViking'). Payloads only name the
    subcategory, so the names come from the website's category list. Unchanged if that isn't available."""
    if website is None or file.category_id is None:
        return file
    try:
        path = category_path(await website.download_categories(), file.category_id)
    except ServiceError as exc:
        log.warning("Could not load download categories, showing the subcategory only: %s", exc)
        return file
    return dataclasses.replace(file, category=path) if path else file


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


def gallery_message(
    image: ev.GalleryImage, data: bytes | None, source_url: str | None = None, mention: str | None = None
) -> dict[str, Any]:
    """The uploaded image with an author line, or the image link if it couldn't be downloaded.
    `source_url` is the version `data` came from (original or large), for the file name; `mention` is
    the author's Discord mention when they're linked."""
    line = author_line(image.author, mention)
    if data:
        file = discord.File(io.BytesIO(data), filename=_filename(source_url or image.image_url))
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
            # No redirects: the host check above only covers this URL, not where a redirect would lead
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=False) as resp:
                if resp.status != 200:
                    log.warning("Image download returned HTTP %s: %s", resp.status, url)
                    return None
                if (resp.content_length or 0) > MAX_UPLOAD_BYTES:
                    log.info("Image too large to upload (%s bytes): %s", resp.content_length, url)
                    return None
                # Read every chunk: content.read(n) returns as soon as *some* data has arrived, which
                # uploaded only the top strip of the image
                data = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    data += chunk
                    if len(data) > MAX_UPLOAD_BYTES:
                        log.info("Image too large to upload: %s", url)
                        return None
                return bytes(data)
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.warning("Image download failed (%s): %s", type(exc).__name__, url)
            return None

    async def author_mention(self, author: ev.Author | None) -> str | None:
        """The author's Discord mention if their website account is linked and they're in the server."""
        storage = getattr(self.bot, "storage", None)
        if author is None or author.member_id is None or storage is None:
            return None
        discord_id = await storage.discord_id_for_ips_member(author.member_id)
        guild = self.bot.primary_guild() if discord_id else None
        return f"<@{discord_id}>" if guild and guild.get_member(discord_id) else None

    async def _fetch_gallery_image(self, image: ev.GalleryImage) -> tuple[bytes | None, str | None]:
        """The best version that fits Discord's upload limit: the original upload, else IPS's resized
        "large" version. Returns (data, the URL it came from), or (None, None) to fall back to the link."""
        for url in dict.fromkeys(u for u in (image.original_url, image.image_url) if u):
            if data := await self._fetch_image(url):
                return data, url
        return None, None

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
                data, source = await self._fetch_gallery_image(image)
                mention = await self.author_mention(image.author)
                await channel.send(**gallery_message(image, data, source, mention))

        elif event == ev.BLOG_ENTRY_CREATE:
            entry = ev.parse_blog_entry(payload)
            if not entry:
                return
            wanted = self.settings.announcement_blog_id
            if wanted is None:
                # Off until set: otherwise members' own blogs would ping @everyone
                log.info("ANNOUNCEMENT_BLOG_ID not set; not posting blog entry %r", entry.title)
                return
            if entry.blog_id != wanted:
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
                file = await with_category_path(getattr(self.bot, "website", None), file)
                await channel.send(**file_message(file, new_version=new_version))

        elif event not in ev.MEMBER_EVENTS:  # member events are handled by the pilots cog
            log.info("Ignoring unhandled webhook event %s", event)


async def setup(bot: FlyVikingBot) -> None:
    await bot.add_cog(WebsiteEvents(bot))
