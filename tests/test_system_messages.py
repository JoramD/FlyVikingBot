"""#system-messages: posts that need staff action ping @Board of Directors; informational posts don't."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from flyvikingbot.bot import FlyVikingBot
from flyvikingbot.config import load_settings

from .conftest import BASE_ENV

BOARD = 631901856997834778


def fake_bot(tmp_path, **env):
    settings = load_settings(env={**BASE_ENV, "DATA_DIR": str(tmp_path), "CHANNEL_SYSTEM_MESSAGES": "900", **env},
                             credentials_path=tmp_path / "none")
    channel = SimpleNamespace(send=AsyncMock())
    return SimpleNamespace(settings=settings, get_channel=lambda cid: channel, fetch_channel=AsyncMock()), channel


async def test_action_required_pings_board_only(tmp_path):
    bot, channel = fake_bot(tmp_path)
    await FlyVikingBot.post_system_message(bot, "Fix <@1>'s link", action_required=True)
    content, mentions = channel.send.await_args.args[0], channel.send.await_args.kwargs["allowed_mentions"]
    assert content == f"<@&{BOARD}> Fix <@1>'s link"
    assert [r.id for r in mentions.roles] == [BOARD] and mentions.users is False and mentions.everyone is False


async def test_informational_posts_ping_nobody(tmp_path):
    bot, channel = fake_bot(tmp_path)
    await FlyVikingBot.post_system_message(bot, "✅ Linked <@1>", action_required=False)
    content, mentions = channel.send.await_args.args[0], channel.send.await_args.kwargs["allowed_mentions"]
    assert content == "✅ Linked <@1>"
    assert mentions.roles is False and mentions.users is False


async def test_ping_role_configurable_and_can_be_disabled(tmp_path):
    bot, channel = fake_bot(tmp_path, SYSTEM_MESSAGES_ROLE_ID="123")
    await FlyVikingBot.post_system_message(bot, "x", action_required=True)
    assert channel.send.await_args.args[0] == "<@&123> x"
    bot, channel = fake_bot(tmp_path, SYSTEM_MESSAGES_ROLE_ID="")
    await FlyVikingBot.post_system_message(bot, "x", action_required=True)
    assert channel.send.await_args.args[0] == "x"


from unittest.mock import MagicMock  # noqa: E402

from flyvikingbot.system_messages import COMPLETED_ID, CompletedButton, is_staff  # noqa: E402


async def test_action_posts_get_completed_button(tmp_path):
    bot, channel = fake_bot(tmp_path)
    await FlyVikingBot.post_system_message(bot, "Create a role", action_required=True)
    view = channel.send.await_args.kwargs["view"]
    assert [b.custom_id for b in view.children] == [COMPLETED_ID]


async def test_informational_and_review_posts_keep_their_own_buttons(tmp_path):
    bot, channel = fake_bot(tmp_path)
    await FlyVikingBot.post_system_message(bot, "✅ Linked", action_required=False)
    assert "view" not in channel.send.await_args.kwargs
    own = object()
    await FlyVikingBot.post_system_message(bot, "Review", action_required=True, view=own)
    assert channel.send.await_args.kwargs["view"] is own


def staff_member(role_ids=(), manage_roles=False):
    return SimpleNamespace(
        guild_permissions=SimpleNamespace(manage_roles=manage_roles, administrator=False),
        roles=[SimpleNamespace(id=r) for r in role_ids],
        mention="<@77>",
    )


def test_is_staff():
    assert is_staff(staff_member(role_ids=[BOARD]), BOARD)
    assert is_staff(staff_member(manage_roles=True), BOARD)
    assert not is_staff(staff_member(role_ids=[1]), BOARD)


async def test_completed_button_records_who_and_removes_button(tmp_path):
    bot, _ = fake_bot(tmp_path)
    interaction = MagicMock(client=bot, user=staff_member(role_ids=[BOARD]),
                            message=SimpleNamespace(content=f"<@&{BOARD}> Create a role"))
    interaction.response.edit_message = AsyncMock()
    await CompletedButton().callback(interaction)
    kwargs = interaction.response.edit_message.await_args.kwargs
    assert kwargs["content"].startswith(f"<@&{BOARD}> Create a role\n✅ Completed by <@77> · ")
    assert kwargs["view"] is None and kwargs["allowed_mentions"].roles is False


async def test_completed_button_staff_only(tmp_path):
    bot, _ = fake_bot(tmp_path)
    interaction = MagicMock(client=bot, user=staff_member(role_ids=[1]))
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    await CompletedButton().callback(interaction)
    assert "Only staff" in interaction.response.send_message.await_args.args[0]
    interaction.response.edit_message.assert_not_awaited()
