import json
from pathlib import Path

import pytest

from flyvikingbot.config import load_settings

FIXTURES = Path(__file__).parent / "fixtures" / "ips"

BASE_ENV = {
    "DISCORD_TOKEN": "token",
    "FSA_API_KEY": "fsa",
    "WEBSITE_API_KEY": "web",
    "WEBHOOK_SECRET": "s" * 32,
    "FSA_VA_ID": "42982",
    "FSA_RANK_ROLES": "Trainee Pilot,First Officer,Senior First Officer,Cargo First Officer,Captain,Cargo Captain",
    "CHANNEL_ANNOUNCEMENTS": "100",
    "CHANNEL_SCREENSHOTS": "200",
}


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def settings(tmp_path):
    return load_settings(env={**BASE_ENV, "DATA_DIR": str(tmp_path)})
