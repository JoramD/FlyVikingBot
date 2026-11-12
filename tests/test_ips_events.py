"""IPS webhook payload parsing, against real (anonymised) captures."""

from flyvikingbot.webhooks import ips_events as ev

from .conftest import load_fixture


def test_gallery_image_prefers_large():
    image = ev.parse_gallery_image(load_fixture("real_galleryImage_create"))
    assert "/large.Screenshot" in image.image_url
    assert image.url == "https://flyviking.net/gallery/image/375-screenshot-2026-11-10-113646png/"


def test_gallery_image_falls_back_to_original():
    payload = load_fixture("real_galleryImage_create")
    del payload["images"]["large"]
    assert "/Screenshot2026-11-10113646.png." in ev.parse_gallery_image(payload).image_url


def test_gallery_image_without_images_returns_none():
    assert ev.parse_gallery_image({"caption": "x", "url": "https://x"}) is None


def test_blog_entry_fields():
    entry = ev.parse_blog_entry(load_fixture("real_blogEntry_create"))
    assert entry.title == "Test9" and entry.blog_id == 1
    assert entry.url == "https://flyviking.net/blogs/entry/21-test9/"
    assert entry.image_url is None  # the real test entry has no image


def test_blog_entry_image_from_cover_or_inline():
    payload = load_fixture("real_blogEntry_create")
    payload["entry"] = '<p>Routes</p><p><img src="https://flyviking.net/uploads/routes.png" alt=""></p>'
    assert ev.parse_blog_entry(payload).image_url == "https://flyviking.net/uploads/routes.png"
    payload["image"] = "https://flyviking.net/uploads/cover.jpg"
    assert ev.parse_blog_entry(payload).image_url == "https://flyviking.net/uploads/cover.jpg"


def test_blog_entry_missing_url_returns_none():
    assert ev.parse_blog_entry({"title": "No url"}) is None


def test_downloads_file_and_new_version():
    file = ev.parse_downloads_file(load_fixture("real_downloadsFile_create"))
    assert file.url == "https://flyviking.net/files/file/34-test/" and file.version == "1.0.0"
    new = ev.parse_downloads_file(load_fixture("real_downloads_new_version"), ev.DOWNLOADS_NEW_VERSION)
    assert new.url == file.url and new.version == "2.0.0"


def test_member_envelope_unwrapped():
    body = load_fixture("real_member_edited")
    assert ev.unwrap(body)["id"] == 3
    assert ev.unwrap(load_fixture("real_galleryImage_create"))["id"] == 375  # bare content stays as-is


def test_garbage_payloads_do_not_raise():
    for junk in (None, 5, "text", [], [1], {"member": "x"}):
        assert ev.parse_gallery_image(junk) is None
        assert ev.parse_blog_entry(junk) is None
        assert ev.parse_downloads_file(junk) is None
