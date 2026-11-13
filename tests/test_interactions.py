from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from flyvikingbot.interactions import EPHEMERAL_SECONDS, reply_ephemeral


def make_interaction(done: bool):
    message = SimpleNamespace(delete=AsyncMock())
    response = SimpleNamespace(is_done=MagicMock(return_value=done), send_message=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock(return_value=message))
    return SimpleNamespace(response=response, followup=followup), message


async def test_first_reply_deletes_itself():
    interaction, _ = make_interaction(done=False)
    await reply_ephemeral(interaction, "hi")
    interaction.response.send_message.assert_awaited_once_with("hi", ephemeral=True, delete_after=EPHEMERAL_SECONDS)


async def test_reply_after_defer_deletes_itself():
    interaction, message = make_interaction(done=True)
    await reply_ephemeral(interaction, "done")
    interaction.followup.send.assert_awaited_once_with("done", ephemeral=True, wait=True)
    message.delete.assert_awaited_once_with(delay=EPHEMERAL_SECONDS)


async def test_reply_can_be_kept():
    interaction, message = make_interaction(done=True)
    await reply_ephemeral(interaction, "keep", delete_after=None)
    message.delete.assert_not_awaited()
