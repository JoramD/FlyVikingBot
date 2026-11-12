import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from flyvikingbot.webhooks.server import WebhookServer, detect_event

SECRET = "x" * 32


@pytest.fixture
async def receiver(tmp_path):
    received = []
    server = WebhookServer(SECRET, lambda e, p: received.append((e, p)), capture_dir=tmp_path, capture_keep=3)
    async with TestClient(TestServer(server.app)) as client:
        yield client, received, tmp_path


async def test_health(receiver):
    client, _, _ = receiver
    resp = await client.get("/health")
    assert resp.status == 200


async def test_wrong_secret_is_404(receiver):
    client, received, _ = receiver
    resp = await client.post("/ips/wrong/galleryImage_create", json={})
    assert resp.status == 404
    assert received == []


async def test_event_from_path(receiver):
    client, received, tmp_path = receiver
    resp = await client.post(f"/ips/{SECRET}/galleryImage_create", json={"id": 1})
    assert resp.status == 200
    assert received == [("galleryImage_create", {"id": 1})]
    captures = list(tmp_path.glob("*.json"))
    assert len(captures) == 1
    assert json.loads(captures[0].read_text())["body"] == {"id": 1}


async def test_event_from_header(receiver):
    client, received, _ = receiver
    await client.post(f"/ips/{SECRET}", json={"id": 2}, headers={"X-IPS-Event": "blogEntry_create"})
    assert received == [("blogEntry_create", {"id": 2})]


async def test_unknown_event_name_not_dispatched(receiver):
    client, received, tmp_path = receiver
    resp = await client.post(f"/ips/{SECRET}", json={"id": 3})
    assert resp.status == 200
    assert received == []
    assert len(list(tmp_path.glob("*.json"))) == 1  # still captured for inspection


async def test_invalid_json(receiver):
    client, received, _ = receiver
    resp = await client.post(f"/ips/{SECRET}/x", data=b"{not json", headers={"Content-Type": "application/json"})
    assert resp.status == 400
    assert received == []


async def test_capture_pruning_and_redaction(receiver):
    client, _, tmp_path = receiver
    for i in range(5):
        await client.post(f"/ips/{SECRET}/e{i}", json={"i": i}, headers={"Authorization": "Bearer secret"})
    captures = sorted(tmp_path.glob("*.json"))
    assert len(captures) == 3
    record = json.loads(captures[-1].read_text())
    assert record["headers"]["Authorization"] == "<redacted>"


async def test_trailing_slash_accepted(receiver):
    client, received, _ = receiver
    resp = await client.post(f"/ips/{SECRET}/galleryImage_create/", json={"id": 4})
    assert resp.status == 200
    assert received == [("galleryImage_create", {"id": 4})]


async def test_unmatched_requests_are_logged_with_secret_masked(receiver, caplog):
    client, _, _ = receiver
    caplog.set_level("INFO")
    resp = await client.get(f"/ips/{SECRET}/galleryImage_create")
    assert resp.status == 405
    assert "GET /ips/<secret>/galleryImage_create" in caplog.text
    assert SECRET not in caplog.text


async def test_capture_redacts_member_personal_data(receiver):
    client, received, tmp_path = receiver
    body = {"title": "x", "author": {"name": "A", "email": "a@b.c", "registrationIpAddress": "1.2.3.4"}}
    await client.post(f"/ips/{SECRET}/blogEntry_create", json=body)
    saved = json.loads(next(tmp_path.glob("*.json")).read_text())["body"]
    assert saved["author"] == {"name": "A", "email": "<redacted>", "registrationIpAddress": "<redacted>"}
    assert received[0][1]["author"]["email"] == "a@b.c"  # handlers still get the original


def test_short_secret_rejected():
    with pytest.raises(ValueError):
        WebhookServer("short", lambda e, p: None)


def test_detect_event_from_body():
    assert detect_event({}, {"event": "downloads_new_version"}) == "downloads_new_version"
    assert detect_event({"X-Signature": "abc"}, {}) is None
