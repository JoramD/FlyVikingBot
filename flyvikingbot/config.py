"""Settings loaded from environment variables (and an optional .env file).

For backwards compatibility with existing deployments, secrets that are not set
in the environment fall back to the legacy ``credentials.json`` file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# env var -> key in legacy credentials.json
_LEGACY_KEYS = {
    "DISCORD_TOKEN": "discordBotToken",
    "FSA_API_KEY": "fsaKey",
    "WEBSITE_API_KEY": "websiteKey",
    "UPTIME_KUMA_URL": "uptimeKumaURL",
}


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    discord_token: str
    fsa_api_key: str
    fsa_va_id: int
    website_api_key: str
    website_base_url: str
    uptime_kuma_url: str | None

    guild_id: int | None
    channel_announcements: int
    channel_screenshots: int

    webhook_secret: str
    webhook_host: str
    webhook_port: int
    webhook_capture: bool
    webhook_capture_keep: int
    announcement_blog_id: int | None

    # FSAirlines integration (each feature is disabled while its setting is unset)
    channel_flight_logs: int | None
    channel_system_messages: int | None
    ips_field_fsa_pilot_id: int | None
    ips_field_discord_name: int | None
    fsa_poll_seconds: int
    fsa_rank_sync_minutes: int

    data_dir: Path

    @property
    def website_root(self) -> str:
        """Public site root, derived from the API URL (https://flyviking.net/api/index.php -> https://flyviking.net/)."""
        return self.website_base_url.split("/api/", 1)[0].rstrip("/") + "/"


def _load_legacy(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(
    env: dict[str, str] | None = None,
    credentials_path: Path | str = "credentials.json",
    load_env_file: bool = True,
) -> Settings:
    if env is None:
        if load_env_file:
            load_dotenv()
        env = dict(os.environ)

    legacy = _load_legacy(Path(credentials_path))

    def get(name: str, default: str | None = None) -> str | None:
        value = env.get(name)
        if value not in (None, ""):
            return value
        if name in _LEGACY_KEYS and legacy.get(_LEGACY_KEYS[name]):
            return legacy[_LEGACY_KEYS[name]]
        return default

    def require(name: str) -> str:
        value = get(name)
        if not value:
            raise ConfigError(f"Missing required setting: {name}")
        return value

    def optional_int(name: str) -> int | None:
        value = get(name)
        return int(value) if value else None

    return Settings(
        discord_token=require("DISCORD_TOKEN"),
        fsa_api_key=require("FSA_API_KEY"),
        fsa_va_id=int(get("FSA_VA_ID", "42982")),
        website_api_key=require("WEBSITE_API_KEY"),
        website_base_url=get("WEBSITE_BASE_URL", "https://flyviking.net/api/index.php"),
        uptime_kuma_url=get("UPTIME_KUMA_URL"),
        guild_id=optional_int("GUILD_ID"),
        channel_announcements=int(get("CHANNEL_ANNOUNCEMENTS", "631949277559783457")),
        channel_screenshots=int(get("CHANNEL_SCREENSHOTS", "631950027476172810")),
        webhook_secret=require("WEBHOOK_SECRET"),
        webhook_host=get("WEBHOOK_BIND", "0.0.0.0"),
        webhook_port=int(get("WEBHOOK_PORT", "8080")),
        webhook_capture=_bool(get("WEBHOOK_CAPTURE", "true")),
        webhook_capture_keep=int(get("WEBHOOK_CAPTURE_KEEP", "50")),
        announcement_blog_id=optional_int("ANNOUNCEMENT_BLOG_ID"),
        channel_flight_logs=optional_int("CHANNEL_FLIGHT_LOGS"),
        channel_system_messages=optional_int("CHANNEL_SYSTEM_MESSAGES"),
        ips_field_fsa_pilot_id=optional_int("IPS_FIELD_FSA_PILOT_ID"),
        ips_field_discord_name=optional_int("IPS_FIELD_DISCORD_NAME"),
        fsa_poll_seconds=max(60, int(get("FSA_POLL_SECONDS", "120"))),
        fsa_rank_sync_minutes=max(5, int(get("FSA_RANK_SYNC_MINUTES", "60"))),
        data_dir=Path(get("DATA_DIR", "data")),
    )
