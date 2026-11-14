"""Website registrations awaiting approval: posted once, approved or deleted from Discord, synced with the ACP."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from flyvikingbot.cogs import registrations as rc
from flyvikingbot.config import load_settings
from flyvikingbot.services.http import ServiceError
from flyvikingbot.services.website import WebsiteClient, WebsiteNotFound
from flyvikingbot.storage import Storage
from flyvikingbot.webhooks import ips_events as ev

from .conftest import BASE_ENV

BOARD = 555
FSA_FIELD, DISCORD_FIELD = 3, 4


def member(mid=5, name="NewPilot42", validating=True, completed=True, fsa=None, discord_name=None):
    fields = {}
    if fsa is not None:
        fields[str(FSA_FIELD)] = {"name": "FSAirlines Pilot ID", "value": fsa}
    if discord_name is not None:
        fields[str(DISCORD_FIELD)] = {"name": "Discord Username", "value": discord_name}
    return {
        "id": mid, "name": name, "profileUrl": f"https://flyviking.net/profile/{mid}-x/",
        "joined": "2026-11-14T10:00:00Z", "validating": validating, "completed": completed,
        "customFields": {"3": {"name": "Linked accounts", "fields": fields}},
    }


def staff(is_staff=True, uid=900):
    perms = SimpleNamespace(manage_roles=is_staff, administrator=False)
    return SimpleNamespace(id=uid, guild_permissions=perms, roles=[], mention=f"<@{uid}>")


@pytest.fixture
async def env(tmp_path):
    settings = load_settings(env={
        **BASE_ENV, "DATA_DIR": str(tmp_path), "CHANNEL_SYSTEM_MESSAGES": "900", "SYSTEM_MESSAGES_ROLE_ID": str(BOARD),
        "REGISTRATION_APPROVALS": "true", "IPS_FIELD_FSA_PILOT_ID": str(FSA_FIELD),
        "IPS_FIELD_DISCORD_NAME": str(DISCORD_FIELD),
    })
    storage = Storage(tmp_path / "db.sqlite")
    await storage.init()
    website = SimpleNamespace(recent_members=AsyncMock(return_value=[]), get_member=AsyncMock(),
                              validate_member=AsyncMock(), delete_member=AsyncMock())
    posts = []

    async def post_system_message(content, *, action_required, view=None):
        posted = SimpleNamespace(
            id=1000 + len(posts), channel=SimpleNamespace(id=900), content=f"<@&{BOARD}> {content}"
        )
        posts.append(SimpleNamespace(content=content, action_required=action_required, view=view, message=posted))
        return posted

    partial = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(id=900, get_partial_message=MagicMock(return_value=partial))
    guild = SimpleNamespace(members=[SimpleNamespace(id=42, name="newpilot42", global_name=None, nick=None,
                                                      display_name="NewPilot")])
    bot = SimpleNamespace(settings=settings, storage=storage, website=website, primary_guild=lambda: guild,
                          post_system_message=post_system_message, get_channel=lambda cid: channel,
                          fetch_channel=AsyncMock(), wait_until_ready=AsyncMock())
    return SimpleNamespace(cog=rc.Registrations(bot), bot=bot, website=website, posts=posts, partial=partial,
                           storage=storage)


def click(user=None, content="<@&555> 🆕 post"):
    """A button click on the registration post."""
    message = SimpleNamespace(delete=AsyncMock())
    return SimpleNamespace(
        user=user or staff(),
        message=SimpleNamespace(content=content, edit=AsyncMock()),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock(),
                                 is_done=MagicMock(return_value=True)),
        followup=SimpleNamespace(send=AsyncMock(return_value=message)),
        edit_original_response=AsyncMock(),
    )


# --- the post ------------------------------------------------------------------------------------------------------


def test_message_without_optional_fields():
    reg = ev.parse_registration(member(), FSA_FIELD, DISCORD_FIELD)
    content = rc.registration_message(reg)["content"]
    assert content.startswith("🆕 **New website registration awaiting approval:** `NewPilot42` · [website profile]")
    assert "Registered <t:" in content
    assert "FSAirlines" not in content and "Discord" not in content  # optional fields: not mentioned when empty


def test_message_with_optional_fields_tags_the_discord_member():
    reg = ev.parse_registration(member(fsa="FVK123", discord_name="newpilot42"), FSA_FIELD, DISCORD_FIELD)
    assert rc.registration_message(reg, 42)["content"].endswith("FSAirlines Pilot ID `FVK123` · Discord <@42>")
    assert rc.registration_message(reg)["content"].endswith("Discord `newpilot42` (not found in the server)")
    assert rc.registration_message(reg)["allowed_mentions"].users is False


# --- finding registrations -----------------------------------------------------------------------------------------


async def test_poll_posts_each_pending_registration_once(env):
    env.website.recent_members.return_value = [
        member(5, discord_name="NewPilot42"),  # pending; the typed name matches server member 42
        member(6, validating=False),  # already validated
        member(7, completed=False),  # registration not finished
    ]
    await env.cog.check_registrations()
    await env.cog.check_registrations()
    assert len(env.posts) == 1
    post = env.posts[0]
    assert "`NewPilot42`" in post.content and "Discord <@42>" in post.content
    assert post.action_required is True  # pings the Board
    assert [b.custom_id for b in post.view.children] == ["fvreg:approve:5", "fvreg:reject:5"]


async def test_webhook_posts_immediately(env):
    await env.cog.on_ips_webhook(ev.MEMBER_REGISTRATION_COMPLETE, member(8))
    await env.cog.on_ips_webhook(ev.MEMBER_CREATE, member(8))  # the same member again: no second post
    assert len(env.posts) == 1


async def test_approved_in_acp_updates_the_post(env):
    await env.cog.on_ips_webhook(ev.MEMBER_CREATE, member(5))
    await env.cog.on_ips_webhook(ev.MEMBER_EDITED, {"member": member(5, validating=False), "changes": {"x": 1}})
    content = env.partial.edit.await_args.kwargs["content"]
    assert content.endswith("✅ Approved on the website") and env.partial.edit.await_args.kwargs["view"] is None
    assert await env.storage.kv_get("registration:5") is None


async def test_deleted_in_acp_updates_the_post(env):
    env.website.recent_members.return_value = [member(5)]
    await env.cog.check_registrations()
    env.website.recent_members.return_value = []  # gone from the list...
    env.website.get_member.side_effect = WebsiteNotFound("INVALID_ID")  # ...because it was deleted
    await env.cog.check_registrations()
    assert env.partial.edit.await_args.kwargs["content"].endswith("🗑️ Removed on the website")


async def test_still_pending_post_is_left_alone(env):
    env.website.recent_members.return_value = [member(5)]
    await env.cog.check_registrations()
    await env.cog.check_registrations()
    env.partial.edit.assert_not_awaited()
    assert await env.storage.kv_get("registration:5") is not None


# --- Approve -----------------------------------------------------------------------------------------------------


async def pending(env, mid=5):
    await env.cog.announce(ev.parse_registration(member(mid)))


async def test_approve_validates_and_closes_the_post(env):
    await pending(env)
    env.website.get_member.side_effect = [member(5), member(5, validating=False)]  # before and after
    interaction = click()
    await env.cog.approve(interaction, 5)
    env.website.validate_member.assert_awaited_once_with(5)
    kwargs = interaction.edit_original_response.await_args.kwargs
    assert "✅ Approved by <@900>" in kwargs["content"] and kwargs["view"] is None
    assert await env.storage.kv_get("registration:5") is None


async def test_approve_ignored_by_api_falls_back_to_acp(env):
    await pending(env)
    env.website.get_member.return_value = member(5)  # still validating afterwards
    interaction = click()
    await env.cog.approve(interaction, 5)
    interaction.edit_original_response.assert_not_awaited()  # buttons stay
    reply = interaction.followup.send.await_args
    assert "didn't accept the approval" in reply.args[0] and "members_filter_validating" in reply.args[0]
    assert reply.kwargs["ephemeral"] is True


async def test_approve_api_error_falls_back_to_acp(env):
    await pending(env)
    env.website.get_member.return_value = member(5)
    env.website.validate_member.side_effect = ServiceError("NO_PERMISSION")
    interaction = click()
    await env.cog.approve(interaction, 5)
    assert "didn't accept the approval" in interaction.followup.send.await_args.args[0]


async def test_approve_when_already_handled(env):
    await pending(env)
    env.website.get_member.return_value = member(5, validating=False)
    interaction = click()
    await env.cog.approve(interaction, 5)
    env.website.validate_member.assert_not_awaited()
    assert interaction.edit_original_response.await_args.kwargs["content"].endswith("Already handled on the website")


async def test_non_staff_cannot_use_the_buttons(env):
    await pending(env)
    for action in (env.cog.approve, env.cog.ask_reject):
        interaction = click(user=staff(is_staff=False))
        await action(interaction, 5)
        interaction.response.send_message.assert_awaited_once()
        assert "Only staff" in interaction.response.send_message.await_args.args[0]
    env.website.validate_member.assert_not_awaited()


# --- Reject ------------------------------------------------------------------------------------------------------


async def test_reject_asks_for_confirmation_then_deletes(env):
    await pending(env)
    interaction = click()
    await env.cog.ask_reject(interaction, 5)
    ask = interaction.response.send_message.await_args
    assert ask.args[0] == "Delete `NewPilot42`'s website account? This can't be undone."
    assert ask.kwargs["ephemeral"] is True and isinstance(ask.kwargs["view"], rc.ConfirmDelete)
    env.website.delete_member.assert_not_awaited()  # nothing happens without the confirmation

    env.website.get_member.return_value = member(5)
    confirm = click()
    post = interaction.message
    await env.cog.reject(confirm, 5, post)
    env.website.delete_member.assert_awaited_once_with(5)
    edited = post.edit.await_args.kwargs
    assert "❌ Rejected by <@900>" in edited["content"] and "(website account deleted)" in edited["content"]
    assert edited["view"] is None
    assert await env.storage.kv_get("registration:5") is None


async def test_reject_failure_falls_back_to_acp(env):
    await pending(env)
    env.website.get_member.return_value = member(5)
    env.website.delete_member.side_effect = ServiceError("NO_PERMISSION")
    confirm = click()
    post = SimpleNamespace(content="post", edit=AsyncMock())
    await env.cog.reject(confirm, 5, post)
    post.edit.assert_not_awaited()
    assert "didn't accept the deletion" in confirm.edit_original_response.await_args.kwargs["content"]
    assert json.loads(await env.storage.kv_get("registration:5"))["name"] == "NewPilot42"  # still pending


# --- setup and the HTTP side ---------------------------------------------------------------------------------------


async def test_off_unless_enabled_and_channel_set(tmp_path):
    for env_vars in ({}, {"REGISTRATION_APPROVALS": "true", "CHANNEL_SYSTEM_MESSAGES": ""}):
        settings = load_settings(env={**BASE_ENV, "DATA_DIR": str(tmp_path), **env_vars})
        bot = SimpleNamespace(settings=settings, add_cog=AsyncMock())
        await rc.setup(bot)
        bot.add_cog.assert_not_awaited()


async def test_website_client_registration_requests():
    received = []

    async def handler(request: web.Request) -> web.Response:
        route = request.query_string.split("&", 1)[0]
        received.append((request.method, route, dict(request.query), dict(await request.post())))
        if request.method == "DELETE":
            return web.Response(status=200)  # IPS may answer a delete with an empty body
        if route == "/core/members/404":
            return web.json_response({"errorCode": "1C292/2", "errorMessage": "INVALID_ID"}, status=404)
        if route == "/core/members":
            return web.json_response({"page": 1, "totalPages": 1, "results": [member(5)]})
        return web.json_response(member(5, validating=False))

    app = web.Application()
    app.router.add_route("*", "/api/index.php", handler)
    server = TestServer(app)
    await server.start_server()
    async with aiohttp.ClientSession() as session:
        client = WebsiteClient(session, str(server.make_url("/api/index.php")), "key")
        assert [m["id"] for m in await client.recent_members(joined_after=1700000000)] == [5]
        method, route, query, _ = received[-1]
        assert query["sortBy"] == "joined" and query["sortDir"] == "desc" and query["joined_after"] == "1700000000"
        await client.validate_member(5)
        assert received[-1][:2] == ("POST", "/core/members/5") and received[-1][3] == {"validated": "1"}
        await client.delete_member(5)
        assert received[-1][:2] == ("DELETE", "/core/members/5")
        with pytest.raises(WebsiteNotFound):
            await client.get_member(404)
    await server.close()
