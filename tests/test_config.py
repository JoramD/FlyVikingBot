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


def test_bad_number_gives_config_error(tmp_path):
    with pytest.raises(ConfigError, match="WEBHOOK_PORT must be a whole number"):
        load_settings(env={**BASE_ENV, "DATA_DIR": str(tmp_path), "WEBHOOK_PORT": "80a"})
    with pytest.raises(ConfigError, match="GUILD_ID"):
        load_settings(env={**BASE_ENV, "DATA_DIR": str(tmp_path), "GUILD_ID": "abc"})


def test_fsa_va_id_required_and_channels_optional(tmp_path):
    env = {k: v for k, v in BASE_ENV.items() if k not in ("FSA_VA_ID", "CHANNEL_ANNOUNCEMENTS", "CHANNEL_SCREENSHOTS")}
    with pytest.raises(ConfigError, match="FSA_VA_ID"):
        load_settings(env={**env, "DATA_DIR": str(tmp_path)})
    s = load_settings(env={**env, "FSA_VA_ID": "42982", "DATA_DIR": str(tmp_path)})
    assert s.channel_announcements is None and s.channel_screenshots is None  # no built-in IDs


def test_sayintentions_keys_optional(settings, tmp_path):
    assert settings.sayintentions_app_key is None and settings.sayintentions_account_key is None
    assert settings.sayintentions_va_key is None
    s = load_settings(env={**BASE_ENV, "DATA_DIR": str(tmp_path), "SAYINTENTIONS_APP_KEY": "app",
                           "SAYINTENTIONS_ACCOUNT_KEY": "account", "SAYINTENTIONS_VA_KEY": "va"})
    assert (s.sayintentions_app_key, s.sayintentions_account_key, s.sayintentions_va_key) == ("app", "account", "va")
