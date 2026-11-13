import pytest

from flyvikingbot import linking as lk
from flyvikingbot.services.fsairlines import Pilot, Rank
from flyvikingbot.storage import Link
from flyvikingbot.webhooks.ips_events import MemberProfile


def pilot(**kw):
    base = dict(id="100", user="jdavids", name="Joram", surname="Davids", rank="Captain", rank_id=2, flights=1,
                hours=1.0, rating_total=9.0, distance=1.0, location="BIKF", last_active=None)
    return Pilot(**{**base, **kw})


def profile(**kw):
    base = dict(member_id=10, name="Joram Davids", profile_url="https://flyviking.net/profile/10-x/",
                fsa_pilot_id="100", discord_name="joramd")
    return MemberProfile(**{**base, **kw})


def link(discord_id=1, ips=10, pilot_id="100", discord_name="joramd"):
    return Link(discord_id, ips, "u", discord_name, "fsa", pilot_id, "auto")


def decide(**kw):
    kw.setdefault("profile", profile())
    kw.setdefault("current", None)
    kw.setdefault("pilot", pilot())
    kw.setdefault("discord_matches", [1])
    return lk.decide(lk.LinkInput(**kw))


def test_auto_link():
    d = decide()
    assert d.action is lk.Action.LINK and d.discord_id == 1


@pytest.mark.parametrize("fields", [dict(fsa_pilot_id=None), dict(discord_name=None), dict(fsa_pilot_id=None,
                                                                                            discord_name=None)])
def test_incomplete_fields(fields):
    assert decide(profile=profile(**fields)).action is lk.Action.IGNORE
    assert decide(profile=profile(**fields), current=link()).action is lk.Action.UNLINK


def test_unchanged_is_noop_and_skips_lookup():
    current = link(discord_name="JoramD")
    p = profile(discord_name="joramd ")
    assert not lk.needs_lookup(p, current)
    assert decide(profile=p, current=current, pilot=None, discord_matches=[]).action is lk.Action.NOOP


def test_changed_pilot_needs_lookup():
    assert lk.needs_lookup(profile(fsa_pilot_id="200"), link())


def test_unknown_pilot_not_approvable():
    d = decide(pilot=None)
    assert d.action is lk.Action.REVIEW and not d.approvable and "doesn't exist" in d.reasons[0]


@pytest.mark.parametrize(("matches", "text"), [([], "No server member"), ([1, 2], "several")])
def test_discord_not_resolved(matches, text):
    d = decide(discord_matches=matches)
    assert d.action is lk.Action.REVIEW and not d.approvable and text in d.reasons[0]


def test_name_mismatch_links_with_note():
    d = decide(profile=profile(name="Captain Viking"))
    assert d.action is lk.Action.LINK and d.discord_id == 1 and not d.reasons
    assert "doesn't match" in d.notes[0]


def test_conflict_plus_name_mismatch_reviewed_with_both():
    d = decide(profile=profile(name="Captain Viking"), pilot_owner=link(discord_id=5, ips=50))
    assert d.action is lk.Action.REVIEW and d.approvable
    assert "<@5>" in d.reasons[0] and "doesn't match" in d.reasons[1]


def test_pilot_owned_by_someone_else():
    d = decide(pilot_owner=link(discord_id=5, ips=50))
    assert d.action is lk.Action.REVIEW and d.approvable and "<@5>" in d.reasons[0]


def test_own_existing_link_is_not_a_conflict():
    d = decide(current=link(pilot_id="999"), pilot_owner=None, discord_owner=link(pilot_id="999"))
    assert d.action is lk.Action.LINK


@pytest.mark.parametrize("website_name", ["Joram Davids", "joramdavids", "JDAVIDS", "joram", "Jöram Dávids"])
def test_names_match(website_name):
    assert lk.names_match(pilot(), website_name)


@pytest.mark.parametrize("website_name", ["Someone Else", "", None, "Davids"])
def test_names_do_not_match(website_name):
    assert not lk.names_match(pilot(), website_name)




def test_map_ranks_to_roles():
    ranks = [Rank(1, "Cadet", "CDT"), Rank(2, "First Officer", "FO"), Rank(3, "Captain", None)]
    roles = [lk.RoleRef(11, "cadet"), lk.RoleRef(12, "FO"), lk.RoleRef(99, "Members")]
    assert lk.map_ranks_to_roles(ranks, roles) == {1: 11, 2: 12, 3: None}


def test_role_changes():
    assert lk.role_changes([11, 99], [11, 12], 12) == ({12}, {11})
    assert lk.role_changes([12, 99], [11, 12], 12) == (set(), set())
    assert lk.role_changes([11, 99], [11, 12], None) == (set(), {11})  # unmapped rank: drop rank roles only


def test_all_blocking_problems_reported_together():
    d = decide(pilot=None, discord_matches=[])
    assert d.action is lk.Action.REVIEW and not d.approvable and len(d.reasons) == 2
    assert "doesn't exist" in d.reasons[0] and "No server member" in d.reasons[1]


@pytest.mark.parametrize(
    ("raw", "shown"),
    [("Joram", "`Joram`"), ("[click](https://evil)", "`[click](https://evil)`"), ("a`b", "`a'b`"),
     ("", "`(empty)`"), (None, "`(empty)`"), ("x" * 200, "`" + "x" * 100 + "`"), ("@everyone\nhi", "`@everyone hi`")],
)
def test_code_neutralises_member_text(raw, shown):
    assert lk.code(raw) == shown
