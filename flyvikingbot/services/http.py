"""Shared HTTP helpers. Failures become ServiceError; URLs in errors never include the query
string, because that's where the API keys travel."""

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
    return await request_json(session, "GET", url, **kwargs)


async def request_json(session: aiohttp.ClientSession, method: str, url: str | URL, **kwargs: Any) -> Any:
    """Like get_json, for any HTTP method (e.g. POST with data=...)."""
    safe_url = URL(str(url)).with_query(None)
    try:
        async with session.request(method, url, timeout=DEFAULT_TIMEOUT, **kwargs) as resp:
            return await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        raise ServiceError(f"{method} {safe_url} failed: {type(exc).__name__}") from exc
