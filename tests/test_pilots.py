import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from flyvikingbot import linking as lk
from flyvikingbot.cogs import pilots as pc
from flyvikingbot.config import load_settings
from flyvikingbot.services.fsairlines import FsaNotFound, Pilot, Rank
from flyvikingbot.storage import Storage
from flyvikingbot.webhooks.ips_events import parse_member_profile

from .conftest import BASE_ENV, load_fixture

FSA_FIELD, DISCORD_FIELD = 7, 8


def member_payload(fsa_id="100", discord="joramd", name="JoramD"):
    """Real IPS member shape (from a captured webhook author object) plus the two profile fields."""
    member = load_fixture("real_blogEntry_create")["author"]
    member["name"] = name
    member["customFields"]["3"] = {
        "name": "FlyViking",
        "fields": {
            str(FSA_FIELD): {"name": "FSAirlines Pilot ID", "value": fsa_id},
            str(DISCORD_FIELD): {"name": "Discord username", "value": f"<p>{discord}</p>" if discord else None},
        },
    }
    return member


def test_parse_member_profile_real_shape():
    p = parse_member_profile(member_payload(), FSA_FIELD, DISCORD_FIELD)
    assert p.member_id == 1 and p.name == "JoramD" and p.fsa_pilot_id == "100" and p.discord_name == "joramd"
    assert p.profile_url == "https://flyviking.net/profile/1-joramd/"
    empty = parse_member_profile(member_payload(fsa_id=None, discord=None), FSA_FIELD, DISCORD_FIELD)
    assert empty.fsa_pilot_id is None and empty.discord_name is None
    assert parse_member_profile(member_payload(), None, None).fsa_pilot_id is None


def fsa_pilot(pid="100", **kw):
    base = dict(user="joramd", name="Joram", surname="Davids", rank="Captain", rank_id=2, flights=1, hours=1.0,
                rating=9.0, distance=1.0, location="BIKF", last_active=None)
    return Pilot(id=pid, **{**base, **kw})


def make_member(mid, name, roles=()):
    m = SimpleNamespace(id=mid, name=name, global_name=None, nick=None, display_name=name, mention=f"<@{mid}>",
                        roles=[SimpleNamespace(id=r) for r in roles])
    m.add_roles, m.remove_roles = AsyncMock(), AsyncMock()
    return m


@pytest.fixture
async def env(tmp_path):
    settings = load_settings(
        env={**BASE_ENV, "DATA_DIR": str(tmp_path), "IPS_FIELD_FSA_PILOT_ID": str(FSA_FIELD),
             "IPS_FIELD_DISCORD_NAME": str(DISCORD_FIELD), "CHANNEL_SYSTEM_MESSAGES": "900"},
        credentials_path=tmp_path / "none",
    )
    storage = Storage(tmp_path / "db.sqlite")
    await storage.init()
    joram = make_member(1, "joramd")
    members = [joram, make_member(2, "someone")]
    guild = SimpleNamespace(members=members, get_member=lambda mid: next((m for m in members if m.id == mid), None))
    fsa = SimpleNamespace(pilot=AsyncMock(return_value=fsa_pilot()))
    bot = SimpleNamespace(settings=settings, storage=storage, fsa=fsa, primary_guild=lambda: guild,
                          post_system_message=AsyncMock())
    cog = pc.Pilots(bot)
    cog.set_rank_role = AsyncMock(return_value=None)
    return SimpleNamespace(cog=cog, bot=bot, storage=storage, fsa=fsa, joram=joram)


def profile(**kw):
    return parse_member_profile(member_payload(**kw), FSA_FIELD, DISCORD_FIELD)


async def test_auto_link_and_noop(env):
    d = await env.cog.process_profile(profile(name="Joram Davids"))
    assert d.action is lk.Action.LINK
    link = await env.storage.link_by_discord(1, "fsa")
    assert link.external_id == "100" and link.ips_member_id == 1
    env.cog.set_rank_role.assert_awaited()  # rank role applied right after linking
    assert "✅ Linked <@1>" in env.bot.post_system_message.await_args.args[0]

    env.fsa.pilot.reset_mock()
    again = await env.cog.process_profile(profile(name="Joram Davids"))
    assert again.action is lk.Action.NOOP
    env.fsa.pilot.assert_not_awaited()  # no API call for unrelated profile edits


async def test_name_mismatch_goes_to_staff_with_buttons_then_approve(env):
    d = await env.cog.process_profile(profile(name="Captain Viking"))
    assert d.action is lk.Action.REVIEW
    msg, kwargs = env.bot.post_system_message.await_args.args[0], env.bot.post_system_message.await_args.kwargs
    assert "https://flyviking.net/profile/1-joramd/" in msg and "doesn't match" in msg
    assert [b.custom_id for b in kwargs["view"].children] == ["fvlink:approve:1", "fvlink:reject:1"]
    assert json.loads(await env.storage.kv_get("review:1"))["discord_id"] == 1

    staff = SimpleNamespace(guild_permissions=SimpleNamespace(manage_roles=True), mention="<@99>")
    interaction = MagicMock(user=staff, message=SimpleNamespace(content="review"))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    await env.cog.handle_review(interaction, "approve", 1)
    assert (await env.storage.link_by_discord(1, "fsa")).source == "staff"
    assert "Approved by <@99>" in interaction.edit_original_response.await_args.kwargs["content"]
    assert await env.storage.kv_get("review:1") is None


async def test_review_buttons_require_staff(env):
    user = SimpleNamespace(guild_permissions=SimpleNamespace(manage_roles=False))
    interaction = MagicMock(user=user)
    interaction.response.send_message = AsyncMock()
    await env.cog.handle_review(interaction, "approve", 1)
    assert "Only staff" in interaction.response.send_message.await_args.args[0]


async def test_unknown_discord_user_not_approvable(env):
    d = await env.cog.process_profile(profile(discord="nobody"))
    assert d.action is lk.Action.REVIEW and not d.approvable
    msg, kwargs = env.bot.post_system_message.await_args.args[0], env.bot.post_system_message.await_args.kwargs
    assert "No server member" in msg and "view" not in kwargs and "correct their profile" in msg


async def test_unknown_pilot(env):
    env.fsa.pilot.side_effect = FsaNotFound("nope")
    d = await env.cog.process_profile(profile(name="Joram Davids"))
    assert d.action is lk.Action.REVIEW and "doesn't exist" in d.reasons[0]


async def test_clearing_fields_unlinks(env):
    await env.cog.process_profile(profile(name="Joram Davids"))
    d = await env.cog.process_profile(profile(name="Joram Davids", fsa_id=None, discord=None))
    assert d.action is lk.Action.UNLINK
    assert await env.storage.link_by_discord(1, "fsa") is None


async def test_find_discord_members_fallbacks(env):
    nick = make_member(3, "zz_user")
    nick.nick = "Viking Pilot"
    guild = SimpleNamespace(members=[env.joram, nick])
    assert env.cog.find_discord_members(guild, "@JoramD") == [1]
    assert env.cog.find_discord_members(guild, "viking pilot") == [3]
    assert env.cog.find_discord_members(guild, "") == []


async def test_set_rank_role_swaps_and_reports_missing_role(env):
    cog = pc.Pilots(env.bot)  # real set_rank_role
    env.fsa.ranks = AsyncMock(return_value=[Rank(1, "First Officer", "FO"), Rank(2, "Captain", None),
                                            Rank(3, "Commander", None)])
    roles = {11: "First Officer", 12: "Captain", 50: "Members"}
    member = make_member(1, "joramd", roles=[11, 50])
    member.guild = SimpleNamespace(
        roles=[SimpleNamespace(id=i, name=n, is_default=lambda: False, managed=False) for i, n in roles.items()],
        get_role=lambda i: SimpleNamespace(id=i, name=roles[i]) if i in roles else None,
    )
    change = await cog.set_rank_role(member, 2)
    member.remove_roles.assert_awaited_once()
    assert [r.id for r in member.add_roles.await_args.args] == [12]
    assert change == "<@1>: First Officer → Captain"

    assert await cog.set_rank_role(member, 3, profile_url="https://p/") is None
    msg = env.bot.post_system_message.await_args.args[0]
    assert "Commander" in msg and "https://p/" in msg
    env.bot.post_system_message.reset_mock()
    await cog.set_rank_role(member, 3)
    env.bot.post_system_message.assert_not_awaited()  # reported only once
