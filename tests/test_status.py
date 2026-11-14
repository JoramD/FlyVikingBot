"""Uptime monitoring: the Kuma push, /health, and per-service reachability."""

from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from flyvikingbot.bot import FlyVikingBot
from flyvikingbot.cogs.status import push_params, push_url
from flyvikingbot.config import load_settings
from flyvikingbot.services.http import ServiceError, ServiceHealth, get_json
from flyvikingbot.webhooks.server import WebhookServer

from .conftest import BASE_ENV


def fake_bot(connected=True, latency=42, services=()):
    return SimpleNamespace(discord_connected=connected, latency_ms=lambda: latency, service_health=lambda: services)


# --- the Kuma push ---------------------------------------------------------------------------------------------


def test_push_up_with_response_time():
    assert push_params(fake_bot()) == {"status": "up", "msg": "OK", "ping": "42"}


def test_push_down_while_disconnected_from_discord():
    assert push_params(fake_bot(connected=False)) == {"status": "down", "msg": "Disconnected from Discord", "ping": ""}


def test_push_names_unreachable_services_but_stays_up():
    fsa = ServiceHealth("FSAirlines", failing_since=1_763_121_780)  # 12:03 UTC
    ok = ServiceHealth("Website", last_ok=1_763_121_000)
    params = push_params(fake_bot(services=[fsa, ok], latency=None))
    assert params == {"status": "up", "msg": "FSAirlines unreachable since 12:03 UTC", "ping": ""}


def test_push_url_replaces_kumas_default_parameters():
    url = push_url("https://kuma.example/api/push/abc123?status=up&msg=OK&ping=", {"status": "down", "msg": "x y",
                                                                                   "ping": ""})
    assert url.path == "/api/push/abc123"
    assert dict(url.query) == {"status": "down", "msg": "x y", "ping": ""}


# --- reachability of outside services -------------------------------------------------------------------------


@pytest.fixture
async def api():
    async def ok(request):
        return web.json_response({"status": "SUCCESS"})

    async def error_answer(request):  # the service answered, just with an error: it's reachable
        return web.json_response({"errorCode": "1C292/2", "errorMessage": "INVALID_ID"}, status=404)

    app = web.Application()
    app.router.add_get("/ok", ok)
    app.router.add_get("/error", error_answer)
    async with TestClient(TestServer(app)) as client, aiohttp.ClientSession() as session:
        yield session, str(client.make_url(""))


async def test_service_health_records_reachability(api):
    session, base = api
    health = ServiceHealth("FSAirlines")
    with pytest.raises(ServiceError):
        await get_json(session, "http://127.0.0.1:1/x?apikey=secret", health=health)
    assert health.failing and "secret" not in health.error  # the URL is logged without its query (API keys)
    first_failure = health.failing_since
    with pytest.raises(ServiceError):
        await get_json(session, "http://127.0.0.1:1/x", health=health)
    assert health.failing_since == first_failure  # "since" is the first failure in a row
    await get_json(session, f"{base}/error", health=health)
    assert not health.failing and health.last_ok is not None
    assert health.as_dict()["ok"] is True and health.as_dict()["error"] is None


# --- /health -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("connected", "status"), [(True, 200), (False, 503)])
async def test_health_endpoint(connected, status):
    report = {"status": "x", "discord": {"connected": connected, "latency_ms": None}, "services": {}}
    server = WebhookServer("x" * 32, lambda e, p: None, health=lambda: report)
    async with TestClient(TestServer(server.app)) as client:
        resp = await client.get("/health")
        assert resp.status == status and await resp.json() == report


async def test_bot_health_report(tmp_path):
    bot = FlyVikingBot(load_settings(env={**BASE_ENV, "DATA_DIR": str(tmp_path)}))
    bot.fsa = SimpleNamespace(health=ServiceHealth("FSAirlines", failing_since=1_763_121_780, error="GET x failed"))
    bot.website = SimpleNamespace(health=ServiceHealth("Website", last_ok=1_763_121_000))
    report = bot.health_report()
    assert report["status"] == "discord_disconnected"  # not connected until on_ready
    assert report["discord"] == {"connected": False, "latency_ms": None}
    assert report["services"]["FSAirlines"]["ok"] is False
    assert report["services"]["Website"]["last_ok"] == "2025-11-14T11:50:00+00:00"
    assert "SayIntentions" not in report["services"]  # not configured

    await bot.on_ready()
    assert bot.health_report()["discord"]["connected"] is True
    await bot.on_disconnect()
    assert bot.discord_connected is False
    await bot.on_resumed()
    assert bot.discord_connected is True
    await bot.close()
