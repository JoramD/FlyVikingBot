"""FSAirlines pilots: /pilot, automatic account linking from website profiles, and rank roles."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import linking as lk
from ..constants import BRAND_COLOUR
from ..services.fsairlines import FsaNotFound, Pilot
from ..services.http import ServiceError
from ..services.website import member_group_ids
from ..storage import Link
from ..system_messages import is_staff
from ..webhooks import ips_events as ev
from .flight_logs import flight_title, format_hours

if TYPE_CHECKING:
    from ..bot import FlyVikingBot

log = logging.getLogger(__name__)

REVIEW_KEY = "review:{}"  # pending staff review, keyed by website member ID
REVIEW_SENT_KEY = "review-sent:{}:{}"  # (member ID, entered values) already posted for review
CALLSIGN_KEY = "fsa-callsign:{}"  # callsign/username -> pilot ID cache
LOOKUP_COOLDOWN_SECONDS = 60  # on-demand website lookups per Discord user
REPORTED_KEY = "reported:{}"  # problems already reported to staff (cleared once resolved)
GROUP_CACHE_SECONDS = 3600


def _profile_link(url: str | None) -> str:
    return f"[website profile](<{url}>)" if url else "website profile unknown"


# --- staff review buttons (persistent across restarts) ---------------------------------------------


REVIEW_TEMPLATE = r"fvlink:(?P<action>approve|reject):(?P<ips>\d+)"


class ReviewButton(discord.ui.DynamicItem[discord.ui.Button], template=REVIEW_TEMPLATE):
    def __init__(self, action: str, ips_member_id: int) -> None:
        approve = action == "approve"
        super().__init__(
            discord.ui.Button(
                label="Approve" if approve else "Reject",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.secondary,
                custom_id=f"fvlink:{action}:{ips_member_id}",
            )
        )
        self.action = action
        self.ips_member_id = ips_member_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match["action"], int(match["ips"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Pilots")
        if cog is None:
            await interaction.response.send_message("Linking is currently disabled.", ephemeral=True)
            return
        await cog.handle_review(interaction, self.action, self.ips_member_id)


def review_view(ips_member_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ReviewButton("approve", ips_member_id))
    view.add_item(ReviewButton("reject", ips_member_id))
    return view


# --- embeds ----------------------------------------------------------------------------------------


def pilot_embed(pilot: Pilot, mention: str | None, last_flight: Any | None, rank_name: str | None = None
                ) -> discord.Embed:
    title = f"{pilot.full_name} · {pilot.callsign}" if pilot.callsign else pilot.full_name
    embed = discord.Embed(title=title, colour=BRAND_COLOUR)
    if mention:
        embed.description = mention
    if pilot.picture_url:
        embed.set_thumbnail(url=pilot.picture_url)
    avg = pilot.average_rating
    for name, value in (
        ("Rank", rank_name or pilot.rank),
        ("Hours", format_hours(pilot.hours)),
        ("Flights", f"{pilot.flights:,}" if pilot.flights is not None else None),
        ("Avg. rating", f"{avg:.1f}%" if avg is not None else None),
        ("Distance", f"{pilot.distance:,.0f} nm" if pilot.distance is not None else None),
        ("Passengers", f"{pilot.pax:,}" if pilot.pax else None),
        ("Cargo", f"{pilot.cargo_kg:,} kg" if pilot.cargo_kg else None),
        ("Location", pilot.location),
        ("Last active", discord.utils.format_dt(pilot.last_active, "R") if pilot.last_active else None),
    ):
        if value:
            embed.add_field(name=name, value=value, inline=True)
    if last_flight:
        parts = [flight_title(last_flight)]
        if last_flight.aircraft:
            parts.append(last_flight.aircraft)
        if last_flight.completed_at:
            parts.append(discord.utils.format_dt(last_flight.completed_at, "d"))
        if last_flight.landing_rate is not None:
            parts.append(f"{last_flight.landing_rate:+d} fpm")
        embed.add_field(name="Last flight", value=" · ".join(parts), inline=False)
    footer = f"FSAirlines pilot {pilot.id}"
    if pilot.fsa_title:
        footer += f" · {pilot.fsa_title}"
    embed.set_footer(text=footer)
    return embed


# --- cog -------------------------------------------------------------------------------------------


class Pilots(commands.Cog):
    def __init__(self, bot: FlyVikingBot) -> None:
        self.bot = bot
        self.settings = bot.settings
        self._groups: tuple[float, list[tuple[int, str]]] | None = None
        self._lookups: dict[int, float] = {}  # Discord user ID -> last on-demand website lookup
        self._profile_locks: dict[int, asyncio.Lock] = {}  # website member ID -> serialises processing

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(ReviewButton)
        self.rank_sync.change_interval(minutes=self.settings.fsa_rank_sync_minutes)
        self.rank_sync.start()

    async def cog_unload(self) -> None:
        self.rank_sync.cancel()
        self.bot.remove_dynamic_items(ReviewButton)

    @property
    def rank_roles_enabled(self) -> bool:
        """Rank roles and website rank groups are only managed when FSA_RANK_ROLES lists some."""
        return bool(self.settings.fsa_rank_roles)

    @property
    def linking_enabled(self) -> bool:
        return bool(self.settings.ips_field_fsa_pilot_id and self.settings.ips_field_discord_name)

    # --- staff notifications ----------------------------------------------------------------------------------------

    async def report_once(self, key: str, message: str) -> None:
        storage_key = REPORTED_KEY.format(key)
        if await self.bot.storage.kv_get(storage_key) is None:
            # Everything reported via report_once is a problem staff must act on
            await self.bot.post_system_message(message, action_required=True)
            await self.bot.storage.kv_set(storage_key, "1")

    async def resolved(self, key: str) -> None:
        await self.bot.storage.kv_delete(REPORTED_KEY.format(key))

    # --- linking ------------------------------------------------------------------------------------------------------

    def find_discord_members(self, guild: discord.Guild, entered: str) -> list[int]:
        username = (entered or "").strip().casefold()  # Discord usernames are lowercase; members may type capitals
        if not username:
            return []
        exact = [m.id for m in guild.members if m.name.casefold() == username]
        if exact:
            return exact
        wanted = lk.normalize(entered)
        return [
            m.id
            for m in guild.members
            if wanted in {lk.normalize(m.global_name), lk.normalize(m.nick), lk.normalize(m.display_name)}
        ]

    @commands.Cog.listener()
    async def on_ips_webhook(self, event: str, payload: Any) -> None:
        if event not in ev.MEMBER_EVENTS or not self.linking_enabled:
            return
        await self.bot.wait_until_ready()  # the server (guild) isn't known until the bot has connected
        if ev.only_bookkeeping_changes(payload):
            log.debug("Ignoring member_edited with only bookkeeping changes (e.g. a profile view)")
            return
        profile = ev.parse_member_profile(
            payload, self.settings.ips_field_fsa_pilot_id, self.settings.ips_field_discord_name
        )
        if profile is None:
            return
        try:
            await self.process_profile(profile)
        except Exception:
            log.exception("Linking failed for website member %s", profile.member_id)

    async def ensure_link(self, user: discord.abc.User) -> tuple[Link | None, str | None]:
        """The rule for any feature that *requires* a member's link: use stored data, and if it's missing,
        fetch it from the website (when possible, rate-limited by the per-user cooldown). Returns (link, note
        for the member). Features that need the link must use this rather than reading storage directly, so
        missed webhooks heal themselves. Purely optional lookups (e.g. an @mention in a flight log) may read
        storage directly."""
        link = await self.bot.storage.link_by_discord(user.id, lk.SERVICE_FSA)
        if link is not None:
            return link, None
        return await self.link_from_website(user)

    async def link_from_website(self, user: discord.abc.User) -> tuple[Link | None, str | None]:
        """On-demand catch-up for an unlinked Discord user: find website members whose "Discord username"
        field names this user and run them through the normal linking. Covers profiles saved while the bot
        was offline. Returns (link or None, explanation for the user or None)."""
        if not self.linking_enabled or self.bot.website is None:
            return None, None
        now = time.monotonic()
        if now - self._lookups.get(user.id, 0) < LOOKUP_COOLDOWN_SECONDS:
            return None, None
        self._lookups[user.id] = now

        wanted = user.name.casefold()
        try:
            members = await self.bot.website.list_members()
        except ServiceError as exc:
            log.warning("Website member lookup failed: %s", exc)
            await self.report_once(
                "website-members",
                f"⚠️ Couldn't search website members ({exc}). Allow the website API key to use GET /core/members, "
                "so members who filled in their profile while the bot was offline get linked.",
            )
            return None, None
        await self.resolved("website-members")

        note = None
        for data in members:
            profile = ev.parse_member_profile(
                data, self.settings.ips_field_fsa_pilot_id, self.settings.ips_field_discord_name
            )
            if profile is None or (profile.discord_name or "").strip().casefold() != wanted:
                continue
            decision = await self.process_profile(profile)
            if decision.action is lk.Action.REVIEW:
                reasons = "\n".join(f"• {r}" for r in decision.reasons)
                note = f"Your flyviking.net profile was found, but the link needs a quick check by staff:\n{reasons}"
        return await self.bot.storage.link_by_discord(user.id, lk.SERVICE_FSA), note

    async def resolve_entered_pilot(self, entered: str | None) -> str | None:
        """Pilot ID for what the member typed (ID, callsign or FSA username). Callsign lookups are cached,
        because member_edited fires on every profile save. None if it can't be resolved."""
        if not entered or not entered.strip():
            return None
        value = entered.strip()
        if value.isdigit():
            return value
        cache_key = CALLSIGN_KEY.format(value.casefold())
        if cached := await self.bot.storage.kv_get(cache_key):
            return cached
        try:
            pilot_id = await self.bot.fsa.resolve_pilot_id(value)
        except ServiceError as exc:
            log.warning("Couldn't resolve FSA callsign %r: %s", value, exc)
            return None
        if pilot_id:
            await self.bot.storage.kv_set(cache_key, pilot_id)
        return pilot_id

    async def process_profile(self, profile: ev.MemberProfile) -> lk.Decision:
        # IPS can deliver several member_edited events for one save; handle one member at a time so the
        # "already posted" checks see each other's results (no duplicate staff posts or double links)
        lock = self._profile_locks.setdefault(profile.member_id, asyncio.Lock())
        async with lock:
            return await self._process_profile(profile)

    async def _process_profile(self, profile: ev.MemberProfile) -> lk.Decision:
        storage, guild = self.bot.storage, self.bot.primary_guild()
        current = await storage.link_by_ips_member(profile.member_id, lk.SERVICE_FSA)
        entered = profile.fsa_pilot_id
        resolved = await self.resolve_entered_pilot(entered)
        if resolved:
            profile = dataclasses.replace(profile, fsa_pilot_id=resolved)
        inp = lk.LinkInput(profile=profile, current=current, pilot=None)

        if lk.needs_lookup(profile, current):
            if guild is None:
                log.warning("Can't link: bot isn't in exactly one guild and GUILD_ID isn't set")
                return lk.Decision(lk.Action.IGNORE)
            pilot_id = profile.fsa_pilot_id.strip()
            pilot = None
            if resolved:
                try:
                    pilot = await self.bot.fsa.pilot(pilot_id)
                except FsaNotFound:
                    pilot = None
            matches = self.find_discord_members(guild, profile.discord_name)
            inp = lk.LinkInput(
                profile=profile,
                current=current,
                pilot=pilot,
                discord_matches=matches,
                pilot_owner=await storage.link_by_external(lk.SERVICE_FSA, pilot_id),
                discord_owner=await storage.link_by_discord(matches[0], lk.SERVICE_FSA) if len(matches) == 1 else None,
            )

        decision = lk.decide(inp)
        log.info("Website member %s: %s %s", profile.member_id, decision.action.value, "; ".join(decision.reasons))

        if decision.action in (lk.Action.LINK, lk.Action.UNLINK):
            await self.clear_review_markers(profile.member_id)
        if decision.action is lk.Action.LINK:
            await self.apply_link(profile, decision.discord_id, source="auto", pilot=inp.pilot)
            await self.bot.post_system_message(
                f"✅ Linked <@{decision.discord_id}> to FSAirlines pilot {lk.code(inp.pilot.id)} "
                f"({lk.code(inp.pilot.full_name)}) "
                f"· {_profile_link(profile.profile_url)}"
                + "".join(f"\nℹ️ {note}" for note in decision.notes),
                action_required=False,  # notes are for awareness only
            )
        elif decision.action is lk.Action.UNLINK:
            await self.apply_unlink(current)
            await self.bot.post_system_message(
                f"Unlinked <@{current.discord_id}> from FSAirlines (profile fields cleared) "
                f"· {_profile_link(profile.profile_url)}",
                action_required=False,
            )
        elif decision.action is lk.Action.REVIEW:
            # Post each distinct problem once, not on every unrelated profile save
            entered_values = f"{(entered or '').strip()}|{(profile.discord_name or '').strip()}".casefold()
            marker = REVIEW_SENT_KEY.format(profile.member_id, entered_values)
            if await storage.kv_get(marker) is None:
                await self.clear_review_markers(profile.member_id)
                await storage.kv_set(marker, "1")
                await self.request_review(profile, decision, entered=entered)
        return decision

    async def clear_review_markers(self, ips_member_id: int) -> None:
        for key in await self.bot.storage.kv_keys(REVIEW_SENT_KEY.format(ips_member_id, "")):
            await self.bot.storage.kv_delete(key)

    async def request_review(
        self, profile: ev.MemberProfile, decision: lk.Decision, entered: str | None = None
    ) -> None:
        """Post a staff review. `profile` carries the resolved pilot ID; `entered` is what the member typed."""
        reasons = "\n".join(f"• {r}" for r in decision.reasons)
        header = (
            f"⚠️ **Couldn't link website member {lk.code(profile.name)} automatically** "
            f"· {_profile_link(profile.profile_url)}\n"
            f"Entered: FSAirlines pilot {lk.code(entered or profile.fsa_pilot_id)}, "
            f"Discord {lk.code(profile.discord_name)}\n"
            f"{reasons}"
        )
        if not decision.approvable:
            # Staff need to tell the member: the bot can't reach them about this
            await self.bot.post_system_message(
                header + "\nThe member needs to correct their profile fields.", action_required=True
            )
            return
        review = {
            "discord_id": decision.discord_id,
            "fsa_pilot_id": profile.fsa_pilot_id.strip(),
            "profile_url": profile.profile_url,
            "discord_name": profile.discord_name,
            "name": profile.name,
        }
        await self.bot.storage.kv_set(REVIEW_KEY.format(profile.member_id), json.dumps(review))
        await self.bot.post_system_message(
            header + f"\nApprove to link <@{decision.discord_id}> anyway.",
            action_required=True,
            view=review_view(profile.member_id),
        )

    async def handle_review(self, interaction: discord.Interaction, action: str, ips_member_id: int) -> None:
        if not is_staff(interaction.user, self.settings.system_messages_role_id):
            await interaction.response.send_message("Only staff can do this.", ephemeral=True)
            return
        key = REVIEW_KEY.format(ips_member_id)
        raw = await self.bot.storage.kv_get(key)
        original = interaction.message.content if interaction.message else ""
        if raw is None:
            await interaction.response.edit_message(content=original + "\n*(already handled)*", view=None)
            return
        review = json.loads(raw)
        await self.bot.storage.kv_delete(key)

        if action == "reject":
            await interaction.response.edit_message(
                content=original + f"\n❌ Rejected by {interaction.user.mention}", view=None
            )
            return

        await interaction.response.defer()
        profile = ev.MemberProfile(
            member_id=ips_member_id,
            name=review.get("name"),
            profile_url=review.get("profile_url"),
            fsa_pilot_id=review["fsa_pilot_id"],
            discord_name=review.get("discord_name"),
        )
        try:
            pilot = await self.bot.fsa.pilot(profile.fsa_pilot_id)
        except ServiceError:
            pilot = None
        await self.apply_link(profile, int(review["discord_id"]), source="staff", pilot=pilot)
        await interaction.edit_original_response(
            content=original + f"\n✅ Approved by {interaction.user.mention}", view=None
        )

    async def apply_link(self, profile: ev.MemberProfile, discord_id: int, source: str, pilot: Pilot | None) -> None:
        storage, pilot_id = self.bot.storage, profile.fsa_pilot_id.strip()
        # Links that this one replaces, captured before saving (their website accounts lose rank groups)
        previous = [
            link
            for link in (
                await storage.link_by_external(lk.SERVICE_FSA, pilot_id),
                await storage.link_by_discord(discord_id, lk.SERVICE_FSA),
            )
            if link and link.ips_member_id not in (None, profile.member_id)
        ]
        displaced = await storage.save_link(
            discord_id=discord_id,
            ips_member_id=profile.member_id,
            ips_profile_url=profile.profile_url,
            discord_name=profile.discord_name,
            service=lk.SERVICE_FSA,
            external_id=pilot_id,
            source=source,
        )
        guild = self.bot.primary_guild()
        for other in displaced:
            await self.sync_ranks(guild.get_member(other) if guild else None, None, None, clear=True)
        for old in previous:
            await self.sync_ranks(None, old.ips_member_id, None, clear=True)
        if pilot:
            member = guild.get_member(discord_id) if guild else None
            await self.sync_ranks(member, profile.member_id, pilot.rank_id, profile_url=profile.profile_url)

    async def apply_unlink(self, link: Link) -> None:
        guild = self.bot.primary_guild()
        member = guild.get_member(link.discord_id) if guild else None
        await self.sync_ranks(member, link.ips_member_id, None, clear=True)
        await self.bot.storage.remove_member(link.discord_id)

    # --- rank roles ---------------------------------------------------------------------------------------------------

    async def rank_role_map(self, guild: discord.Guild) -> tuple[dict[int, int | None], set[int]]:
        """Map FSA ranks to Discord roles, considering only the roles in FSA_RANK_ROLES.

        Returns (rank_id -> role ID or None, IDs of all allowlisted roles). Other roles
        (e.g. Board of Directors) are never assigned or removed by the bot, even if an
        FSA rank happens to share their name.
        """
        ranks = await self.bot.fsa.ranks()
        allowed = {lk.normalize(name): name for name in self.settings.fsa_rank_roles}
        roles = [
            lk.RoleRef(r.id, r.name)
            for r in guild.roles
            if not r.is_default() and not r.managed and lk.normalize(r.name) in allowed
        ]
        missing = allowed.keys() - {lk.normalize(r.name) for r in roles}
        for key in sorted(missing):
            await self.report_once(
                f"rank-role-missing:{key}",
                f"⚠️ Rank role '{allowed[key]}' is listed in FSA_RANK_ROLES but doesn't exist in Discord.",
            )
        return lk.map_ranks_to_roles(ranks, roles), {r.id for r in roles}

    async def sync_ranks(
        self,
        member: discord.Member | None,
        ips_member_id: int | None,
        rank_id: int | None,
        *,
        clear: bool = False,
        profile_url: str | None = None,
    ) -> str | None:
        """Sync a pilot's rank to their Discord roles and website groups. Returns a change summary.

        Does nothing while FSA_RANK_ROLES is empty: with no automatic roles every rank would look like a
        Board rank, which would strip roles and post a Board notice for every pilot."""
        if not self.rank_roles_enabled:
            return None
        changes = []
        if member is not None and (
            change := await self.set_rank_role(member, rank_id, clear=clear, profile_url=profile_url)
        ):
            changes.append(change)
        if ips_member_id is not None:
            label = member.mention if member else f"website member {ips_member_id}"
            if change := await self.set_website_groups(ips_member_id, rank_id, clear=clear, label=label):
                changes.append(change)
        return "; ".join(changes) or None

    async def website_group_map(self) -> tuple[dict[int, int | None], set[int]]:
        """Like rank_role_map, for website member groups (cached for an hour)."""
        now = time.monotonic()
        if self._groups is None or now - self._groups[0] > GROUP_CACHE_SECONDS:
            self._groups = (now, await self.bot.website.list_groups())
        allowed = {lk.normalize(name): name for name in self.settings.fsa_rank_roles}
        groups = [lk.RoleRef(gid, name) for gid, name in self._groups[1] if lk.normalize(name) in allowed]
        for key in sorted(allowed.keys() - {lk.normalize(g.name) for g in groups}):
            await self.report_once(
                f"rank-group-missing:{key}",
                f"⚠️ Rank group '{allowed[key]}' (FSA_RANK_ROLES) doesn't exist on the website. "
                f"Create a secondary member group with that name.",
            )
        return lk.map_ranks_to_roles(await self.bot.fsa.ranks(), groups), {g.id for g in groups}

    async def set_website_groups(
        self, ips_member_id: int, rank_id: int | None, *, clear: bool = False, label: str = ""
    ) -> str | None:
        """Mirror the FSA rank to the member's website groups.

        - Secondary groups: exactly one allowlisted rank group (none for Board ranks or on unlink).
        - Primary group: set to Members for pilots on an automatic rank, unless it's a protected group
          (WEBSITE_PROTECTED_GROUPS: Administrators, Moderators). The old primary group (e.g. Painter/Modder)
          is kept as a secondary group. Board ranks, unlinks and unlinked members never change the primary.
        """
        if not self.settings.website_rank_sync or (rank_id is None and not clear):
            return None
        try:
            mapping, managed = await self.website_group_map()
            primary, secondary = member_group_ids(await self.bot.website.get_member(ips_member_id))
            target = None if clear else mapping.get(rank_id)

            new_primary, kept = primary, set()
            if target is not None:
                members_id, protected, not_kept = await self.website_primary_rules()
                if members_id is not None and primary not in protected and primary != members_id:
                    new_primary = members_id
                    # Keep the old primary (e.g. Painter/Modder) as a secondary group so no permissions are lost
                    if primary is not None and primary not in managed and primary not in not_kept:
                        kept = {primary}
            secondary_target = None if target == new_primary else target  # no duplicate of the primary
            to_add, to_remove = lk.role_changes(secondary | kept, managed, secondary_target)
            to_add |= kept - secondary
            new_secondary = ((secondary | kept) - to_remove) | to_add
            if new_secondary != secondary or new_primary != primary:
                await self.bot.website.update_member_groups(
                    ips_member_id,
                    primary=new_primary if new_primary != primary else None,
                    secondary=sorted(new_secondary) if new_secondary != secondary else None,
                )
        except ServiceError as exc:
            await self.report_once(
                "website-groups",
                f"⚠️ Couldn't update website rank groups ({exc}). Check that the website API key may use "
                "GET /core/groups and GET + POST /core/members/{id}.",
            )
            return None
        await self.resolved("website-groups")
        if not to_add and not to_remove and new_primary == primary:
            return None
        names = dict(self._groups[1]) if self._groups else {}

        def names_of(ids: set[int]) -> str:
            return ", ".join(names.get(i, str(i)) for i in sorted(ids)) or "none"

        parts = []
        if to_add or to_remove:
            parts.append(f"{names_of(to_remove)} → {names_of(to_add)}")
        if new_primary != primary:
            parts.append(f"primary group {names_of({primary} if primary else set())} → {names_of({new_primary})}")
        return f"{label} (website): " + "; ".join(parts)

    async def website_primary_rules(self) -> tuple[int | None, set[int], set[int]]:
        """(ID of the Members group, IDs of protected primary groups, IDs never kept as secondary), by name."""
        by_name = {lk.normalize(name): gid for gid, name in (self._groups[1] if self._groups else [])}
        members_id = by_name.get(lk.normalize(self.settings.website_member_group))
        if members_id is None:
            await self.report_once(
                "member-group-missing",
                f"⚠️ Website group '{self.settings.website_member_group}' (WEBSITE_MEMBER_GROUP) doesn't exist, "
                "so pilots' primary groups aren't being set.",
            )
        protected = {gid for name in self.settings.website_protected_groups if (gid := by_name.get(lk.normalize(name)))}
        not_kept = {gid for name in ("Guests",) if (gid := by_name.get(lk.normalize(name)))}
        return members_id, protected, not_kept

    async def set_rank_role(
        self, member: discord.Member, rank_id: int | None, *, clear: bool = False, profile_url: str | None = None
    ) -> str | None:
        """Give the member the role for rank_id, or remove all rank roles if clear.

        A rank without an automatic role (e.g. Board of Directors) removes the member's
        allowlisted rank roles; staff assign Board roles manually. Returns a change summary.
        """
        if rank_id is None and not clear:
            return None  # FSA gave no rank: don't strip roles on incomplete data
        try:
            mapping, rank_role_ids = await self.rank_role_map(member.guild)
        except ServiceError as exc:
            log.warning("Can't load FSA ranks: %s", exc)
            return None

        target = None if clear else mapping.get(rank_id)
        to_add, to_remove = lk.role_changes((r.id for r in member.roles), rank_role_ids, target)
        guild = member.guild

        def names_of(ids: set[int]) -> str:
            return ", ".join(r.name for i in ids if (r := guild.get_role(i))) or "none"

        if to_add or to_remove:
            try:
                if to_remove:
                    await member.remove_roles(*(guild.get_role(r) for r in to_remove if guild.get_role(r)),
                                              reason="FSAirlines rank sync")
                if to_add:
                    await member.add_roles(*(guild.get_role(r) for r in to_add if guild.get_role(r)),
                                           reason="FSAirlines rank sync")
            except discord.Forbidden:
                await self.report_once(
                    f"rank-forbidden:{member.id}",
                    f"⚠️ Can't update rank roles for {member.mention}: missing permission, or the bot's role is "
                    f"below the rank roles. Move the bot's role above them. · {_profile_link(profile_url)}",
                )
                return None
            await self.resolved(f"rank-forbidden:{member.id}")

        if not clear and target is None:
            await self.notify_board_rank(member, rank_id, names_of(to_remove) if to_remove else None, profile_url)
        else:
            await self.clear_board_notices(member.id)

        if not to_add and not to_remove:
            return None
        return f"{member.mention}: {names_of(to_remove)} → {names_of(to_add)}"

    async def rank_name(self, rank_id: int | None) -> str | None:
        """Full FSA rank name (getPilotData only gives the short form, e.g. "CEO"). None if unknown."""
        try:
            return next((r.name for r in await self.bot.fsa.ranks() if r.id == rank_id), None)
        except ServiceError:
            return None

    async def notify_board_rank(
        self, member: discord.Member, rank_id: int, removed: str | None, profile_url: str | None
    ) -> None:
        """Tell staff (once per member and rank) that a pilot is on a Board of Directors rank."""
        rank_name = await self.rank_name(rank_id) or f"rank {rank_id}"
        removed_text = f" Removed pilot rank role(s): {removed}." if removed else ""
        await self.report_once(
            f"board:{member.id}:{rank_id}",
            f"🎖️ {member.mention} is now **{rank_name}** in FSAirlines (Board of Directors).{removed_text} "
            f"Assign their Board of Directors role manually. · {_profile_link(profile_url)}",
        )

    async def clear_board_notices(self, discord_id: int) -> None:
        """Forget earlier Board notices so a later promotion is announced again."""
        for key in await self.bot.storage.kv_keys(REPORTED_KEY.format(f"board:{discord_id}:")):
            await self.bot.storage.kv_delete(key)

    async def sync_all(self) -> tuple[list[str], int]:
        """Sync every linked member. Returns (changes, unchanged count)."""
        if not self.rank_roles_enabled:
            return [], 0
        guild = self.bot.primary_guild()
        ranks_by_pilot = {p.id: p.rank_id for p in await self.bot.fsa.pilot_list()}
        changes, unchanged = [], 0
        for link in await self.bot.storage.all_links(lk.SERVICE_FSA):
            # A member who left the Discord server still gets their website groups synced
            member = guild.get_member(link.discord_id) if guild else None
            if link.external_id not in ranks_by_pilot:
                await self.report_once(
                    f"pilot-missing:{link.external_id}",
                    f"⚠️ <@{link.discord_id}> is linked to FSAirlines pilot `{link.external_id}`, "
                    "which no longer exists. "
                    f"· {_profile_link(link.ips_profile_url)}",
                )
                continue
            await self.resolved(f"pilot-missing:{link.external_id}")
            rank_id = ranks_by_pilot[link.external_id]
            change = await self.sync_ranks(member, link.ips_member_id, rank_id, profile_url=link.ips_profile_url)
            if change:
                changes.append(change)
            else:
                unchanged += 1
        return changes, unchanged

    @tasks.loop(minutes=60)
    async def rank_sync(self) -> None:
        try:
            changes, _ = await self.sync_all()
            if changes:
                log.info("Rank sync: %s", "; ".join(changes))
        except ServiceError as exc:
            log.warning("Rank sync failed, retrying next cycle: %s", exc)
        except Exception:
            log.exception("Unexpected error in rank sync")

    @rank_sync.before_loop
    async def before_rank_sync(self) -> None:
        await self.bot.wait_until_ready()

    # --- commands -----------------------------------------------------------------------------------------------------

    @app_commands.command(name="sync_rank", description="Sync FSAirlines rank roles now (staff)")
    @app_commands.describe(member="Only sync this member (default: everyone linked)")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.guild_only()
    async def sync_rank(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not self.rank_roles_enabled:
            await interaction.followup.send("Rank roles are switched off (FSA_RANK_ROLES is empty).", ephemeral=True)
            return
        try:
            if member is None:
                changes, unchanged = await self.sync_all()
                summary = "\n".join(changes) if changes else "No changes."
                await interaction.followup.send(f"{summary}\n{unchanged} member(s) already up to date.", ephemeral=True)
                return
            link, note = await self.ensure_link(member)
            if link is None:
                msg = f"{member.mention} isn't linked to an FSAirlines pilot."
                if note:
                    msg += " Their website profile was found, but it's waiting for staff review in #system-messages."
                await interaction.followup.send(msg, ephemeral=True)
                return
            pilot = await self.bot.fsa.pilot(link.external_id)
            change = await self.sync_ranks(
                member, link.ips_member_id, pilot.rank_id, profile_url=link.ips_profile_url
            )
            rank_name = await self.rank_name(pilot.rank_id) or pilot.rank or "unknown rank"
            await interaction.followup.send(
                change or f"{member.mention} is already up to date ({rank_name}).", ephemeral=True
            )
        except ServiceError as exc:
            log.error("sync_rank failed: %s", exc)
            await interaction.followup.send(":x: ERROR: Could not reach FSAirlines.", ephemeral=True)

    @app_commands.command(name="pilot", description="Show FSAirlines pilot statistics")
    @app_commands.describe(
        member="A linked server member (default: yourself)",
        pilot="Any FSAirlines pilot: callsign (e.g. FVK003) or pilot ID",
    )
    async def pilot(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        pilot: app_commands.Range[str, 1, 40] | None = None,
    ) -> None:
        if member and pilot:
            await interaction.response.send_message("Use either `member` or `pilot`, not both.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        mention, fsa_id = None, None
        if pilot is None:
            target = member or interaction.user
            link, note = await self.ensure_link(target)
            if link is None:
                if member:
                    msg = f"{member.mention} hasn't linked an FSAirlines pilot."
                else:
                    msg = (
                        "You haven't linked your FSAirlines pilot yet. On your "
                        f"[flyviking.net profile](<{self.settings.website_root}>), fill in your "
                        "**FSAirlines Pilot ID** (or callsign) and **Discord username**; the bot links you "
                        "automatically."
                    )
                if note and not member:  # review details are only shown to the member themselves
                    msg = note
                await interaction.delete_original_response()
                await interaction.followup.send(msg, ephemeral=True)
                return
            fsa_id, mention = link.external_id, f"<@{link.discord_id}>"

        try:
            if fsa_id is None:
                fsa_id = await self.bot.fsa.resolve_pilot_id(pilot)
                if fsa_id is None:
                    raise FsaNotFound(pilot)
                if link := await self.bot.storage.link_by_external(lk.SERVICE_FSA, fsa_id):
                    mention = f"<@{link.discord_id}>"
            data = await self.bot.fsa.pilot(fsa_id)
        except FsaNotFound:
            await interaction.delete_original_response()
            await interaction.followup.send(f":x: FSAirlines pilot `{pilot or fsa_id}` not found.", ephemeral=True)
            return
        except ServiceError as exc:
            log.error("Could not retrieve pilot %s: %s", pilot or fsa_id, exc)
            await interaction.delete_original_response()
            await interaction.followup.send(":x: ERROR: Could not retrieve data from FSAirlines", ephemeral=True)
            return
        try:
            flights = await self.bot.fsa.flight_logs(count=1, pilot_id=fsa_id)
        except ServiceError:
            flights = []
        rank_name = await self.rank_name(data.rank_id)
        await interaction.followup.send(embed=pilot_embed(data, mention, flights[-1] if flights else None, rank_name))


async def setup(bot: FlyVikingBot) -> None:
    await bot.add_cog(Pilots(bot))
    if not (bot.settings.ips_field_fsa_pilot_id and bot.settings.ips_field_discord_name):
        log.info("IPS_FIELD_FSA_PILOT_ID / IPS_FIELD_DISCORD_NAME not set; automatic linking disabled")
