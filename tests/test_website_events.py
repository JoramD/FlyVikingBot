from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from flyvikingbot.cogs import website_events
from flyvikingbot.cogs.website_events import WebsiteEvents
from flyvikingbot.config import load_settings
from flyvikingbot.webhooks import ips_events as ev

from .conftest import BASE_ENV, load_fixture


def make_cog(tmp_path, **env):
    settings = load_settings(env={**BASE_ENV, "DATA_DIR": str(tmp_path), **env}, credentials_path=tmp_path / "x")
    channels = {100: AsyncMock(), 200: AsyncMock()}
    bot = SimpleNamespace(
        settings=settings, get_channel=lambda cid: channels.get(cid), fetch_channel=AsyncMock(), http_session=None
    )
    return WebsiteEvents(bot), channels


async def test_gallery_uploads_image_as_attachment(tmp_path):
    cog, ch = make_cog(tmp_path)
    cog._fetch_image = AsyncMock(return_value=b"\x89PNG fake")
    await cog.handle(ev.GALLERY_IMAGE_CREATE, load_fixture("real_galleryImage_create"))
    cog._fetch_image.assert_awaited_once()
    assert "/large.Screenshot" in cog._fetch_image.await_args.args[0]
    file = ch[200].send.await_args.kwargs["file"]
    assert file.filename.startswith("large.Screenshot") and file.filename.endswith(".png")
    ch[100].send.assert_not_awaited()


async def test_gallery_falls_back_to_link_when_download_fails(tmp_path):
    cog, ch = make_cog(tmp_path)  # http_session=None -> no download possible
    await cog.handle(ev.GALLERY_IMAGE_CREATE, load_fixture("galleryImage_create"))
    ch[200].send.assert_awaited_once()
    assert ch[200].send.await_args.args[0].endswith("sunset.large.jpg")


@pytest.fixture
async def image_server():
    app = web.Application()
    app.router.add_get("/ok.png", lambda r: web.Response(body=b"img", content_type="image/png"))
    app.router.add_get("/big.png", lambda r: web.Response(body=b"x" * 64, content_type="image/png"))
    async with TestClient(TestServer(app)) as client, aiohttp.ClientSession() as session:
        yield session, str(client.make_url(""))


async def test_fetch_image_against_server(tmp_path, image_server, monkeypatch):
    session, base = image_server
    cog, _ = make_cog(tmp_path)
    cog.bot.http_session = session
    monkeypatch.setattr(website_events, "MAX_UPLOAD_BYTES", 32)
    assert await cog._fetch_image(f"{base}/ok.png") == b"img"
    assert await cog._fetch_image(f"{base}/big.png") is None  # over the limit
    assert await cog._fetch_image(f"{base}/missing.png") is None  # 404
    assert await cog._fetch_image("http://127.0.0.1:1/x.png") is None  # unreachable


async def test_blog_entry_announces_with_everyone(tmp_path):
    cog, ch = make_cog(tmp_path, ANNOUNCEMENT_BLOG_ID="3")
    await cog.handle(ev.BLOG_ENTRY_CREATE, load_fixture("blogEntry_create"))
    call = ch[100].send.await_args
    assert call.args[0] == "@everyone"
    assert call.kwargs["embed"].title == "New routes for winter 2026"
    assert call.kwargs["embed"].thumbnail.url.endswith("cover.jpg")
    assert call.kwargs["allowed_mentions"].everyone is True


async def test_real_ips_blog_entry_payload(tmp_path):
    cog, ch = make_cog(tmp_path, ANNOUNCEMENT_BLOG_ID="1")
    await cog.handle(ev.BLOG_ENTRY_CREATE, load_fixture("real_blogEntry_create"))
    call = ch[100].send.await_args
    assert call.args[0] == "@everyone"
    assert call.kwargs["embed"].title == "Test9"
    assert call.kwargs["embed"].url == "https://flyviking.net/blogs/entry/21-test9/"
    assert call.kwargs["embed"].thumbnail.url is None


@pytest.mark.parametrize(
    ("event", "text"),
    [(ev.DOWNLOADS_FILE_CREATE, "New file available!"), (ev.DOWNLOADS_NEW_VERSION, "New file version available!")],
)
async def test_real_ips_downloads_payloads(tmp_path, event, text):
    cog, ch = make_cog(tmp_path)
    await cog.handle(event, load_fixture(f"real_{event}"))
    assert ch[100].send.await_args.args[0] == f"## [{text}](https://flyviking.net/files/file/34-test/)"


async def test_blog_entry_without_image_still_posts(tmp_path):
    cog, ch = make_cog(tmp_path)
    payload = load_fixture("blogEntry_create")
    payload["image"] = None
    payload["entry"] = "<p>No pictures</p>"
    await cog.handle(ev.BLOG_ENTRY_CREATE, payload)
    assert ch[100].send.await_args.kwargs["embed"].thumbnail.url is None


async def test_blog_entry_other_blog_ignored(tmp_path):
    cog, ch = make_cog(tmp_path, ANNOUNCEMENT_BLOG_ID="9")
    await cog.handle(ev.BLOG_ENTRY_CREATE, load_fixture("blogEntry_create"))
    ch[100].send.assert_not_awaited()


@pytest.mark.parametrize(
    ("event", "text"),
    [(ev.DOWNLOADS_FILE_CREATE, "New file available!"), (ev.DOWNLOADS_NEW_VERSION, "New file version available!")],
)
async def test_downloads(tmp_path, event, text):
    cog, ch = make_cog(tmp_path)
    await cog.handle(event, load_fixture("downloadsFile_create"))
    msg = ch[100].send.await_args.args[0]
    assert text in msg and "55-flyviking-a320neo-livery" in msg


async def test_unknown_and_malformed_events_do_nothing(tmp_path):
    cog, ch = make_cog(tmp_path)
    await cog.on_ips_webhook("member_create", {"id": 1})
    await cog.on_ips_webhook(ev.GALLERY_IMAGE_CREATE, {"nothing": "useful"})
    await cog.on_ips_webhook(ev.BLOG_ENTRY_CREATE, "garbage")
    ch[100].send.assert_not_awaited()
    ch[200].send.assert_not_awaited()
