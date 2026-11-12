import pytest

from flyvikingbot.config import ConfigError, load_settings

from .conftest import BASE_ENV


def test_env_values(settings):
    assert settings.discord_token == "token"
    assert settings.channel_announcements == 100
    assert settings.fsa_va_id == 42982
    assert settings.guild_id is None
    assert settings.webhook_capture is False  # off by default


def test_missing_required(tmp_path):
    env = {k: v for k, v in BASE_ENV.items() if k != "WEBHOOK_SECRET"}
    with pytest.raises(ConfigError, match="WEBHOOK_SECRET"):
        load_settings(env=env)
