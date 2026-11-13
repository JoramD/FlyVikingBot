"""Post a sample of every automatic message to the bot development channel, to check how posts look
without posting anything on the website or flying a flight. Run on the server:

    docker compose exec bot python -m flyvikingbot.tools.preview_posts            # all previews
    docker compose exec bot python -m flyvikingbot.tools.preview_posts gallery    # just one (or a few)
    docker compose exec bot python -m flyvikingbot.tools.preview_posts --list

Posts to CHANNEL_BOT_DEVELOPMENT using the bot's own message builders, so a preview looks exactly like the real
post. Nobody is pinged: an announcement still shows "@everyone" but mentions are switched off. It only uses
Discord's REST API, so it can run while the bot is online.

RULE: every kind of automatic post has a ``*_message`` builder in its cog, and that builder is listed in
PREVIEWS below with sample data. tests/test_preview_posts.py fails when a builder is missing here.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import struct
import sys
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import discord

from ..cogs.flight_logs import flight_log_message
from ..cogs.website_events import announcement_message, file_message, gallery_message
from ..config import ConfigError, load_settings
from ..services.fsairlines import FlightLog
from ..webhooks import ips_events as ev

SAMPLE_IMAGE = "preview.png"


def sample_png(width: int = 640, height: int = 360) -> bytes:
    """A small gradient image, made without image libraries, to stand in for screenshots."""
    rows = b"".join(
        b"\x00" + bytes(c for x in range(width) for c in (237, (x * 160) // width, (y * 200) // height))
        for y in range(height)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


@dataclass(frozen=True)
class Context:
    """What the samples need from the environment."""

    site: str  # website root, e.g. https://flyviking.net
    avatar_url: str | None  # the bot's avatar stands in for a member's
    mention: str  # the bot's mention stands in for a linked pilot

    @property
    def author(self) -> ev.Author:
        return ev.Author(name="SamplePilot", profile_url=f"{self.site}/profile/", avatar_url=self.avatar_url)


NOW = datetime.now(UTC)
INTRO = (
    "<p>Starting next month FlyViking operates three new routes out of Keflavik. "
    "Book your flights in FSAirlines and see the full schedule on the website.</p>"
)


def _announcement(ctx: Context) -> dict[str, Any]:
    entry = ev.BlogEntry(
        title="New routes from Keflavik", url=f"{ctx.site}/blogs/", blog_id=1,
        image_url=f"attachment://{SAMPLE_IMAGE}", author=ctx.author, excerpt=ev.excerpt(INTRO),
        category="Announcement", date=NOW,
    )
    return announcement_message(entry)


def _file(ctx: Context, *, new_version: bool) -> dict[str, Any]:
    file = ev.DownloadsFile(
        title="FlyViking A320neo livery", url=f"{ctx.site}/files/", version="2.0.0" if new_version else "1.0.0",
        author=ctx.author, excerpt=ev.excerpt("<p>Our house livery for the default A320neo.</p>"),
        category="Paints › FlyViking", image_url=f"attachment://{SAMPLE_IMAGE}",
        changelog=ev.excerpt("<p>Sharper tail logo, fixed the <b>door</b> textures.</p>") if new_version else None,
        date=NOW,
    )
    return file_message(file, new_version=new_version)


def _gallery(ctx: Context) -> dict[str, Any]:
    image = ev.GalleryImage(url=f"{ctx.site}/gallery/", image_url=f"{ctx.site}/{SAMPLE_IMAGE}", author=ctx.author)
    # A linked uploader is shown as their Discord mention (unlinked ones get their website profile link)
    return gallery_message(image, sample_png(), mention=ctx.mention)


def _flight_log(ctx: Context) -> dict[str, Any]:
    flight = FlightLog(
        id=0, number="FVK123", dep="BIKF", arr="ENGM", planned_arr="ENGM", aircraft="Airbus A320neo",
        pilot_id="0", pilot_name="SamplePilot", hours=2.75, distance=1046, rating=95, pax=164, cargo_kg=2140,
        landing_rate=-142, completed_at=NOW, callsign="FVK000", packages_kg=None, sim="FS2024",
    )
    return flight_log_message(flight, ctx.mention)


# name -> (the real builder, a function that calls it with sample data)
PREVIEWS: dict[str, tuple[Callable[..., dict[str, Any]], Callable[[Context], dict[str, Any]]]] = {
    "announcement": (announcement_message, _announcement),
    "new_file": (file_message, lambda ctx: _file(ctx, new_version=False)),
    "new_version": (file_message, lambda ctx: _file(ctx, new_version=True)),
    "gallery": (gallery_message, _gallery),
    "flight_log": (flight_log_message, _flight_log),
}


def render(name: str, ctx: Context) -> dict[str, Any]:
    """The send() kwargs for one preview, with pings off and the sample image attached where an embed uses it."""
    kwargs = PREVIEWS[name][1](ctx)
    kwargs["allowed_mentions"] = discord.AllowedMentions.none()
    embed = kwargs.get("embed")
    if embed is not None and embed.image.url == f"attachment://{SAMPLE_IMAGE}":
        kwargs["file"] = discord.File(io.BytesIO(sample_png()), filename=SAMPLE_IMAGE)
    return kwargs


async def main(names: list[str]) -> int:
    settings = load_settings()
    if not settings.channel_bot_development:
        print("CHANNEL_BOT_DEVELOPMENT is not set in .env; nowhere to post previews.")
        return 1
    async with discord.Client(intents=discord.Intents.none()) as client:
        await client.login(settings.discord_token)  # REST only; no gateway connection next to the running bot
        user = client.user
        ctx = Context(
            site=settings.website_root.rstrip("/"),
            avatar_url=user.display_avatar.url if user else None,
            mention=user.mention if user else "SamplePilot",
        )
        channel = await client.fetch_channel(settings.channel_bot_development)
        for name in names:
            await channel.send(f"-# Preview: {name} (sample data)", allowed_mentions=discord.AllowedMentions.none())
            await channel.send(**render(name, ctx))
            print(f"Posted {name}")
    return 0


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post sample messages to CHANNEL_BOT_DEVELOPMENT.")
    parser.add_argument(
        "names", nargs="*", metavar="name", help=f"previews to post (default: all): {', '.join(PREVIEWS)}"
    )
    parser.add_argument("--list", action="store_true", help="list the available previews")
    args = parser.parse_args(argv)
    if args.list:
        print("\n".join(PREVIEWS))
        return 0
    if unknown := [n for n in args.names if n not in PREVIEWS]:
        parser.error(f"unknown preview(s): {', '.join(unknown)}; choose from {', '.join(PREVIEWS)}")
    try:
        return asyncio.run(main(args.names or list(PREVIEWS)))
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(run())
