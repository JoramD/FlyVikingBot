import json

import pytest

from flyvikingbot.config import ConfigError, load_settings

from .conftest import BASE_ENV


def test_env_values(settings):
    assert settings.discord_token == "token"
    assert settings.channel_announcements == 100
    assert settings.fsa_va_id == 42982
    assert settings.guild_id is None
    assert settings.webhook_capture is True


def test_legacy_credentials_fallback(tmp_path):
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"discordBotToken": "legacy-token", "fsaKey": "legacy-fsa", "websiteKey": "legacy-web",
                                 "uptimeKumaURL": "https://kuma/push"}))
    env = {"WEBHOOK_SECRET": "s" * 32, "FSA_API_KEY": "env-wins"}
    s = load_settings(env=env, credentials_path=creds)
    assert s.discord_token == "legacy-token"
    assert s.fsa_api_key == "env-wins"
    assert s.uptime_kuma_url == "https://kuma/push"


def test_missing_required(tmp_path):
    env = {k: v for k, v in BASE_ENV.items() if k != "WEBHOOK_SECRET"}
    with pytest.raises(ConfigError, match="WEBHOOK_SECRET"):
        load_settings(env=env, credentials_path=tmp_path / "none.json")
