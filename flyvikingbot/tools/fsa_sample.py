"""Capture real FSAirlines API responses for test fixtures. Run ON THE SERVER only:

    docker compose exec discord-bot python -m flyvikingbot.tools.fsa_sample [pilot_id]

Makes about 5 API calls and writes data/fsa_samples/<function>.json. Pilot names and
usernames are replaced with placeholders; callsigns and IDs are kept. (A site API key
locks to one IP after 100 calls, so don't run this from a development machine.)
"""

from __future__ import annotations

import asyncio
import json
import sys

import aiohttp

from ..config import load_settings
from ..services.fsairlines import FsaClient
from ..services.http import ServiceError
from ..webhooks.server import redact

# FSA fields holding a pilot's real name or FSAirlines username
NAME_FIELDS = {"name", "surname", "user", "pic"}


def anonymise(records: list[dict]) -> list[dict]:
    """Redact personal data (emails etc.) and pilot names, keeping the structure intact."""
    return [{k: ("<redacted>" if k in NAME_FIELDS and v else v) for k, v in redact(r).items()} for r in records]


async def main(pilot_id: str | None) -> None:
    settings = load_settings()
    out = settings.data_dir / "fsa_samples"
    out.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession(headers={"User-Agent": "FlyVikingBot"}) as session:
        fsa = FsaClient(session, settings.fsa_va_id, settings.fsa_api_key)

        async def dump(function: str, **params) -> list[dict]:
            try:
                records = await fsa.call(function, **params)
            except ServiceError as exc:
                print(f"{function}: {exc}")
                return []
            (out / f"{function}.json").write_text(json.dumps(anonymise(records[:5]), indent=2), encoding="utf-8")
            print(f"{function}: {len(records)} record(s) -> {out / (function + '.json')}")
            return records

        reports = await dump("getFlightReports", count=3)
        if reports and reports[0].get("id"):
            await dump("getReportDetail", report_id=reports[0]["id"])
        pilots = await dump("getPilotList")
        pilot_id = pilot_id or (str(pilots[0]["id"]) if pilots and pilots[0].get("id") else None)
        if pilot_id:
            await dump("getPilotData", pilot_id=pilot_id)
        await dump("getRankList")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
