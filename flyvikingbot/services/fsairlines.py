from typing import Any

import aiohttp

from .http import ServiceError, get_json

API_URL = "https://www.fsairlines.net/va_interface2.php"


async def get_airline_stats(
    session: aiohttp.ClientSession, va_id: int, api_key: str, api_url: str = API_URL
) -> dict[str, Any]:
    params = {"function": "getAirlineStats", "va_id": va_id, "format": "json", "apikey": api_key}
    data = await get_json(session, api_url, params=params)
    if not isinstance(data, dict) or data.get("status") != "SUCCESS" or not data.get("data"):
        status = data.get("status") if isinstance(data, dict) else type(data).__name__
        raise ServiceError(f"FSAirlines returned an unexpected response (status={status})")
    return data["data"][0]
