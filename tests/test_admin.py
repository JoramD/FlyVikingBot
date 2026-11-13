from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from flyvikingbot.cogs.admin import TIME_ONLY_LIMIT, Admin, clear_summary


def make(deleted: int = 3):
    channel = MagicMock(spec=discord.TextChannel)
    channel.purge = AsyncMock(return_value=[object()] * deleted)
    message = SimpleNamespace(delete=AsyncMock())
    interaction = MagicMock()
    interaction.channel = channel
    interaction.response.is_done = MagicMock(return_value=True)
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock(return_value=message)
    return Admin(SimpleNamespace()), interaction, channel


@pytest.mark.parametrize(
    ("amount", "minutes", "limit"),
    [(20, None, 20), (None, 30, TIME_ONLY_LIMIT), (20, 30, 20)],
)
async def test_clear_limits(amount, minutes, limit):
    cog, interaction, channel = make()
    before = discord.utils.utcnow()
    await cog.clear.callback(cog, interaction, amount=amount, minutes=minutes)
    kwargs = channel.purge.await_args.kwargs
    assert kwargs["limit"] == limit
    if minutes is None:
        assert kwargs["after"] is None
    else:
        expected = before - timedelta(minutes=minutes)
        assert abs((kwargs["after"] - expected).total_seconds()) < 5


async def test_clear_needs_amount_or_minutes():
    cog, interaction, channel = make()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.send_message = AsyncMock()
    await cog.clear.callback(cog, interaction, amount=None, minutes=None)
    channel.purge.assert_not_awaited()
    call = interaction.response.send_message.await_args
    assert "amount, minutes, or both" in call.args[0] and call.kwargs["delete_after"] is None


async def test_clear_reply_mentions_the_period():
    cog, interaction, _ = make(deleted=14)
    await cog.clear.callback(cog, interaction, amount=None, minutes=30)
    assert interaction.followup.send.await_args.args[0] == (
        ":white_check_mark: Deleted 14 messages from the last 30 minutes."
    )


@pytest.mark.parametrize(
    ("count", "minutes", "text"),
    [
        (20, None, "Deleted 20 messages."),
        (1, None, "Deleted 1 message."),
        (0, None, "No messages to delete."),
        (0, 5, "No messages to delete from the last 5 minutes."),
        (3, 1, "Deleted 3 messages from the last 1 minute."),
        (7, 60, "Deleted 7 messages from the last 1 h."),
        (7, 90, "Deleted 7 messages from the last 1 h 30 min."),
        (7, 1440, "Deleted 7 messages from the last 24 h."),
    ],
)
def test_clear_summary(count, minutes, text):
    assert clear_summary(count, minutes) == text
