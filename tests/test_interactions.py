from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from flyvikingbot.interactions import EPHEMERAL_SECONDS, fit_lines, reply_ephemeral


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


def test_fit_lines_short_list_is_unchanged():
    assert fit_lines(["a", "b"], "2 up to date.") == "a\nb\n2 up to date."
    assert fit_lines([]) == ""


def test_fit_lines_stays_under_the_limit():
    lines = [f"<@{i}>: First Officer → Captain" for i in range(200)]
    text = fit_lines(lines, "5 member(s) already up to date.")
    assert len(text) <= 2000
    assert text.endswith("more (see the bot log)\n5 member(s) already up to date.")
    shown = text.count("→")
    assert f"...and {200 - shown} more" in text
