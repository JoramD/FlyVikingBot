"""Client for the Invision Community REST API on flyviking.net."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import aiohttp
from yarl import URL

from .http import ServiceError, request_json


@dataclass
class FileSearchResult:
    total: int
    file: dict[str, Any] | None = None  # set only when exactly one match


class WebsiteClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, api_key: str) -> None:
        self._session = session
        self._base_url = base_url
        self._api_key = api_key

    def build_url(self, endpoint: str, **params: Any) -> URL:
        # IPS routes on the raw query string (index.php?/core/search&q=...), so the
        # endpoint must stay unencoded while the parameters are encoded normally.
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
        data = await self._get(
            "/core/search", q=query, type="downloads_file", search_and_or="and", doNotTrack="0"
        )
        total = int(data.get("totalResults", 0))
        if total != 1:
            return FileSearchResult(total=total)
        item_id = data["results"][0]["itemId"]
        return FileSearchResult(total=1, file=await self.get_file(item_id))

    async def get_file(self, file_id: int) -> dict[str, Any]:
        return await self._get(f"/downloads/files/{file_id}")

    # --- members & groups -------------------------------------------------------------------

    async def list_groups(self) -> list[tuple[int, str]]:
        """All member groups as (id, name)."""
        groups, page = [], 1
        while True:
            data = await self._get("/core/groups", perPage=100, page=page)
            groups += [(int(g["id"]), str(g.get("name", ""))) for g in data.get("results", []) if "id" in g]
            if page >= int(data.get("totalPages", 1) or 1):
                return groups
            page += 1

    async def list_members(self) -> list[dict[str, Any]]:
        """All members (paged). Used for on-demand lookups; the API can't filter by profile field."""
        members, page = [], 1
        while True:
            data = await self._get("/core/members", perPage=100, page=page)
            members += [m for m in data.get("results", []) if isinstance(m, dict)]
            if page >= int(data.get("totalPages", 1) or 1):
                return members
            page += 1

    async def get_member(self, member_id: int) -> dict[str, Any]:
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


def member_group_ids(member: dict[str, Any]) -> tuple[int | None, set[int]]:
    """(primary group ID, secondary group IDs) from an IPS member object."""
    primary = member.get("primaryGroup") or {}
    secondary = member.get("secondaryGroups") or []
    primary_id = int(primary["id"]) if isinstance(primary, dict) and "id" in primary else None
    return primary_id, {int(g["id"]) for g in secondary if isinstance(g, dict) and "id" in g}
