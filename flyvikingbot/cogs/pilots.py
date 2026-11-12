"""FSAirlines pilots: /pilot, automatic account linking from website profiles, and rank roles."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import linking as lk
from ..constants import BRAND_COLOUR
from ..services.fsairlines import FsaNotFound, Pilot
from ..services.http import ServiceError
from ..storage import Link
from ..webhooks import ips_events as ev
from .flight_logs import format_hours

if TYPE_CHECKING:
    from ..bot import FlyVikingBot

log = logging.getLogger(__name__)

REVIEW_KEY = "review:{}"  # pending staff review, keyed by website member ID
REPORTED_KEY = "reported:{}"  # problems already reported to staff (cleared once resolved)


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


def pilot_embed(pilot: Pilot, mention: str | None, last_flight: Any | None) -> discord.Embed:
    embed = discord.Embed(title=pilot.full_name, colour=BRAND_COLOUR)
    if mention:
        embed.description = mention
    for name, value in (
        ("Rank", pilot.rank),
        ("Hours", format_hours(pilot.hours)),
        ("Flights", f"{pilot.flights:,}" if pilot.flights is not None else None),
        ("Avg. rating", f"{pilot.rating:g}" if pilot.rating is not None else None),
        ("Distance", f"{pilot.distance:,.0f} nm" if pilot.distance is not None else None),
        ("Location", pilot.location),
        ("Last active", discord.utils.format_dt(pilot.last_active, "R") if pilot.last_active else None),
    ):
        if value:
            embed.add_field(name=name, value=value, inline=True)
    if last_flight:
        parts = [f"{last_flight.dep or '????'} → {last_flight.arr or '????'}"]
        if last_flight.aircraft:
            parts.append(last_flight.aircraft)
        if last_flight.completed_at:
            parts.append(discord.utils.format_dt(last_flight.completed_at, "d"))
        if last_flight.landing_rate is not None:
            parts.append(f"{last_flight.landing_rate:+d} fpm")
        embed.add_field(name="Last flight", value=" · ".join(parts), inline=False)
    embed.set_footer(text=f"FSAirlines pilot {pilot.id}")
    return embed


# --- cog -------------------------------------------------------------------------------------------


class Pilots(commands.Cog):
    def __init__(self, bot: FlyVikingBot) -> None:
        self.bot = bot
        self.settings = bot.settings

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(ReviewButton)
        self.rank_sync.change_interval(minutes=self.settings.fsa_rank_sync_minutes)
        self.rank_sync.start()

    async def cog_unload(self) -> None:
        self.rank_sync.cancel()
        self.bot.remove_dynamic_items(ReviewButton)

    @property
    def linking_enabled(self) -> bool:
        return bool(self.settings.ips_field_fsa_pilot_id and self.settings.ips_field_discord_name)

    # --- staff notifications ----------------------------------------------------------------------------------------

    async def report_once(self, key: str, message: str) -> None:
        storage_key = REPORTED_KEY.format(key)
        if await self.bot.storage.kv_get(storage_key) is None:
            await self.bot.post_system_message(message)
            await self.bot.storage.kv_set(storage_key, "1")

    async def resolved(self, key: str) -> None:
        await self.bot.storage.kv_delete(REPORTED_KEY.format(key))

    # --- linking ------------------------------------------------------------------------------------------------------

    def find_discord_members(self, guild: discord.Guild, entered: str) -> list[int]:
        username = lk.clean_discord_name(entered)
        if not username:
            return []
        exact = [m.id for m in guild.members if m.name.casefold() == username]
        if exact:
            return exact
        wanted = lk.normalize(entered.strip().lstrip("@"))
        return [
            m.id
            for m in guild.members
            if wanted in {lk.normalize(m.global_name), lk.normalize(m.nick), lk.normalize(m.display_name)}
        ]

    @commands.Cog.listener()
    async def on_ips_webhook(self, event: str, payload: Any) -> None:
        if event not in ev.MEMBER_EVENTS or not self.linking_enabled:
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

    async def process_profile(self, profile: ev.MemberProfile) -> lk.Decision:
        storage, guild = self.bot.storage, self.bot.primary_guild()
        current = await storage.link_by_ips_member(profile.member_id, lk.SERVICE_FSA)
        inp = lk.LinkInput(profile=profile, current=current, pilot=None)

        if lk.needs_lookup(profile, current):
            if guild is None:
                log.warning("Can't link: bot isn't in exactly one guild and GUILD_ID isn't set")
                return lk.Decision(lk.Action.IGNORE)
            pilot_id = profile.fsa_pilot_id.strip()
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

        if decision.action is lk.Action.LINK:
            await self.apply_link(profile, decision.discord_id, source="auto", pilot=inp.pilot)
            await self.bot.post_system_message(
                f"✅ Linked <@{decision.discord_id}> to FSAirlines pilot `{inp.pilot.id}` ({inp.pilot.full_name}) "
                f"· {_profile_link(profile.profile_url)}"
            )
        elif decision.action is lk.Action.UNLINK:
            await self.apply_unlink(current)
            await self.bot.post_system_message(
                f"Unlinked <@{current.discord_id}> from FSAirlines (profile fields cleared) "
                f"· {_profile_link(profile.profile_url)}"
            )
        elif decision.action is lk.Action.REVIEW:
            await self.request_review(profile, decision)
        return decision

    async def request_review(self, profile: ev.MemberProfile, decision: lk.Decision) -> None:
        reasons = "\n".join(f"• {r}" for r in decision.reasons)
        header = (
            f"⚠️ **Couldn't link website member '{profile.name}' automatically** "
            f"· {_profile_link(profile.profile_url)}\n"
            f"Entered: FSAirlines pilot `{profile.fsa_pilot_id}`, Discord `{profile.discord_name}`\n{reasons}"
        )
        if not decision.approvable:
            await self.bot.post_system_message(header + "\nThe member needs to correct their profile fields.")
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
            header + f"\nApprove to link <@{decision.discord_id}> anyway.", view=review_view(profile.member_id)
        )

    async def handle_review(self, interaction: discord.Interaction, action: str, ips_member_id: int) -> None:
        perms = getattr(interaction.user, "guild_permissions", None)
        if not perms or not perms.manage_roles:
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
        displaced = await self.bot.storage.save_link(
            discord_id=discord_id,
            ips_member_id=profile.member_id,
            ips_profile_url=profile.profile_url,
            discord_name=profile.discord_name,
            service=lk.SERVICE_FSA,
            external_id=profile.fsa_pilot_id.strip(),
            source=source,
        )
        guild = self.bot.primary_guild()
        if guild:
            for other in displaced:
                if member := guild.get_member(other):
                    await self.set_rank_role(member, None, clear=True)
            if pilot and (member := guild.get_member(discord_id)):
                await self.set_rank_role(member, pilot.rank_id, profile_url=profile.profile_url)

    async def apply_unlink(self, link: Link) -> None:
        guild = self.bot.primary_guild()
        if guild and (member := guild.get_member(link.discord_id)):
            await self.set_rank_role(member, None, clear=True)
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

    async def notify_board_rank(
        self, member: discord.Member, rank_id: int, removed: str | None, profile_url: str | None
    ) -> None:
        """Tell staff (once per member and rank) that a pilot is on a Board of Directors rank."""
        try:
            rank_name = next((r.name for r in await self.bot.fsa.ranks() if r.id == rank_id), f"rank {rank_id}")
        except ServiceError:
            rank_name = f"rank {rank_id}"
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
        guild = self.bot.primary_guild()
        if guild is None:
            return [], 0
        ranks_by_pilot = {p.id: p.rank_id for p in await self.bot.fsa.pilot_list()}
        changes, unchanged = [], 0
        for link in await self.bot.storage.all_links(lk.SERVICE_FSA):
            member = guild.get_member(link.discord_id)
            if member is None:
                continue  # left the server
            if link.external_id not in ranks_by_pilot:
                await self.report_once(
                    f"pilot-missing:{link.external_id}",
                    f"⚠️ {member.mention} is linked to FSAirlines pilot `{link.external_id}`, which no longer exists. "
                    f"· {_profile_link(link.ips_profile_url)}",
                )
                continue
            await self.resolved(f"pilot-missing:{link.external_id}")
            rank_id = ranks_by_pilot[link.external_id]
            change = await self.set_rank_role(member, rank_id, profile_url=link.ips_profile_url)
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
        try:
            if member is None:
                changes, unchanged = await self.sync_all()
                summary = "\n".join(changes) if changes else "No changes."
                await interaction.followup.send(f"{summary}\n{unchanged} member(s) already up to date.", ephemeral=True)
                return
            link = await self.bot.storage.link_by_discord(member.id, lk.SERVICE_FSA)
            if link is None:
                msg = f"{member.mention} isn't linked to an FSAirlines pilot."
                await interaction.followup.send(msg, ephemeral=True)
                return
            pilot = await self.bot.fsa.pilot(link.external_id)
            change = await self.set_rank_role(member, pilot.rank_id, profile_url=link.ips_profile_url)
            await interaction.followup.send(
                change or f"{member.mention} is already up to date ({pilot.rank or 'unknown rank'}).", ephemeral=True
            )
        except ServiceError as exc:
            log.error("sync_rank failed: %s", exc)
            await interaction.followup.send(":x: ERROR: Could not reach FSAirlines.", ephemeral=True)

    @app_commands.command(name="pilot", description="Show FSAirlines pilot statistics")
    @app_commands.describe(member="A linked server member (default: yourself)", pilot_id="Any FSAirlines pilot ID")
    async def pilot(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        pilot_id: app_commands.Range[int, 1] | None = None,
    ) -> None:
        if member and pilot_id:
            await interaction.response.send_message("Use either `member` or `pilot_id`, not both.", ephemeral=True)
            return

        mention = None
        if pilot_id is None:
            target = member or interaction.user
            link = await self.bot.storage.link_by_discord(target.id, lk.SERVICE_FSA)
            if link is None:
                if member:
                    msg = f"{member.mention} hasn't linked an FSAirlines pilot."
                else:
                    msg = (
                        "You haven't linked your FSAirlines pilot yet. On your "
                        f"[flyviking.net profile](<{self.settings.website_root}>), fill in your "
                        "**FSAirlines Pilot ID** and **Discord username**; the bot links you automatically."
                    )
                await interaction.response.send_message(msg, ephemeral=True)
                return
            fsa_id, mention = link.external_id, f"<@{link.discord_id}>"
        else:
            fsa_id = str(pilot_id)
            if link := await self.bot.storage.link_by_external(lk.SERVICE_FSA, fsa_id):
                mention = f"<@{link.discord_id}>"

        await interaction.response.defer(thinking=True)
        try:
            pilot = await self.bot.fsa.pilot(fsa_id)
        except FsaNotFound:
            await interaction.delete_original_response()
            await interaction.followup.send(f":x: FSAirlines pilot `{fsa_id}` not found.", ephemeral=True)
            return
        except ServiceError as exc:
            log.error("Could not retrieve pilot %s: %s", fsa_id, exc)
            await interaction.delete_original_response()
            await interaction.followup.send(":x: ERROR: Could not retrieve data from FSAirlines", ephemeral=True)
            return
        try:
            flights = await self.bot.fsa.flight_logs(count=1, pilot_id=fsa_id)
        except ServiceError:
            flights = []
        await interaction.followup.send(embed=pilot_embed(pilot, mention, flights[-1] if flights else None))


async def setup(bot: FlyVikingBot) -> None:
    await bot.add_cog(Pilots(bot))
    if not (bot.settings.ips_field_fsa_pilot_id and bot.settings.ips_field_discord_name):
        log.info("IPS_FIELD_FSA_PILOT_ID / IPS_FIELD_DISCORD_NAME not set; automatic linking disabled")
