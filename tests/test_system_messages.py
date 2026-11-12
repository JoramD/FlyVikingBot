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
