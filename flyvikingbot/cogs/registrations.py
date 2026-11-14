"""Website registrations awaiting approval: posted to #system-messages with Approve / Reject.

Pending registrations are found two ways that guard each other: member webhooks (immediate) and a
poll of recent members (catches anything missed, e.g. while the bot was offline). Approve validates
the account through the API; Reject deletes it after a confirmation. If an admin handles the
registration in the ACP instead, the poll updates the Discord post to match.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands, tasks

from .. import linking as lk
from ..interactions import reply_ephemeral
from ..services.http import ServiceError
from ..services.website import WebsiteNotFound
from ..system_messages import STAFF_ONLY_MESSAGE, is_staff
from ..webhooks import ips_events as ev

if TYPE_CHECKING:
    from ..bot import FlyVikingBot

log = logging.getLogger(__name__)

POLL_MINUTES = 15
LOOKBACK_SECONDS = 14 * 24 * 3600  # registrations older than this are left to the ACP
PENDING_KEY = "registration:{}"  # member ID -> the Discord post for a pending registration (JSON)
CONFIRM_SECONDS = 60
NO_MENTIONS = discord.AllowedMentions.none()

REGISTRATION_TEMPLATE = r"fvreg:(?P<action>approve|reject):(?P<member>\d+)"


def _profile_link(url: str | None) -> str:
    return f"[website profile](<{url}>)" if url else "website profile unknown"


def acp_validating_url(website_root: str) -> str:
    """The ACP members list filtered to accounts awaiting validation."""
    return f"{website_root}admin/?app=core&module=members&controller=members&filter=members_filter_validating"


def registration_message(reg: ev.Registration, discord_member_id: int | None = None) -> dict[str, Any]:
    """The #system-messages post for a registration awaiting approval (channel.send() kwargs; the Board
    ping and the Approve/Reject buttons are added when it's posted). `discord_member_id` is set when the
    Discord username they typed matches exactly one server member."""
    lines = [
        f"🆕 **New website registration awaiting approval:** {lk.code(reg.name)} · {_profile_link(reg.profile_url)}"
    ]
    if reg.joined:
        lines.append(f"Registered {discord.utils.format_dt(reg.joined, 'R')}")
    # Optional at registration (members are reminded on first login), so only shown when filled in
    extras = []
    if reg.fsa_pilot_id:
        extras.append(f"FSAirlines Pilot ID {lk.code(reg.fsa_pilot_id)}")
    if reg.discord_name:
        if discord_member_id:
            extras.append(f"Discord <@{discord_member_id}>")
        else:
            extras.append(f"Discord {lk.code(reg.discord_name)} (not found in the server)")
    if extras:
        lines.append(" · ".join(extras))
    return {"content": "\n".join(lines), "allowed_mentions": NO_MENTIONS}


def _closed(original: str, outcome: str) -> str:
    """The post with its outcome appended (buttons are removed separately)."""
    return f"{original}\n{outcome}"


class RegistrationButton(discord.ui.DynamicItem[discord.ui.Button], template=REGISTRATION_TEMPLATE):
    """Approve / Reject on a registration post; the member ID is in the custom ID, so it survives restarts."""

    def __init__(self, action: str, member_id: int) -> None:
        approve = action == "approve"
        super().__init__(
            discord.ui.Button(
                label="Approve" if approve else "Reject",
                style=discord.ButtonStyle.success if approve else discord.ButtonStyle.danger,
                custom_id=f"fvreg:{action}:{member_id}",
            )
        )
        self.action = action
        self.member_id = member_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(match["action"], int(match["member"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Registrations")
        if cog is None:
            await interaction.response.send_message(
                ":x: ERROR: Registration approvals are switched off.", ephemeral=True
            )
            return
        if self.action == "approve":
            await cog.approve(interaction, self.member_id)
        else:
            await cog.ask_reject(interaction, self.member_id)


def registration_view(member_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(RegistrationButton("approve", member_id))
    view.add_item(RegistrationButton("reject", member_id))
    return view


class ConfirmDelete(discord.ui.View):
    """The private "really delete?" step behind Reject."""

    def __init__(self, cog: Registrations, member_id: int, post: discord.Message | None) -> None:
        super().__init__(timeout=CONFIRM_SECONDS)
        self.cog, self.member_id, self.post = cog, member_id, post

    @discord.ui.button(label="Delete account", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await self.cog.reject(interaction, self.member_id, self.post)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled; the registration is still pending.", view=None)


class Registrations(commands.Cog):
    def __init__(self, bot: FlyVikingBot) -> None:
        self.bot = bot
        self.settings = bot.settings
        self._locks: dict[int, asyncio.Lock] = {}  # member ID -> serialises webhook, poll and buttons

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(RegistrationButton)
        self.poll.start()

    async def cog_unload(self) -> None:
        self.poll.cancel()
        self.bot.remove_dynamic_items(RegistrationButton)

    def _lock(self, member_id: int) -> asyncio.Lock:
        return self._locks.setdefault(member_id, asyncio.Lock())

    def _parse(self, data: Any) -> ev.Registration | None:
        return ev.parse_registration(data, self.settings.ips_field_fsa_pilot_id, self.settings.ips_field_discord_name)

    # --- finding registrations ----------------------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_ips_webhook(self, event: str, payload: Any) -> None:
        if event not in ev.MEMBER_EVENTS:
            return
        await self.bot.wait_until_ready()
        reg = self._parse(payload)
        if reg is None:
            return
        try:
            if reg.awaiting_approval:
                await self.announce(reg)
            else:
                await self.sync(reg.member_id, reg)  # e.g. validated in the ACP: update our post right away
        except Exception:
            log.exception("Handling registration of website member %s failed", reg.member_id)

    @tasks.loop(minutes=POLL_MINUTES)
    async def poll(self) -> None:
        try:
            await self.check_registrations()
        except ServiceError as exc:
            log.warning("Registration poll failed, retrying next cycle: %s", exc)
        except Exception:
            log.exception("Unexpected error in registration poll")

    @poll.before_loop
    async def before_poll(self) -> None:
        await self.bot.wait_until_ready()

    async def check_registrations(self) -> None:
        """Post new pending registrations, and update posts whose registration was handled elsewhere."""
        members = await self.bot.website.recent_members(joined_after=int(time.time()) - LOOKBACK_SECONDS)
        recent = {r.member_id: r for m in members if (r := self._parse(m))}
        for reg in recent.values():
            if reg.awaiting_approval:
                await self.announce(reg)
        for key in await self.bot.storage.kv_keys(PENDING_KEY.format("")):
            member_id = int(key.rsplit(":", 1)[1])
            await self.sync(member_id, recent.get(member_id))

    async def announce(self, reg: ev.Registration) -> None:
        """Post a pending registration once."""
        async with self._lock(reg.member_id):
            key = PENDING_KEY.format(reg.member_id)
            if await self.bot.storage.kv_get(key) is not None:
                return
            message = registration_message(reg, self._discord_member(reg.discord_name))
            posted = await self.bot.post_system_message(
                message["content"], action_required=True, view=registration_view(reg.member_id)
            )
            record = {"name": reg.name}
            if posted is not None:
                record.update(channel_id=posted.channel.id, message_id=posted.id, content=posted.content)
            await self.bot.storage.kv_set(key, json.dumps(record))
            log.info("Registration of website member %s (%s) posted for approval", reg.member_id, reg.name)

    def _discord_member(self, typed: str | None) -> int | None:
        guild = self.bot.primary_guild()
        matches = lk.find_discord_members(guild.members, typed) if guild and typed else []
        return matches[0] if len(matches) == 1 else None

    async def sync(self, member_id: int, reg: ev.Registration | None) -> None:
        """A stored pending registration that's no longer pending (handled in the ACP) closes its post."""
        async with self._lock(member_id):
            raw = await self.bot.storage.kv_get(PENDING_KEY.format(member_id))
            if raw is None:
                return
            if reg is None:
                try:
                    reg = self._parse(await self.bot.website.get_member(member_id))
                except WebsiteNotFound:
                    await self._close(member_id, json.loads(raw), "🗑️ Removed on the website")
                    return
            if reg is not None and not reg.validating:
                await self._close(member_id, json.loads(raw), "✅ Approved on the website")

    async def _close(self, member_id: int, record: dict[str, Any], outcome: str) -> None:
        """Append the outcome to the stored post, remove its buttons and forget the registration."""
        await self.bot.storage.kv_delete(PENDING_KEY.format(member_id))
        if not record.get("message_id"):
            return
        try:
            channel = self.bot.get_channel(record["channel_id"]) or await self.bot.fetch_channel(record["channel_id"])
            await channel.get_partial_message(record["message_id"]).edit(
                content=_closed(record.get("content", ""), outcome), view=None, allowed_mentions=NO_MENTIONS
            )
        except discord.HTTPException:
            log.warning("Couldn't update the registration post for website member %s", member_id)

    # --- buttons --------------------------------------------------------------------------------------------------

    async def _staff_only(self, interaction: discord.Interaction) -> bool:
        if is_staff(interaction.user, self.settings.system_messages_role_id):
            return True
        await interaction.response.send_message(STAFF_ONLY_MESSAGE, ephemeral=True)
        return False

    def _acp_fallback(self, what: str) -> str:
        return (
            f":x: ERROR: The website didn't accept the {what}. Please handle it in the ACP: "
            f"<{acp_validating_url(self.settings.website_root)}>"
        )

    async def _pending(self, member_id: int) -> ev.Registration | None:
        """The registration if it's still pending; None if it was approved or deleted in the meantime."""
        try:
            reg = self._parse(await self.bot.website.get_member(member_id))
        except WebsiteNotFound:
            return None
        return reg if reg and reg.validating else None

    async def approve(self, interaction: discord.Interaction, member_id: int) -> None:
        if not await self._staff_only(interaction):
            return
        await interaction.response.defer()  # acknowledges the click; the post is edited below
        original = interaction.message.content if interaction.message else ""
        async with self._lock(member_id):
            try:
                if await self._pending(member_id) is None:
                    await self._finish(interaction, member_id, original, "ℹ️ Already handled on the website")
                    return
                await self.bot.website.validate_member(member_id)
                still_pending = await self._pending(member_id) is not None
            except ServiceError as exc:
                log.error("Approving website member %s failed: %s", member_id, exc)
                await reply_ephemeral(interaction, self._acp_fallback("approval"), delete_after=None)
                return
            if still_pending:  # the API answered but didn't validate the account
                log.error("Website member %s is still validating after the API approval", member_id)
                await reply_ephemeral(interaction, self._acp_fallback("approval"), delete_after=None)
                return
            when = discord.utils.format_dt(discord.utils.utcnow(), "f")
            await self._finish(interaction, member_id, original, f"✅ Approved by {interaction.user.mention} · {when}")
            log.info("Website member %s approved by %s", member_id, interaction.user)

    async def ask_reject(self, interaction: discord.Interaction, member_id: int) -> None:
        if not await self._staff_only(interaction):
            return
        record = json.loads(await self.bot.storage.kv_get(PENDING_KEY.format(member_id)) or "{}")
        name = lk.code(record.get("name") or f"member {member_id}")
        await interaction.response.send_message(
            f"Delete {name}'s website account? This can't be undone.",
            view=ConfirmDelete(self, member_id, interaction.message),
            ephemeral=True,
        )

    async def reject(self, interaction: discord.Interaction, member_id: int, post: discord.Message | None) -> None:
        if not await self._staff_only(interaction):
            return
        await interaction.response.edit_message(content="Deleting the account…", view=None)
        original = post.content if post else ""
        async with self._lock(member_id):
            try:
                if await self._pending(member_id) is None:
                    outcome = "ℹ️ Already handled on the website"
                else:
                    await self.bot.website.delete_member(member_id)
                    when = discord.utils.format_dt(discord.utils.utcnow(), "f")
                    outcome = f"❌ Rejected by {interaction.user.mention} · {when} (website account deleted)"
            except ServiceError as exc:
                log.error("Deleting website member %s failed: %s", member_id, exc)
                await interaction.edit_original_response(content=self._acp_fallback("deletion"))
                return
            await self.bot.storage.kv_delete(PENDING_KEY.format(member_id))
            if post is not None:
                try:
                    await post.edit(content=_closed(original, outcome), view=None, allowed_mentions=NO_MENTIONS)
                except discord.HTTPException:
                    log.warning("Couldn't update the registration post for website member %s", member_id)
            await interaction.edit_original_response(content=outcome)
            log.info("Website member %s: %s", member_id, outcome)

    async def _finish(self, interaction: discord.Interaction, member_id: int, original: str, outcome: str) -> None:
        await self.bot.storage.kv_delete(PENDING_KEY.format(member_id))
        await interaction.edit_original_response(
            content=_closed(original, outcome), view=None, allowed_mentions=NO_MENTIONS
        )


async def setup(bot: FlyVikingBot) -> None:
    if not bot.settings.registration_approvals:
        log.info("REGISTRATION_APPROVALS is off; registrations are not posted to Discord")
        return
    if not bot.settings.channel_system_messages:
        log.warning("REGISTRATION_APPROVALS needs CHANNEL_SYSTEM_MESSAGES; registration approvals are off")
        return
    await bot.add_cog(Registrations(bot))
