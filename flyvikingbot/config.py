"""Settings loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_RANK_ROLES = (
    "Trainee Pilot,First Officer,Senior First Officer,Cargo First Officer,Captain,Cargo Captain"
)


class ConfigError(RuntimeError):
    """A setting is missing or invalid; the bot exits with a readable message."""


@dataclass(frozen=True)
class Settings:
    """All settings, read once at startup. Every setting is documented in .env.example."""

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
    webhook_bind: str
    webhook_port: int
    webhook_capture: bool
    webhook_capture_keep: int
    announcement_blog_id: int | None

    # FSAirlines integration (each feature is disabled while its setting is unset)
    channel_flight_logs: int | None
    channel_system_messages: int | None
    system_messages_role_id: int | None  # pinged on #system-messages posts that need staff action
    ips_field_fsa_pilot_id: int | None
    ips_field_discord_name: int | None
    fsa_poll_seconds: int
    fsa_rank_sync_minutes: int
    fsa_rank_roles: tuple[str, ...]  # the only Discord roles / website groups the bot may manage for FSA ranks
    website_rank_sync: bool  # also mirror ranks to website groups (needs member-edit API permission)
    website_member_group: str  # primary group for pilots on an automatic rank
    website_protected_groups: tuple[str, ...]  # primary groups the bot never changes

    data_dir: Path

    @property
    def website_root(self) -> str:
        """Public site root, derived from the API URL (https://flyviking.net/api/index.php -> https://flyviking.net/)."""
        return self.website_base_url.split("/api/", 1)[0].rstrip("/") + "/"


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(
    env: dict[str, str] | None = None,
    load_env_file: bool = True,
) -> Settings:
    """Read settings from `env` (tests) or the process environment plus `.env`. Raises ConfigError."""
    if env is None:
        if load_env_file:
            load_dotenv()
        env = dict(os.environ)

    def get(name: str, default: str | None = None) -> str | None:
        value = env.get(name)
        return value if value not in (None, "") else default

    def require(name: str) -> str:
        value = get(name)
        if not value:
            raise ConfigError(f"Missing required setting: {name}")
        return value

    def to_int(name: str, value: str) -> int:
        try:
            return int(value)
        except ValueError:
            raise ConfigError(f"{name} must be a whole number, got {value!r}") from None

    def optional_int(name: str) -> int | None:
        value = get(name)
        return to_int(name, value) if value else None

    def int_with_default(name: str, default: str) -> int:
        return to_int(name, get(name, default))

    return Settings(
        discord_token=require("DISCORD_TOKEN"),
        fsa_api_key=require("FSA_API_KEY"),
        fsa_va_id=int_with_default("FSA_VA_ID", "42982"),
        website_api_key=require("WEBSITE_API_KEY"),
        website_base_url=get("WEBSITE_BASE_URL", "https://flyviking.net/api/index.php"),
        uptime_kuma_url=get("UPTIME_KUMA_URL"),
        guild_id=optional_int("GUILD_ID"),
        channel_announcements=int_with_default("CHANNEL_ANNOUNCEMENTS", "631949277559783457"),
        channel_screenshots=int_with_default("CHANNEL_SCREENSHOTS", "631950027476172810"),
        webhook_secret=require("WEBHOOK_SECRET"),
        webhook_bind=get("WEBHOOK_BIND", "0.0.0.0"),
        webhook_port=int_with_default("WEBHOOK_PORT", "8080"),
        webhook_capture=_bool(get("WEBHOOK_CAPTURE", "false")),
        webhook_capture_keep=int_with_default("WEBHOOK_CAPTURE_KEEP", "50"),
        announcement_blog_id=optional_int("ANNOUNCEMENT_BLOG_ID"),
        channel_flight_logs=optional_int("CHANNEL_FLIGHT_LOGS"),
        channel_system_messages=optional_int("CHANNEL_SYSTEM_MESSAGES"),
        system_messages_role_id=optional_int("SYSTEM_MESSAGES_ROLE_ID"),
        ips_field_fsa_pilot_id=optional_int("IPS_FIELD_FSA_PILOT_ID"),
        ips_field_discord_name=optional_int("IPS_FIELD_DISCORD_NAME"),
        fsa_poll_seconds=max(60, int_with_default("FSA_POLL_SECONDS", "120")),
        fsa_rank_sync_minutes=max(5, int_with_default("FSA_RANK_SYNC_MINUTES", "60")),
        website_rank_sync=_bool(get("WEBSITE_RANK_SYNC", "true")),
        website_member_group=get("WEBSITE_MEMBER_GROUP", "Members"),
        website_protected_groups=tuple(
            n.strip() for n in get("WEBSITE_PROTECTED_GROUPS", "Administrators,Moderators").split(",")
            if n.strip()
        ),
        fsa_rank_roles=tuple(
            name.strip() for name in get("FSA_RANK_ROLES", DEFAULT_RANK_ROLES).split(",") if name.strip()
        ),
        data_dir=Path(get("DATA_DIR", "data")),
    )
