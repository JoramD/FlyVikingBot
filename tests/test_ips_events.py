from flyvikingbot.webhooks import ips_events as ev

from .conftest import load_fixture


def test_gallery_image_prefers_large():
    image = ev.parse_gallery_image(load_fixture("galleryImage_create"))
    assert image.image_url.endswith("sunset.large.jpg")


def test_gallery_image_without_images_returns_none():
    assert ev.parse_gallery_image({"title": "x", "url": "https://x"}) is None


def test_blog_entry_fields():
    entry = ev.parse_blog_entry(load_fixture("blogEntry_create"))
    assert entry.title == "New routes for winter 2026"
    assert entry.blog_id == 3
    assert entry.image_url.endswith("cover.jpg")


def test_blog_entry_falls_back_to_inline_image():
    payload = load_fixture("blogEntry_create")
    payload["image"] = None
    assert ev.parse_blog_entry(payload).image_url.endswith("routes.png")


def test_blog_entry_without_any_image():
    payload = load_fixture("blogEntry_create")
    payload["image"] = None
    payload["entry"] = "<p>Text only</p>"
    entry = ev.parse_blog_entry(payload)
    assert entry is not None and entry.image_url is None


def test_blog_entry_missing_url_returns_none():
    assert ev.parse_blog_entry({"title": "No url"}) is None


def test_downloads_file():
    file = ev.parse_downloads_file(load_fixture("downloadsFile_create"))
    assert file.url.endswith("55-flyviking-a320neo-livery/")
    assert file.version == "1.0.0"


def test_new_version_with_nested_file():
    payload = {"version": "1.1.0", "file": load_fixture("downloadsFile_create")}
    file = ev.parse_downloads_file(payload, ev.DOWNLOADS_NEW_VERSION)
    assert file.url.endswith("55-flyviking-a320neo-livery/")


def test_unwrap_envelope():
    wrapped = {"event": ev.GALLERY_IMAGE_CREATE, "data": load_fixture("galleryImage_create")}
    assert ev.parse_gallery_image(wrapped) is not None


def test_garbage_payloads_do_not_raise():
    for junk in (None, 5, "text", [], [1], {"data": "x"}):
        assert ev.parse_gallery_image(junk) is None
        assert ev.parse_blog_entry(junk) is None
        assert ev.parse_downloads_file(junk) is None
