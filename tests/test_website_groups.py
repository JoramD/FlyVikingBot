"""Website rank groups: mirroring FSA ranks to IPS secondary groups."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from flyvikingbot.cogs import pilots as pc
from flyvikingbot.config import load_settings
from flyvikingbot.services.fsairlines import Rank
from flyvikingbot.services.http import ServiceError
from flyvikingbot.services.website import WebsiteClient, member_group_ids
from flyvikingbot.storage import Storage

from .conftest import BASE_ENV

GROUPS = [(3, "Members"), (4, "Administrators"), (20, "First Officer"), (21, "Captain"), (22, "Cargo Captain"),
          (23, "Trainee Pilot"), (24, "Senior First Officer"), (25, "Cargo First Officer"), (30, "Board of Directors")]
RANKS = [Rank(1, "First Officer", "FO"), Rank(2, "Captain", None), Rank(9, "Chief Executive", "CEO")]


def ips_member(primary=3, secondary=()):
    return {"id": 1, "primaryGroup": {"id": primary, "name": "x"}, "secondaryGroups": [{"id": g} for g in secondary]}


@pytest.fixture
async def env(tmp_path):
    settings = load_settings(
        env={**BASE_ENV, "DATA_DIR": str(tmp_path), "WEBSITE_RANK_SYNC": "true", "CHANNEL_SYSTEM_MESSAGES": "900"},
        credentials_path=tmp_path / "none",
    )
    storage = Storage(tmp_path / "db.sqlite")
    await storage.init()
    website = SimpleNamespace(list_groups=AsyncMock(return_value=GROUPS), get_member=AsyncMock(),
                              set_secondary_groups=AsyncMock())
    bot = SimpleNamespace(settings=settings, storage=storage, fsa=SimpleNamespace(ranks=AsyncMock(return_value=RANKS)),
                          website=website, primary_guild=lambda: None, post_system_message=AsyncMock())
    return SimpleNamespace(cog=pc.Pilots(bot), bot=bot, website=website)


async def test_promotion_swaps_rank_group_and_keeps_others(env):
    env.website.get_member.return_value = ips_member(secondary=[20, 30, 99])  # FO, Board, something else
    change = await env.cog.set_website_groups(1, 2, label="<@1>")
    env.website.set_secondary_groups.assert_awaited_once_with(1, [21, 30, 99])
    assert change == "<@1> (website): First Officer → Captain"


async def test_board_rank_removes_rank_groups_never_adds_board(env):
    env.website.get_member.return_value = ips_member(secondary=[21, 22, 30])
    await env.cog.set_website_groups(1, 9)
    env.website.set_secondary_groups.assert_awaited_once_with(1, [30])


async def test_up_to_date_makes_no_write(env):
    env.website.get_member.return_value = ips_member(secondary=[21])
    assert await env.cog.set_website_groups(1, 2) is None
    env.website.set_secondary_groups.assert_not_awaited()


async def test_primary_group_never_changed_or_duplicated(env):
    env.website.get_member.return_value = ips_member(primary=21, secondary=[20])  # primary already Captain
    await env.cog.set_website_groups(1, 2)
    env.website.set_secondary_groups.assert_awaited_once_with(1, [])


async def test_clear_on_unlink(env):
    env.website.get_member.return_value = ips_member(secondary=[21, 30])
    await env.cog.set_website_groups(1, None, clear=True)
    env.website.set_secondary_groups.assert_awaited_once_with(1, [30])


async def test_disabled_by_default_and_unknown_rank_skipped(env):
    assert await env.cog.set_website_groups(1, None) is None  # no rank from FSA: don't touch
    env.cog.settings = load_settings(env={**BASE_ENV, "DATA_DIR": "x"}, credentials_path="none")
    assert env.cog.settings.website_rank_sync is False
    assert await env.cog.set_website_groups(1, 2) is None
    env.website.get_member.assert_not_awaited()


async def test_api_permission_error_reported_once(env):
    env.website.get_member.side_effect = ServiceError("Website API error 3S290/C: NO_PERMISSION")
    await env.cog.set_website_groups(1, 2)
    await env.cog.set_website_groups(1, 2)
    env.bot.post_system_message.assert_awaited_once()
    assert "NO_PERMISSION" in env.bot.post_system_message.await_args.args[0]


async def test_missing_group_reported(env):
    env.website.list_groups.return_value = [g for g in GROUPS if g[1] != "Trainee Pilot"]
    env.website.get_member.return_value = ips_member()
    await env.cog.set_website_groups(1, 2)
    assert "'Trainee Pilot'" in env.bot.post_system_message.await_args.args[0]


def test_member_group_ids():
    assert member_group_ids(ips_member(primary=4, secondary=[20, 30])) == (4, {20, 30})
    assert member_group_ids({}) == (None, set())


# --- the HTTP side against a local fake IPS ------------------------------------------------------------


async def test_website_client_group_requests():
    received = {}

    async def handler(request: web.Request) -> web.Response:
        route = request.query_string.split("&", 1)[0]
        if request.method == "POST":
            received["route"], received["form"] = route, await request.post()
            return web.json_response({"id": 1})
        page = int(request.query.get("page", 1))
        results = [{"id": 20, "name": "First Officer"}] if page == 1 else [{"id": 21, "name": "Captain"}]
        return web.json_response({"page": page, "totalPages": 2, "results": results})

    app = web.Application()
    app.router.add_route("*", "/api/index.php", handler)
    server = TestServer(app)
    await server.start_server()
    async with aiohttp.ClientSession() as session:
        client = WebsiteClient(session, str(server.make_url("/api/index.php")), "key")
        assert await client.list_groups() == [(20, "First Officer"), (21, "Captain")]
        await client.set_secondary_groups(1, [21, 30])
        assert received["route"] == "/core/members/1" and received["form"].getall("secondaryGroups[]") == ["21", "30"]
        await client.set_secondary_groups(1, [])
        assert received["form"]["secondaryGroups"] == ""
    await server.close()
