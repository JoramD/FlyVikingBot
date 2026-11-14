"""Shared HTTP helpers. Failures become ServiceError; URLs in errors never include the query
string, because that's where the API keys travel."""

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiohttp
from yarl import URL

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15)


class ServiceError(Exception):
    """Raised when an upstream service can't be reached or returns garbage."""


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds") if ts else None


@dataclass
class ServiceHealth:
    """How the latest requests to one outside service went, for /health and the Uptime Kuma message.
    Only reaching the service counts: an API error answer (e.g. "not found") still means it's up."""

    name: str
    last_ok: float | None = None  # unix time of the last successful request
    last_error: float | None = None
    error: str | None = None  # the last failure (URL without query string, so no keys)
    failing_since: float | None = None  # set while the latest request failed

    @property
    def failing(self) -> bool:
        return self.failing_since is not None

    def record_ok(self) -> None:
        self.last_ok, self.failing_since, self.error = time.time(), None, None

    def record_failure(self, error: str) -> None:
        self.last_error, self.error = time.time(), error
        if self.failing_since is None:
            self.failing_since = self.last_error

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": not self.failing,
            "last_ok": _iso(self.last_ok),
            "failing_since": _iso(self.failing_since),
            "error": self.error,
        }


async def get_json(
    session: aiohttp.ClientSession, url: str | URL, *, health: ServiceHealth | None = None, **kwargs: Any
) -> Any:
    """GET ``url`` and decode JSON. Raises ServiceError on any network/decode failure.

    Error responses (4xx/5xx) are still decoded, because both the website and
    FSAirlines APIs return error details as JSON. The query string (which may
    hold API keys) is never included in error messages.
    """
    return await request_json(session, "GET", url, health=health, **kwargs)


async def request_json(
    session: aiohttp.ClientSession, method: str, url: str | URL, *, health: ServiceHealth | None = None,
    **kwargs: Any,
) -> Any:
    """Like get_json, for any HTTP method (e.g. POST with data=...). `health` records whether the
    service could be reached."""
    safe_url = URL(str(url)).with_query(None)
    try:
        async with session.request(method, url, timeout=DEFAULT_TIMEOUT, **kwargs) as resp:
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        error = f"{method} {safe_url} failed: {type(exc).__name__}"
        if health is not None:
            health.record_failure(error)
        raise ServiceError(error) from exc
    if health is not None:
        health.record_ok()
    return data
