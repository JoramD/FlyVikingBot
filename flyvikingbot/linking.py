"""Pure decision logic for account linking and rank roles (no Discord or API calls)."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

from .services.fsairlines import Pilot, Rank
from .storage import Link
from .webhooks.ips_events import MemberProfile

SERVICE_FSA = "fsa"


def code(text: str | None, limit: int = 100) -> str:
    """Show text a member typed (names, profile fields) as inline code. Inside a code span Discord
    renders no markdown, links or mentions, so a crafted name can't masquerade as something else."""
    cleaned = " ".join((text or "").replace("`", "'").split())[:limit]
    return f"`{cleaned}`" if cleaned else "`(empty)`"


def normalize(text: str | None) -> str:
    """Lowercase, strip accents and collapse whitespace: 'Jöram  Davids' -> 'joram davids'."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.casefold().split())


def _compact(text: str | None) -> str:
    return re.sub(r"[^0-9a-z]", "", normalize(text))


def names_match(pilot: Pilot, website_name: str | None) -> bool:
    """Loose match between an FSA pilot and a website display name.

    Matches the FSA username, callsign, 'name surname' or first name, ignoring case, accents,
    spaces and punctuation ('Joram Davids' == 'JoramDavids' == 'joram.davids').
    """
    target = _compact(website_name)
    if not target:
        return False
    candidates = {pilot.user, pilot.callsign, pilot.full_name, pilot.name, f"{pilot.surname or ''} {pilot.name or ''}"}
    return any(_compact(c) == target for c in candidates if _compact(c))


# --- link decisions ------------------------------------------------------------


class Action(Enum):
    IGNORE = "ignore"  # nothing to do (fields empty or incomplete, not linked)
    NOOP = "noop"  # already linked with these values
    LINK = "link"  # link automatically
    UNLINK = "unlink"  # fields cleared: remove the existing link
    REVIEW = "review"  # needs staff: post to #system-messages


@dataclass(frozen=True)
class LinkInput:
    profile: MemberProfile
    current: Link | None  # existing link of this website member
    pilot: Pilot | None  # None if the pilot ID doesn't exist in FSA
    discord_matches: list[int] = field(default_factory=list)  # Discord IDs matching the entered username
    pilot_owner: Link | None = None  # whoever currently has this FSA pilot linked
    discord_owner: Link | None = None  # whoever currently has the matched Discord account linked


@dataclass(frozen=True)
class Decision:
    action: Action
    reasons: tuple[str, ...] = ()
    discord_id: int | None = None  # set for LINK, and for REVIEW when staff could approve
    notes: tuple[str, ...] = ()  # informational remarks on a LINK (e.g. names differ)

    @property
    def approvable(self) -> bool:
        return self.action is Action.REVIEW and self.discord_id is not None


def needs_lookup(profile: MemberProfile, current: Link | None) -> bool:
    """False when the decision can be made without calling FSA or searching Discord."""
    return bool(profile.fsa_pilot_id and profile.discord_name) and not _unchanged(profile, current)


def _unchanged(profile: MemberProfile, current: Link | None) -> bool:
    return (
        current is not None
        and current.external_id == (profile.fsa_pilot_id or "").strip()
        and (current.discord_name or "").strip().casefold() == (profile.discord_name or "").strip().casefold()
    )


def decide(inp: LinkInput) -> Decision:
    p = inp.profile
    if not (p.fsa_pilot_id and p.discord_name):
        # Both fields are needed; clearing either one removes an existing link
        return Decision(Action.UNLINK) if inp.current else Decision(Action.IGNORE)
    if _unchanged(p, inp.current):
        return Decision(Action.NOOP)

    # Report every problem with the entered values in one go, not one per profile save
    blocking = []
    if inp.pilot is None:
        blocking.append(f"FSAirlines pilot {code(p.fsa_pilot_id)} doesn't exist.")
    if not inp.discord_matches:
        blocking.append(f"No server member found with Discord username {code(p.discord_name)}.")
    elif len(inp.discord_matches) > 1:
        blocking.append(f"Discord name {code(p.discord_name)} matches several server members.")
    if blocking:
        return Decision(Action.REVIEW, tuple(blocking))

    discord_id = inp.discord_matches[0]
    conflicts = []  # linking anyway would take an account away from someone: staff must approve
    if inp.pilot_owner and inp.pilot_owner.ips_member_id != p.member_id:
        conflicts.append(f"FSAirlines pilot {code(inp.pilot.id)} is already linked to <@{inp.pilot_owner.discord_id}>.")
    if inp.discord_owner and inp.discord_owner.ips_member_id != p.member_id:
        conflicts.append(f"<@{discord_id}> is already linked to another website account.")
    notes = []  # worth knowing, but not a reason to block the link
    if not names_match(inp.pilot, p.name):
        notes.append(f"FSAirlines name {code(inp.pilot.full_name)} doesn't match website name {code(p.name)}.")
    if conflicts:
        return Decision(Action.REVIEW, tuple(conflicts + notes), discord_id)
    return Decision(Action.LINK, (), discord_id, tuple(notes))


# --- rank roles ------------------------------------------------------------------


@dataclass(frozen=True)
class RoleRef:
    id: int
    name: str


def map_ranks_to_roles(ranks: Iterable[Rank], roles: Iterable[RoleRef]) -> dict[int, int | None]:
    """rank_id -> Discord role ID (or None if no role matches the rank's name or short name)."""
    by_name: dict[str, int] = {}
    for role in roles:
        by_name.setdefault(normalize(role.name), role.id)
    return {
        rank.id: by_name.get(normalize(rank.name)) or (by_name.get(normalize(rank.short)) if rank.short else None)
        for rank in ranks
    }


def role_changes(member_role_ids: Iterable[int], rank_role_ids: Iterable[int], target: int | None):
    """Return (to_add, to_remove) so the member holds exactly the target rank role.

    With no target (a rank without an automatic role, e.g. Board of Directors) all
    managed rank roles are removed; roles outside the managed set are never touched.
    """
    have = set(member_role_ids)
    managed = have & set(rank_role_ids)
    if target is None:
        return set(), managed
    return {target} - have, managed - {target}
