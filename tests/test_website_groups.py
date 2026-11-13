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

GROUPS = [(2, "Guests"), (3, "Members"), (4, "Administrators"), (6, "Moderators"), (8, "Painter/Modder"),
          (20, "First Officer"), (21, "Captain"), (22, "Cargo Captain"),
          (23, "Trainee Pilot"), (24, "Senior First Officer"), (25, "Cargo First Officer"), (30, "Board of Directors")]
RANKS = [Rank(1, "First Officer", "FO"), Rank(2, "Captain", None), Rank(9, "Chief Executive", "CEO")]


def ips_member(primary=3, secondary=()):
    return {"id": 1, "primaryGroup": {"id": primary, "name": "x"}, "secondaryGroups": [{"id": g} for g in secondary]}


@pytest.fixture
async def env(tmp_path):
    settings = load_settings(
        env={**BASE_ENV, "DATA_DIR": str(tmp_path), "WEBSITE_RANK_SYNC": "true", "CHANNEL_SYSTEM_MESSAGES": "900"},
    )
    storage = Storage(tmp_path / "db.sqlite")
    await storage.init()
    website = SimpleNamespace(list_groups=AsyncMock(return_value=GROUPS), get_member=AsyncMock(),
                              update_member_groups=AsyncMock(), update_member_field=AsyncMock())
    bot = SimpleNamespace(settings=settings, storage=storage, fsa=SimpleNamespace(ranks=AsyncMock(return_value=RANKS)),
                          website=website, primary_guild=lambda: None, post_system_message=AsyncMock())
    return SimpleNamespace(cog=pc.Pilots(bot), bot=bot, website=website)


async def test_promotion_swaps_rank_group_and_keeps_others(env):
    env.website.get_member.return_value = ips_member(secondary=[20, 30, 99])  # FO, Board, something else
    change = await env.cog.set_website_groups(1, 2, label="<@1>")
    env.website.update_member_groups.assert_awaited_once_with(1, primary=None, secondary=[21, 30, 99])
    assert change == "<@1> (website): First Officer → Captain"


async def test_board_rank_removes_rank_groups_never_adds_board(env):
    env.website.get_member.return_value = ips_member(secondary=[21, 22, 30])
    await env.cog.set_website_groups(1, 9)
    env.website.update_member_groups.assert_awaited_once_with(1, primary=None, secondary=[30])


async def test_up_to_date_makes_no_write(env):
    env.website.get_member.return_value = ips_member(secondary=[21])
    assert await env.cog.set_website_groups(1, 2) is None
    env.website.update_member_groups.assert_not_awaited()


@pytest.mark.parametrize(
    ("primary", "secondary"),
    [
        (2, [21]),  # Guests: not kept
        (21, [21]),  # a rank group: not kept (it's managed)
        (8, [8, 21]),  # Painter/Modder: kept as secondary so they keep painter permissions
        (30, [21, 30]),  # Board group: kept
    ],
)
async def test_pilot_rank_sets_primary_to_members(env, primary, secondary):
    env.website.get_member.return_value = ips_member(primary=primary, secondary=[20])
    change = await env.cog.set_website_groups(1, 2, label="<@1>")
    env.website.update_member_groups.assert_awaited_once_with(1, primary=3, secondary=secondary)
    assert "primary group" in change and "→ Members" in change


async def test_painter_without_rank_untouched(env):
    env.website.get_member.return_value = ips_member(primary=8, secondary=[])
    await env.cog.set_website_groups(1, 9)  # Board/unmapped rank: primary stays Painter/Modder
    await env.cog.set_website_groups(1, None, clear=True)  # unlinked
    env.website.update_member_groups.assert_not_awaited()


@pytest.mark.parametrize("primary", [4, 6])  # Administrators, Moderators
async def test_protected_primary_groups_untouched(env, primary):
    env.website.get_member.return_value = ips_member(primary=primary, secondary=[20])
    await env.cog.set_website_groups(1, 2)
    env.website.update_member_groups.assert_awaited_once_with(1, primary=None, secondary=[21])


async def test_board_rank_and_unlink_never_change_primary(env):
    env.website.get_member.return_value = ips_member(primary=2, secondary=[21])
    await env.cog.set_website_groups(1, 9)
    env.website.update_member_groups.assert_awaited_once_with(1, primary=None, secondary=[])
    env.website.update_member_groups.reset_mock()
    await env.cog.set_website_groups(1, None, clear=True)
    env.website.update_member_groups.assert_awaited_once_with(1, primary=None, secondary=[])


async def test_missing_members_group_reported_and_primary_left(env):
    env.website.list_groups.return_value = [g for g in GROUPS if g[1] != "Members"]
    env.website.get_member.return_value = ips_member(primary=2, secondary=[21])
    assert await env.cog.set_website_groups(1, 2) is None
    env.website.update_member_groups.assert_not_awaited()
    assert "WEBSITE_MEMBER_GROUP" in env.bot.post_system_message.await_args.args[0]


async def test_clear_on_unlink(env):
    env.website.get_member.return_value = ips_member(secondary=[21, 30])
    await env.cog.set_website_groups(1, None, clear=True)
    env.website.update_member_groups.assert_awaited_once_with(1, primary=None, secondary=[30])


async def test_enabled_by_default_can_be_disabled_and_unknown_rank_skipped(env):
    assert await env.cog.set_website_groups(1, None) is None  # no rank from FSA: don't touch
    assert load_settings(env={**BASE_ENV, "DATA_DIR": "x"}).website_rank_sync is True  # on by default
    env.cog.settings = load_settings(env={**BASE_ENV, "DATA_DIR": "x", "WEBSITE_RANK_SYNC": "false"})
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
        await client.update_member_groups(1, primary=3, secondary=[21, 30])
        form = received["form"]
        assert received["route"] == "/core/members/1" and form["group"] == "3"
        assert form.getall("secondaryGroups[]") == ["21", "30"]
        await client.update_member_groups(1, secondary=[])
        assert received["form"]["secondaryGroups"] == "" and "group" not in received["form"]
        await client.update_member_field(1, 7, "Captain")
        assert dict(received["form"]) == {"customFields[7]": "Captain"}
    await server.close()


# --- the rank profile field (IPS_FIELD_FSA_RANK) ---------------------------------------------------------

RANK_FIELD = 7


def member_with_rank(value):
    return {"id": 1, "customFields": {"3": {"name": "Linked accounts", "fields": {str(RANK_FIELD): {"value": value}}}}}


@pytest.fixture
def field_env(env, tmp_path):
    env.cog.settings = load_settings(env={**BASE_ENV, "DATA_DIR": str(tmp_path), "CHANNEL_SYSTEM_MESSAGES": "900",
                                          "IPS_FIELD_FSA_RANK": str(RANK_FIELD)})
    return env


@pytest.mark.parametrize(
    ("current", "rank_id", "clear", "written"),
    [
        ("First Officer", 2, False, "Captain"),  # promotion
        (None, 1, False, "First Officer"),  # first sync: field still empty
        ("Captain", 9, False, "Chief Executive"),  # Board ranks are shown too (display only)
        ("Captain", None, True, ""),  # unlink clears it
    ],
)
async def test_rank_field_written(field_env, current, rank_id, clear, written):
    field_env.website.get_member.return_value = member_with_rank(current)
    change = await field_env.cog.set_website_rank_field(1, rank_id, clear=clear, label="<@1>")
    field_env.website.update_member_field.assert_awaited_once_with(1, RANK_FIELD, written)
    assert change == f"<@1> (website rank field): {current or 'empty'} → {written or 'empty'}"


async def test_rank_field_up_to_date_makes_no_write(field_env):
    field_env.website.get_member.return_value = member_with_rank("Captain")
    assert await field_env.cog.set_website_rank_field(1, 2) is None
    field_env.website.update_member_field.assert_not_awaited()


async def test_rank_field_skipped_without_setting_rank_or_rank_list(env, field_env):
    assert await field_env.cog.set_website_rank_field(1, None) is None  # FSA gave no rank
    assert await field_env.cog.set_website_rank_field(1, 42) is None  # rank not in the rank list
    field_env.bot.fsa.ranks.side_effect = ServiceError("down")
    assert await field_env.cog.set_website_rank_field(1, 2) is None  # rank list unavailable
    field_env.website.get_member.assert_not_awaited()
    field_env.cog.settings = load_settings(env={**BASE_ENV, "DATA_DIR": "x"})  # IPS_FIELD_FSA_RANK unset
    field_env.bot.fsa.ranks.side_effect = None
    assert await field_env.cog.set_website_rank_field(1, 2) is None
    field_env.website.get_member.assert_not_awaited()


async def test_rank_field_error_reported_once(field_env):
    field_env.website.get_member.return_value = member_with_rank("First Officer")
    field_env.website.update_member_field.side_effect = ServiceError("Website API error: NO_PERMISSION")
    await field_env.cog.set_website_rank_field(1, 2)
    await field_env.cog.set_website_rank_field(1, 2)
    field_env.bot.post_system_message.assert_awaited_once()
    assert "IPS_FIELD_FSA_RANK" in field_env.bot.post_system_message.await_args.args[0]


async def test_sync_ranks_updates_groups_and_rank_field(field_env):
    field_env.website.get_member.side_effect = [ips_member(secondary=[20]), member_with_rank("First Officer")]
    change = await field_env.cog.sync_ranks(None, 1, 2)
    assert change == ("website member 1 (website): First Officer → Captain; "
                      "website member 1 (website rank field): First Officer → Captain")
