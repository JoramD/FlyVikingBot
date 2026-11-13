"""Client for the Invision Community REST API on flyviking.net."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import aiohttp
from yarl import URL

from .http import ServiceError, request_json


@dataclass
class FileSearchResult:
    """Outcome of a downloads search."""

    total: int
    file: dict[str, Any] | None = None  # set only when exactly one match
    matches: list[dict[str, Any]] = field(default_factory=list)  # the first few files when several match


MAX_MATCHES = 5  # files listed when a search has several matches
CATEGORY_CACHE_SECONDS = 3600
CATEGORY_SEPARATOR = " › "


def category_path(categories: dict[int, tuple[str, int | None]], category_id: int) -> str | None:
    """'Paints › FlyViking' for a subcategory: its parents' names, top first. None if the ID is unknown."""
    names: list[str] = []
    seen: set[int] = set()
    current: int | None = category_id
    while current and current in categories and current not in seen:  # `seen` guards against loops
        seen.add(current)
        name, current = categories[current]
        names.insert(0, name)
    return CATEGORY_SEPARATOR.join(n for n in names if n) or None


class WebsiteClient:
    """Thin async client for the endpoints the bot uses. Every failure raises ServiceError."""

    def __init__(self, session: aiohttp.ClientSession, base_url: str, api_key: str) -> None:
        self._session = session
        self._base_url = base_url
        self._api_key = api_key
        self._categories: tuple[float, dict[int, tuple[str, int | None]]] | None = None

    def build_url(self, endpoint: str, **params: Any) -> URL:
        # IPS routes on the raw query string (index.php?/core/search&q=...), so the
        # endpoint must stay unencoded while the parameters are encoded normally.
        # The API key travels as ?key= by choice: it does appear in the website's access log,
        # which only the site owner can read. (HTTP Basic auth would avoid that, but depends on
        # the web server passing the Authorization header through to PHP.)
        query = urlencode({"key": self._api_key, **params})
        return URL(f"{self._base_url}?{endpoint}&{query}", encoded=True)

    async def _get(self, endpoint: str, **params: Any) -> Any:
        return await self._request("GET", endpoint, params)

    async def _request(self, method: str, endpoint: str, params: dict[str, Any], data: Any = None) -> Any:
        data = await request_json(self._session, method, self.build_url(endpoint, **params), data=data)
        if not isinstance(data, dict):
            raise ServiceError(f"Website returned unexpected data for {endpoint}")
        if "errorCode" in data:
            raise ServiceError(f"Website API error {data.get('errorCode')}: {data.get('errorMessage')}")
        return data

    async def search_files(self, query: str) -> FileSearchResult:
        """Search the downloads. One match: that file. Several: the first MAX_MATCHES files, fetched by ID
        (one call each, in parallel) because only the search result's itemId is known to be reliable."""
        data = await self._get(
            "/core/search", q=query, type="downloads_file", search_and_or="and", doNotTrack="0"
        )
        try:
            total = int(data.get("totalResults", 0))
            ids = list(dict.fromkeys(int(r["itemId"]) for r in data["results"][:MAX_MATCHES])) if total else []
            if total == 1 and not ids:
                raise IndexError("no result for the single match")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ServiceError(f"Website search returned unexpected data ({type(exc).__name__})") from exc
        if total == 1:
            return FileSearchResult(total=1, file=await self.get_file(ids[0]))
        files = await asyncio.gather(*(self.get_file(i) for i in ids), return_exceptions=True)
        if ids and all(isinstance(f, BaseException) for f in files):
            raise ServiceError("Website returned no data for the matching files")
        return FileSearchResult(total=total, matches=[f for f in files if isinstance(f, dict)])

    async def download_categories(self) -> dict[int, tuple[str, int | None]]:
        """Downloads categories as {id: (name, parent id or None)}, cached for an hour."""
        now = time.monotonic()
        if self._categories and now - self._categories[0] < CATEGORY_CACHE_SECONDS:
            return self._categories[1]
        categories: dict[int, tuple[str, int | None]] = {}
        async for page in self._pages("/downloads/categories"):
            for c in page:
                try:
                    parent = int(c.get("parentId") or 0)
                    categories[int(c["id"])] = (str(c.get("name") or ""), parent or None)
                except (KeyError, TypeError, ValueError):
                    continue
        self._categories = (now, categories)
        return categories

    async def get_file(self, file_id: int) -> dict[str, Any]:
        """A downloads file object (title, url, author, primaryScreenshot, ...)."""
        return await self._get(f"/downloads/files/{file_id}")

    # --- members & groups -------------------------------------------------------------------

    async def list_groups(self) -> list[tuple[int, str]]:
        """All member groups as (id, name)."""
        groups: list[tuple[int, str]] = []
        async for page in self._pages("/core/groups"):
            try:
                groups += [(int(g["id"]), str(g.get("name", ""))) for g in page if "id" in g]
            except (TypeError, ValueError) as exc:
                raise ServiceError("Website returned an unexpected group list") from exc
        return groups

    async def list_members(self) -> list[dict[str, Any]]:
        """All members (paged). Used for on-demand lookups; the API can't filter by profile field."""
        members: list[dict[str, Any]] = []
        async for page in self._pages("/core/members"):
            members += page
        return members

    async def _pages(self, endpoint: str, per_page: int = 100, max_pages: int = 100):
        """Yield the `results` of each page of a paged IPS endpoint (capped as a safety net)."""
        for page in range(1, max_pages + 1):
            data = await self._get(endpoint, perPage=per_page, page=page)
            results = data.get("results")
            yield [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []
            try:
                total_pages = int(data.get("totalPages", 1) or 1)
            except (TypeError, ValueError):
                total_pages = 1
            if page >= total_pages:
                return

    async def get_member(self, member_id: int) -> dict[str, Any]:
        """A member object (groups, profile fields, profileUrl, ...)."""
        return await self._get(f"/core/members/{member_id}")

    async def update_member_groups(
        self, member_id: int, *, primary: int | None = None, secondary: list[int] | None = None
    ) -> dict[str, Any]:
        """Set a member's primary group and/or replace their secondary groups (None = leave unchanged).
        Nothing else about the member is changed."""
        form: list[tuple[str, str]] = []
        if primary is not None:
            form.append(("group", str(primary)))
        if secondary is not None:
            form += [("secondaryGroups[]", str(g)) for g in secondary] or [("secondaryGroups", "")]
        if not form:
            return {}
        return await self._request("POST", f"/core/members/{member_id}", {}, data=form)


    async def update_member_field(self, member_id: int, field_id: int, value: str) -> dict[str, Any]:
        """Set one custom profile field (an empty value clears it). Nothing else about the member is changed."""
        form = [(f"customFields[{field_id}]", value)]
        return await self._request("POST", f"/core/members/{member_id}", {}, data=form)


def member_group_ids(member: dict[str, Any]) -> tuple[int | None, set[int]]:
    """(primary group ID, secondary group IDs) from an IPS member object."""
    primary = member.get("primaryGroup") or {}
    secondary = member.get("secondaryGroups") or []
    primary_id = int(primary["id"]) if isinstance(primary, dict) and "id" in primary else None
    return primary_id, {int(g["id"]) for g in secondary if isinstance(g, dict) and "id" in g}
