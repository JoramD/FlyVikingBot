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
                rating_total=9.0, distance=1.0, location="BIKF", last_active=None)
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


def fake_guild_member(roles, have):
    member = make_member(1, "joramd", roles=have)
    member.guild = SimpleNamespace(
        roles=[SimpleNamespace(id=i, name=n, is_default=lambda: False, managed=False) for i, n in roles.items()],
        get_role=lambda i: SimpleNamespace(id=i, name=roles[i]) if i in roles else None,
    )
    return member


RANK_ROLES = {11: "Trainee Pilot", 12: "First Officer", 13: "Senior First Officer", 14: "Cargo First Officer",
              15: "Captain", 16: "Cargo Captain", 60: "Board of Directors", 50: "Members"}


async def test_set_rank_role_swaps_roles(env):
    cog = pc.Pilots(env.bot)  # real set_rank_role
    env.fsa.ranks = AsyncMock(return_value=[Rank(1, "First Officer", "FO"), Rank(2, "Captain", None)])
    member = fake_guild_member(RANK_ROLES, have=[12, 50])
    change = await cog.set_rank_role(member, 2)
    assert [r.id for r in member.remove_roles.await_args.args] == [12]
    assert [r.id for r in member.add_roles.await_args.args] == [15]
    assert change == "<@1>: First Officer → Captain"
    env.bot.post_system_message.assert_not_awaited()


async def test_board_rank_removes_pilot_roles_but_never_assigns_board_role(env):
    cog = pc.Pilots(env.bot)
    # An FSA rank that shares its name with a non-allowlisted Discord role
    env.fsa.ranks = AsyncMock(return_value=[Rank(2, "Captain", None), Rank(9, "Board of Directors", "BOD")])
    member = fake_guild_member(RANK_ROLES, have=[15, 16, 60, 50])  # Captain, Cargo Captain, Board, Members
    change = await cog.set_rank_role(member, 9)
    assert {r.id for r in member.remove_roles.await_args.args} == {15, 16}  # only allowlisted rank roles
    member.add_roles.assert_not_awaited()  # Board role is never given out
    assert change.startswith("<@1>: ") and change.endswith(" → none")


async def test_board_promotion_notifies_staff_once_and_again_after_demotion(env):
    cog = pc.Pilots(env.bot)
    env.fsa.ranks = AsyncMock(return_value=[Rank(2, "Captain", None), Rank(9, "Chief Executive", "CEO")])
    member = fake_guild_member(RANK_ROLES, have=[15, 50])
    await cog.set_rank_role(member, 9, profile_url="https://flyviking.net/profile/1-joramd/")
    msg = env.bot.post_system_message.await_args.args[0]
    assert "<@1> is now **Chief Executive**" in msg and "Board of Directors" in msg
    assert "Removed pilot rank role(s): Captain" in msg and "https://flyviking.net/profile/1-joramd/" in msg

    env.bot.post_system_message.reset_mock()
    member.roles = []  # the Captain role was removed
    await cog.set_rank_role(member, 9)  # hourly sync: no repeat
    env.bot.post_system_message.assert_not_awaited()

    await cog.set_rank_role(member, 2)  # back to Captain clears the notice...
    await cog.set_rank_role(member, 9)  # ...so a new promotion is announced again
    assert "Chief Executive" in env.bot.post_system_message.await_args.args[0]


async def test_unknown_rank_keeps_roles(env):
    cog = pc.Pilots(env.bot)
    member = fake_guild_member(RANK_ROLES, have=[15])
    assert await cog.set_rank_role(member, None) is None
    member.remove_roles.assert_not_awaited()


async def test_missing_allowlisted_role_reported_once(env):
    cog = pc.Pilots(env.bot)
    env.fsa.ranks = AsyncMock(return_value=[Rank(2, "Captain", None)])
    roles = {k: v for k, v in RANK_ROLES.items() if v != "Cargo Captain"}
    member = fake_guild_member(roles, have=[])
    await cog.set_rank_role(member, 2)
    assert "'Cargo Captain'" in env.bot.post_system_message.await_args.args[0]
    env.bot.post_system_message.reset_mock()
    await cog.set_rank_role(member, 2)
    env.bot.post_system_message.assert_not_awaited()


async def test_callsign_in_profile_field_resolves_and_is_cached(env):
    env.fsa.resolve_pilot_id = AsyncMock(return_value="100")
    d = await env.cog.process_profile(profile(name="Joram Davids", fsa_id="FVK003"))
    assert d.action is lk.Action.LINK
    assert (await env.storage.link_by_discord(1, "fsa")).external_id == "100"  # numeric ID stored, not FVK003
    env.fsa.pilot.reset_mock()
    again = await env.cog.process_profile(profile(name="Joram Davids", fsa_id="FVK003"))
    assert again.action is lk.Action.NOOP
    env.fsa.resolve_pilot_id.assert_awaited_once()  # cached after the first lookup
    env.fsa.pilot.assert_not_awaited()


async def test_unknown_callsign_reviewed(env):
    env.fsa.resolve_pilot_id = AsyncMock(return_value=None)
    d = await env.cog.process_profile(profile(fsa_id="FVK999"))
    assert d.action is lk.Action.REVIEW and not d.approvable
    assert "`FVK999`" in env.bot.post_system_message.await_args.args[0]
    env.fsa.pilot.assert_not_awaited()


async def test_review_posted_once_per_entered_values(env):
    await env.cog.process_profile(profile(name="Captain Viking"))
    await env.cog.process_profile(profile(name="Captain Viking"))  # unrelated profile save
    assert env.bot.post_system_message.await_count == 1
    await env.cog.process_profile(profile(name="Captain Viking", fsa_id="200"))  # changed values: new review
    assert env.bot.post_system_message.await_count == 2


async def test_approved_callsign_review_stores_numeric_id(env):
    env.fsa.resolve_pilot_id = AsyncMock(return_value="100")
    await env.cog.process_profile(profile(name="Captain Viking", fsa_id="FVK003"))
    assert json.loads(await env.storage.kv_get("review:1"))["fsa_pilot_id"] == "100"
    assert "`FVK003`" in env.bot.post_system_message.await_args.args[0]  # shows what the member typed


async def test_on_demand_lookup_links_profile_saved_while_offline(env):
    env.bot.website = SimpleNamespace(list_members=AsyncMock(return_value=[
        member_payload(name="Someone", fsa_id="555", discord="someoneelse"),
        member_payload(name="Joram Davids"),
    ]))
    user = SimpleNamespace(id=1, name="joramd")
    link, note = await env.cog.link_from_website(user)
    assert link is not None and link.external_id == "100" and note is None
    env.fsa.pilot.assert_awaited_once()  # only the matching profile was processed

    # Cooldown: an immediate second lookup doesn't hit the website again
    await env.storage.remove_member(1)
    assert await env.cog.link_from_website(user) == (None, None)
    env.bot.website.list_members.assert_awaited_once()


async def test_on_demand_lookup_explains_pending_review(env):
    env.bot.website = SimpleNamespace(list_members=AsyncMock(return_value=[member_payload(name="Captain Viking")]))
    link, note = await env.cog.link_from_website(SimpleNamespace(id=1, name="joramd"))
    assert link is None and "needs a quick check by staff" in note and "doesn't match" in note


async def test_on_demand_lookup_permission_error_reported_once(env):
    from flyvikingbot.services.http import ServiceError

    env.bot.website = SimpleNamespace(list_members=AsyncMock(side_effect=ServiceError("NO_PERMISSION")))
    await env.cog.link_from_website(SimpleNamespace(id=1, name="joramd"))
    await env.cog.link_from_website(SimpleNamespace(id=2, name="someone"))
    env.bot.post_system_message.assert_awaited_once()
    assert "GET /core/members" in env.bot.post_system_message.await_args.args[0]


async def test_sync_rank_member_links_on_demand(env):
    env.bot.website = SimpleNamespace(list_members=AsyncMock(return_value=[member_payload(name="Joram Davids")]))
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    await env.cog.sync_rank.callback(env.cog, interaction, member=env.joram)
    assert (await env.storage.link_by_discord(1, "fsa")).external_id == "100"
    env.bot.website.list_members.assert_awaited_once()


async def test_other_commands_do_not_look_up_links(env):
    # Only /pilot and /sync_rank need link info; no global per-command hook exists
    assert not hasattr(env.cog, "on_app_command_completion")


async def test_ensure_link_rule(env):
    env.bot.website = SimpleNamespace(list_members=AsyncMock(return_value=[member_payload(name="Joram Davids")]))
    user = SimpleNamespace(id=1, name="joramd")
    link, _ = await env.cog.ensure_link(user)  # missing -> fetched from the website
    assert link.external_id == "100"
    env.bot.website.list_members.reset_mock()
    link, _ = await env.cog.ensure_link(user)  # stored -> no website call
    assert link.external_id == "100"
    env.bot.website.list_members.assert_not_awaited()
    stranger = SimpleNamespace(id=9, name="stranger")
    await env.cog.ensure_link(stranger)
    await env.cog.ensure_link(stranger)  # within the cooldown -> one lookup only
    env.bot.website.list_members.assert_awaited_once()


async def test_system_message_action_flags(env):
    await env.cog.process_profile(profile(name="Joram Davids"))  # auto-link: informational
    assert env.bot.post_system_message.await_args.kwargs["action_required"] is False
    await env.cog.process_profile(profile(name="Joram Davids", fsa_id=None, discord=None))  # unlink: informational
    assert env.bot.post_system_message.await_args.kwargs["action_required"] is False
    await env.cog.process_profile(profile(name="Captain Viking"))  # review: staff must act
    assert env.bot.post_system_message.await_args.kwargs["action_required"] is True
    await env.cog.report_once("x", "problem")  # every reported problem needs action
    assert env.bot.post_system_message.await_args.kwargs["action_required"] is True
