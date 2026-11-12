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


@pytest.fixture(autouse=True)
def no_image_delays(monkeypatch):
    monkeypatch.setattr(website_events, "IMAGE_CHECK_INTERVAL", 0)
    monkeypatch.setattr(website_events, "IMAGE_SETTLE_DELAY", 0)


async def test_gallery_posts_link_once_image_is_available(tmp_path):
    cog, ch = make_cog(tmp_path)
    cog._image_available = AsyncMock(side_effect=[False, False, True])
    await cog.handle(ev.GALLERY_IMAGE_CREATE, load_fixture("real_galleryImage_create"))
    assert cog._image_available.await_count == 3
    posted = ch[200].send.await_args.args[0]
    assert posted.startswith("https://flyviking.net/uploads/monthly_2026_11/large.Screenshot")
    ch[100].send.assert_not_awaited()


async def test_gallery_posts_link_even_if_image_never_loads(tmp_path):
    cog, ch = make_cog(tmp_path)  # http_session=None -> never available
    await cog.handle(ev.GALLERY_IMAGE_CREATE, load_fixture("galleryImage_create"))
    ch[200].send.assert_awaited_once()
    assert ch[200].send.await_args.args[0].endswith("sunset.large.jpg")


@pytest.fixture
async def image_server():
    app = web.Application()
    app.router.add_get("/ok.png", lambda r: web.Response(body=b"img", content_type="image/png"))
    app.router.add_get("/page", lambda r: web.Response(text="<html>", content_type="text/html"))
    async with TestClient(TestServer(app)) as client, aiohttp.ClientSession() as session:
        yield session, str(client.make_url(""))


async def test_image_available_against_server(tmp_path, image_server):
    session, base = image_server
    cog, _ = make_cog(tmp_path)
    cog.bot.http_session = session
    assert await cog._image_available(f"{base}/ok.png") is True
    assert await cog._image_available(f"{base}/page") is False  # not an image (e.g. error page)
    assert await cog._image_available(f"{base}/missing.png") is False  # 404
    assert await cog._image_available("http://127.0.0.1:1/x.png") is False  # unreachable


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
