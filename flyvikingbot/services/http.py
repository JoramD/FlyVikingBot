import logging
from typing import Any

import aiohttp
from yarl import URL

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15)


class ServiceError(Exception):
    """Raised when an upstream service can't be reached or returns garbage."""


async def get_json(session: aiohttp.ClientSession, url: str | URL, **kwargs: Any) -> Any:
    """GET ``url`` and decode JSON. Raises ServiceError on any network/decode failure.

    Error responses (4xx/5xx) are still decoded, because both the website and
    FSAirlines APIs return error details as JSON. The query string (which may
    hold API keys) is never included in error messages.
    """
    safe_url = URL(str(url)).with_query(None)
    try:
        async with session.get(url, timeout=DEFAULT_TIMEOUT, **kwargs) as resp:
            return await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise ServiceError(f"Request to {safe_url} failed: {type(exc).__name__}") from exc
