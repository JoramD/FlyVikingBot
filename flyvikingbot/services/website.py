"""Client for the Invision Community REST API on flyviking.net."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import aiohttp
from yarl import URL

from .http import ServiceError, get_json


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
        data = await get_json(self._session, self.build_url(endpoint, **params))
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
