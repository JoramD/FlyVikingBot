"""Staff access (one definition for commands and buttons) and the persistent "Completed" button
on #system-messages posts."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, TypeVar

import discord
from discord import app_commands

COMPLETED_ID = "fvdone"
STAFF_ONLY_MESSAGE = ":x: ERROR: Only staff can do this."

T = TypeVar("T")


def is_staff(user: Any, staff_role_id: int | None) -> bool:
    """Staff = Manage Roles, Administrator, or the system-messages role (@Board of Directors)."""
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and (perms.manage_roles or perms.administrator):
        return True
    return staff_role_id is not None and any(r.id == staff_role_id for r in getattr(user, "roles", []))


def staff_only() -> Callable[[T], T]:
    """Decorator for staff-only slash commands.

    Discord hides the command from members without Manage Roles (administrators have it implicitly),
    and the bot checks again with is_staff, so loosened overrides in Server Settings → Integrations
    can't open it up. A refused user gets STAFF_ONLY_MESSAGE (see FlyVikingBot.on_app_command_error).
    """

    def predicate(interaction: discord.Interaction) -> bool:
        settings = getattr(interaction.client, "settings", None)
        return is_staff(interaction.user, getattr(settings, "system_messages_role_id", None))

    def decorator(func: T) -> T:
        func = app_commands.default_permissions(manage_roles=True)(func)
        func = app_commands.guild_only()(func)
        return app_commands.check(predicate)(func)

    return decorator


class CompletedButton(discord.ui.DynamicItem[discord.ui.Button], template=COMPLETED_ID):
    """Marks an action-required post as done: records who did it and removes the button.

    The custom ID carries no state, so it keeps working after restarts.
    """

    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(label="Completed", style=discord.ButtonStyle.success, emoji="✅", custom_id=COMPLETED_ID)
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        staff_role_id = getattr(interaction.client.settings, "system_messages_role_id", None)
        if not is_staff(interaction.user, staff_role_id):
            await interaction.response.send_message(STAFF_ONLY_MESSAGE, ephemeral=True)
            return
        original = interaction.message.content if interaction.message else ""
        when = discord.utils.format_dt(discord.utils.utcnow(), "f")
        await interaction.response.edit_message(
            content=f"{original}\n✅ Completed by {interaction.user.mention} · {when}",
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def completed_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(CompletedButton())
    return view
