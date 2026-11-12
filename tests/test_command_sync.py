"""With GUILD_ID set, commands are registered for the guild only and stale global ones are removed."""

from unittest.mock import AsyncMock

from flyvikingbot.bot import FlyVikingBot
from flyvikingbot.config import load_settings

from .conftest import BASE_ENV


async def make_bot(tmp_path, **env):
    settings = load_settings(env={**BASE_ENV, "DATA_DIR": str(tmp_path), **env}, credentials_path=tmp_path / "x")
    bot = FlyVikingBot(settings)
    await bot._async_setup_hook()  # what login() does
    bot.load_extension = AsyncMock()
    bot.webhook_server.start = AsyncMock()

    async def clear(interaction):  # a global command, like the cogs add
        pass

    bot.tree.command(name="clear", description="x")(clear)
    calls = []

    async def fake_sync(*, guild=None):
        calls.append((guild.id if guild else None, sorted(c.name for c in bot.tree.get_commands(guild=guild))))
        return []

    bot.tree.sync = fake_sync
    return bot, calls


async def test_guild_sync_clears_global_commands(tmp_path):
    bot, calls = await make_bot(tmp_path, GUILD_ID="42")
    await bot.setup_hook()
    assert calls == [(42, ["clear"]), (None, [])]  # guild gets the commands, global is emptied
    await bot.close()


async def test_without_guild_id_commands_stay_global(tmp_path):
    bot, calls = await make_bot(tmp_path)
    await bot.setup_hook()
    assert calls == [(None, ["clear"])]
    await bot.close()
