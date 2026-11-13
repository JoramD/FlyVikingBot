"""Capture a real SayIntentions getWX response for test fixtures. Run on the server:

    docker compose exec bot python -m flyvikingbot.tools.si_sample [ICAO ...]

Makes one API call (default airports: EKCH ENGM) and writes data/sayintentions_samples/getWX.json.
The keys are only sent in the request; any key-like field in the response is redacted.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import aiohttp

from ..config import load_settings
from ..services.http import ServiceError
from ..services.sayintentions import SayIntentionsClient

KEY_FIELDS = {"api_key", "va_api_key", "developer_key", "email"}


def scrub(value: Any) -> Any:
    """Redact key-like fields anywhere in the response, keeping the structure intact."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if k.lower() in KEY_FIELDS and v else scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


async def main(icaos: list[str]) -> int:
    settings = load_settings()
    if not (settings.sayintentions_app_key and settings.sayintentions_account_key):
        print("Set SAYINTENTIONS_APP_KEY and SAYINTENTIONS_ACCOUNT_KEY in .env first.")
        return 1
    out = settings.data_dir / "sayintentions_samples"
    out.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession(headers={"User-Agent": "FlyVikingBot"}) as session:
        client = SayIntentionsClient(session, settings.sayintentions_app_key, settings.sayintentions_account_key)
        try:
            data = await client.call("getWX", icao=",".join(icaos), with_comms=1)
        except ServiceError as exc:
            print(f"getWX: {exc}")
            return 1
    path = out / "getWX.json"
    path.write_text(json.dumps(scrub(data), indent=2), encoding="utf-8")
    print(f"getWX ({', '.join(icaos)}) -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main([a.upper() for a in sys.argv[1:]] or ["EKCH", "ENGM"])))
