import asyncio
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
    settings = load_settings(env={**BASE_ENV, "DATA_DIR": str(tmp_path), **env})
    channels = {100: AsyncMock(), 200: AsyncMock()}
    bot = SimpleNamespace(
        settings=settings, get_channel=lambda cid: channels.get(cid), fetch_channel=AsyncMock(), http_session=None,
        wait_until_ready=AsyncMock()
    )
    return WebsiteEvents(bot), channels


async def test_gallery_uploads_the_original_image(tmp_path):
    cog, ch = make_cog(tmp_path)
    cog._fetch_image = AsyncMock(return_value=b"\x89PNG fake")
    await cog.handle(ev.GALLERY_IMAGE_CREATE, load_fixture("real_galleryImage_create"))
    cog._fetch_image.assert_awaited_once()  # the original fitted, so the large version isn't needed
    assert "/monthly_2026_11/Screenshot" in cog._fetch_image.await_args.args[0]
    call = ch[200].send.await_args
    file = call.kwargs["file"]
    assert file.filename.startswith("Screenshot") and file.filename.endswith(".png")
    # Author line with the profile link wrapped in <> so Discord doesn't add a preview
    assert call.kwargs["content"] == "Uploaded by [Alexhs2](<https://flyviking.net/profile/3-alexhs2/>)"
    assert call.kwargs["allowed_mentions"].everyone is False
    ch[100].send.assert_not_awaited()


async def test_gallery_uses_large_version_when_original_is_too_big(tmp_path):
    cog, ch = make_cog(tmp_path)
    cog._fetch_image = AsyncMock(side_effect=[None, b"\x89PNG large"])  # original over the limit, large fits
    await cog.handle(ev.GALLERY_IMAGE_CREATE, load_fixture("real_galleryImage_create"))
    urls = [c.args[0] for c in cog._fetch_image.await_args_list]
    assert "/monthly_2026_11/Screenshot" in urls[0] and "/large.Screenshot" in urls[1]
    assert ch[200].send.await_args.kwargs["file"].filename.startswith("large.Screenshot")


async def test_gallery_falls_back_to_link_when_download_fails(tmp_path):
    cog, ch = make_cog(tmp_path)  # http_session=None -> no download possible
    await cog.handle(ev.GALLERY_IMAGE_CREATE, load_fixture("real_galleryImage_create"))
    ch[200].send.assert_awaited_once()
    text = ch[200].send.await_args.kwargs["content"]
    line, url = text.split("\n")
    assert line.startswith("Uploaded by [Alexhs2]")
    assert "/large.Screenshot" in url


async def test_gallery_without_author_posts_image_only(tmp_path):
    cog, ch = make_cog(tmp_path)
    cog._fetch_image = AsyncMock(return_value=b"\x89PNG fake")
    payload = load_fixture("real_galleryImage_create")
    del payload["author"]
    await cog.handle(ev.GALLERY_IMAGE_CREATE, payload)
    assert ch[200].send.await_args.kwargs["content"] is None


STREAMED_IMAGE = bytes(range(256)) * 4096  # 1 MiB, arriving in many network chunks like a real download


async def _streamed(request: web.Request, body: bytes) -> web.StreamResponse:
    """Send the body in 64 KiB pieces without a Content-Length (chunked transfer), pausing in between,
    so the client sees it arrive in parts."""
    response = web.StreamResponse(headers={"Content-Type": "image/png"})
    await response.prepare(request)
    for i in range(0, len(body), 64 * 1024):
        await response.write(body[i : i + 64 * 1024])
        await asyncio.sleep(0.001)
    await response.write_eof()
    return response


@pytest.fixture
async def image_server():
    async def ok(request):
        return web.Response(body=b"img", content_type="image/png")

    async def big(request):
        return web.Response(body=b"x" * 64, content_type="image/png")

    async def streamed(request):
        return await _streamed(request, STREAMED_IMAGE)

    async def moved(request):
        raise web.HTTPFound("http://169.254.169.254/latest/meta-data")

    app = web.Application()
    for path, handler in (("/ok.png", ok), ("/big.png", big), ("/streamed.png", streamed), ("/moved.png", moved)):
        app.router.add_get(path, handler)
    async with TestClient(TestServer(app)) as client, aiohttp.ClientSession() as session:
        yield session, str(client.make_url(""))


async def test_fetch_image_reads_the_whole_streamed_file(tmp_path, image_server):
    """Regression: the bot uploaded only the first network chunk, so gallery posts showed a thin strip."""
    session, base = image_server
    cog, _ = make_cog(tmp_path, WEBSITE_BASE_URL=f"{base}/api/index.php")
    cog.bot.http_session = session
    assert await cog._fetch_image(f"{base}/streamed.png") == STREAMED_IMAGE


async def test_fetch_image_size_limit_without_content_length(tmp_path, image_server, monkeypatch):
    session, base = image_server
    cog, _ = make_cog(tmp_path, WEBSITE_BASE_URL=f"{base}/api/index.php")
    cog.bot.http_session = session
    monkeypatch.setattr(website_events, "MAX_UPLOAD_BYTES", 100 * 1024)
    assert await cog._fetch_image(f"{base}/streamed.png") is None  # chunked, so only the running total can tell


async def test_fetch_image_does_not_follow_redirects(tmp_path, image_server):
    """The host check only sees the first URL; a redirect could point the download anywhere."""
    session, base = image_server
    cog, _ = make_cog(tmp_path, WEBSITE_BASE_URL=f"{base}/api/index.php")
    cog.bot.http_session = session
    assert await cog._fetch_image(f"{base}/moved.png") is None  # falls back to posting the link


async def test_fetch_image_against_server(tmp_path, image_server, monkeypatch):
    session, base = image_server
    cog, _ = make_cog(tmp_path, WEBSITE_BASE_URL=f"{base}/api/index.php")  # the test server plays the website
    cog.bot.http_session = session
    monkeypatch.setattr(website_events, "MAX_UPLOAD_BYTES", 32)
    assert await cog._fetch_image(f"{base}/ok.png") == b"img"
    assert await cog._fetch_image(f"{base}/big.png") is None  # over the limit
    assert await cog._fetch_image(f"{base}/missing.png") is None  # 404
    assert await cog._fetch_image("http://127.0.0.1:1/x.png") is None  # unreachable


async def test_blog_entry_with_image_gets_large_image(tmp_path):
    cog, ch = make_cog(tmp_path, ANNOUNCEMENT_BLOG_ID="1")
    payload = load_fixture("real_blogEntry_create")
    payload["image"] = "https://flyviking.net/uploads/cover.jpg"
    await cog.handle(ev.BLOG_ENTRY_CREATE, payload)
    call = ch[100].send.await_args
    assert call.kwargs["embed"].image.url == "https://flyviking.net/uploads/cover.jpg"
    assert call.kwargs["allowed_mentions"].everyone is True


async def test_real_ips_blog_entry_payload(tmp_path):
    cog, ch = make_cog(tmp_path, ANNOUNCEMENT_BLOG_ID="1")
    await cog.handle(ev.BLOG_ENTRY_CREATE, load_fixture("real_blogEntry_create"))
    call = ch[100].send.await_args
    embed = call.kwargs["embed"]
    assert call.kwargs["content"] == "@everyone"
    assert embed.title == "Test9"
    assert embed.url == "https://flyviking.net/blogs/entry/21-test9/"
    assert embed.description == "9"
    assert embed.author.name is None  # announcements don't name the author
    assert embed.footer.text == "Announcement · flyviking.net"
    assert embed.timestamp is not None and embed.timestamp.year == 2026
    assert embed.image.url is None


@pytest.mark.parametrize(
    ("event", "text"),
    [(ev.DOWNLOADS_FILE_CREATE, "New file available"), (ev.DOWNLOADS_NEW_VERSION, "New version available")],
)
async def test_real_ips_downloads_payloads(tmp_path, event, text):
    cog, ch = make_cog(tmp_path)
    await cog.handle(event, load_fixture(f"real_{event}"))
    call = ch[100].send.await_args
    embed = call.kwargs["embed"]
    assert call.kwargs["content"] == text
    assert call.kwargs["allowed_mentions"].everyone is False
    assert embed.title == "Test"
    assert embed.url == "https://flyviking.net/files/file/34-test/"
    assert embed.description == "This is a test"
    assert embed.author.name == "JoramD"
    assert embed.footer.text == "GSX Configurations · flyviking.net"
    assert [f.name for f in embed.fields] == ["Version"]  # the fixture's changelog is empty


async def test_new_version_shows_changelog_and_screenshot(tmp_path):
    cog, ch = make_cog(tmp_path)
    payload = load_fixture("real_downloads_new_version")
    payload["changelog"] = "<p>Fixed the <b>doors</b></p>"
    payload["primaryScreenshot"] = "https://flyviking.net/uploads/shot.png"
    await cog.handle(ev.DOWNLOADS_NEW_VERSION, payload)
    embed = ch[100].send.await_args.kwargs["embed"]
    assert {f.name: f.value for f in embed.fields} == {"Version": "2.0.0", "Changes": "Fixed the doors"}
    assert embed.image.url == "https://flyviking.net/uploads/shot.png"


async def test_new_file_does_not_show_changelog(tmp_path):
    cog, ch = make_cog(tmp_path)
    payload = load_fixture("real_downloadsFile_create")
    payload["changelog"] = "<p>Old notes</p>"
    await cog.handle(ev.DOWNLOADS_FILE_CREATE, payload)
    assert [f.name for f in ch[100].send.await_args.kwargs["embed"].fields] == ["Version"]


async def test_blog_entry_other_blog_ignored(tmp_path):
    cog, ch = make_cog(tmp_path, ANNOUNCEMENT_BLOG_ID="9")
    await cog.handle(ev.BLOG_ENTRY_CREATE, load_fixture("real_blogEntry_create"))
    ch[100].send.assert_not_awaited()


async def test_blog_entries_off_without_announcement_blog(tmp_path):
    """No ANNOUNCEMENT_BLOG_ID: nothing is posted, so members' own blogs can't ping @everyone."""
    cog, ch = make_cog(tmp_path, ANNOUNCEMENT_BLOG_ID="")
    await cog.handle(ev.BLOG_ENTRY_CREATE, load_fixture("real_blogEntry_create"))
    ch[100].send.assert_not_awaited()


async def test_unknown_and_malformed_events_do_nothing(tmp_path):
    cog, ch = make_cog(tmp_path)
    await cog.on_ips_webhook("member_create", {"id": 1})
    await cog.on_ips_webhook(ev.GALLERY_IMAGE_CREATE, {"nothing": "useful"})
    await cog.on_ips_webhook(ev.BLOG_ENTRY_CREATE, "garbage")
    ch[100].send.assert_not_awaited()
    ch[200].send.assert_not_awaited()


async def test_images_only_downloaded_from_the_website(tmp_path):
    """A webhook can name any URL; fetching it would let a leaked secret probe internal addresses (SSRF)."""
    cog, _ = make_cog(tmp_path)
    cog.bot.http_session = AsyncMock()
    for url in ("http://flyviking-bot:8080/health", "http://169.254.169.254/latest", "file:///etc/passwd",
                "https://evil.example/x.png"):
        assert await cog._fetch_image(url) is None
    cog.bot.http_session.get.assert_not_called()
    assert cog._is_website_url("https://flyviking.net/uploads/x.png")


@pytest.mark.parametrize(
    ("event", "fixture"),
    [(ev.GALLERY_IMAGE_CREATE, "real_galleryImage_create"), (ev.BLOG_ENTRY_CREATE, "real_blogEntry_create"),
     (ev.DOWNLOADS_FILE_CREATE, "real_downloadsFile_create"),
     (ev.DOWNLOADS_NEW_VERSION, "real_downloads_new_version")],
)
async def test_unset_channel_means_not_posted(tmp_path, event, fixture):
    cog, ch = make_cog(tmp_path, CHANNEL_ANNOUNCEMENTS="", CHANNEL_SCREENSHOTS="")
    await cog.handle(event, load_fixture(fixture))
    ch[100].send.assert_not_awaited()
    ch[200].send.assert_not_awaited()


async def test_new_file_post_shows_full_category_path(tmp_path):
    cog, ch = make_cog(tmp_path)
    cog.bot.website = SimpleNamespace(download_categories=AsyncMock(return_value={
        5: ("Add-ons", None), 6: ("GSX Configurations", 5)}))
    await cog.handle(ev.DOWNLOADS_FILE_CREATE, load_fixture("real_downloadsFile_create"))
    assert ch[100].send.await_args.kwargs["embed"].footer.text == "Add-ons › GSX Configurations · flyviking.net"
