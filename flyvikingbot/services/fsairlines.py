"""Client for the FSAirlines XML-Interface v2 (JSON mode).

Docs: https://wiki.fsairlines.net/index.php/XML-Interface-v2

Responses look like {"status": "SUCCESS", "data": [...]}. Parsing is verified against real
responses (tests/fixtures/fsa/) and tolerates numbers sent as strings or missing fields.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import aiohttp

from .http import ServiceError, get_json

API_URL = "https://www.fsairlines.net/va_interface2.php"
SITE_URL = "https://www.fsairlines.net/"
RANK_CACHE_SECONDS = 3600


class FsaNotFound(ServiceError):
    """The requested pilot/flight doesn't exist."""


# --- parsing helpers --------------------------------------------------------


def _str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if isinstance(value, str) and ":" in value:  # "HH:MM"
        try:
            h, m = value.split(":", 1)
            return int(h) + int(m[:2]) / 60
        except ValueError:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _datetime(value: Any) -> datetime | None:
    """FSA timestamps may be unix seconds or 'YYYY-MM-DD HH:MM:SS' (assumed UTC)."""
    if (ts := _int(value)) is not None and ts > 10**8:
        return datetime.fromtimestamp(ts, UTC)
    if s := _str(value):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


# --- models -----------------------------------------------------------------


@dataclass(frozen=True)
class FlightLog:
    """A completed flight (FSA calls these 'flight reports')."""

    id: int
    number: str | None
    dep: str | None
    arr: str | None
    planned_arr: str | None
    aircraft: str | None
    pilot_id: str | None
    pilot_name: str | None
    hours: float | None
    distance: float | None
    rating: float | None  # flight rating in percent (0-100)
    pax: int | None
    cargo_kg: int | None
    landing_rate: int | None  # vertical speed at touchdown (fpm)
    completed_at: datetime | None
    callsign: str | None = None  # pilot's VA callsign, e.g. FVK004
    packages_kg: int | None = None
    sim: str | None = None  # e.g. FS2024

    def merged(self, detail: FlightLog) -> FlightLog:
        """Fill gaps in this (list) record with values from the detail record."""
        return FlightLog(**{k: getattr(self, k) if getattr(self, k) is not None else getattr(detail, k)
                            for k in self.__dataclass_fields__})


@dataclass(frozen=True)
class Pilot:
    id: str
    user: str | None  # FSAirlines username
    name: str | None
    surname: str | None
    rank: str | None  # FSA returns the rank's short name here (e.g. "CEO")
    rank_id: int | None
    flights: int | None
    hours: float | None
    rating_total: float | None  # sum of all flight ratings, not an average
    distance: float | None
    location: str | None
    last_active: datetime | None
    callsign: str | None = None  # VA callsign, e.g. FVK003
    fsa_title: str | None = None  # FSAirlines-wide title, e.g. "Senior Flight Captain"
    pax: int | None = None
    cargo_kg: int | None = None  # cargo + packages
    picture_url: str | None = None

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.name, self.surname) if p) or self.user or f"Pilot {self.id}"

    @property
    def average_rating(self) -> float | None:
        if self.rating_total is None or not self.flights:
            return None
        return self.rating_total / self.flights


@dataclass(frozen=True)
class PilotSummary:
    """Entry of getPilotList."""

    id: str
    user: str | None
    name: str | None
    surname: str | None
    rank_id: int | None
    callsign: str | None = None


@dataclass(frozen=True)
class Rank:
    id: int
    name: str
    short: str | None


def parse_flight_log(d: dict[str, Any]) -> FlightLog | None:
    flight_id = _int(d.get("id"))
    if flight_id is None:
        return None
    return FlightLog(
        id=flight_id,
        number=_str(d.get("number")),
        dep=_str(d.get("dep")),
        arr=_str(d.get("arr")),
        planned_arr=_str(d.get("pln_arr")),
        aircraft=_str(d.get("ac_type")),
        pilot_id=_str(d.get("pilot_id")),
        pilot_name=_str(d.get("pic")),
        hours=_float(d.get("hours")),
        distance=_float(d.get("distance")),
        rating=_float(d.get("rating")),
        pax=_int(d.get("pax")),
        cargo_kg=_int(d.get("cargo_kg")),
        landing_rate=_int(d.get("vs")),
        completed_at=_datetime(d.get("ts")),  # deptime/arrtime are HHMM clock times, not timestamps
        callsign=_str(d.get("pid")),
        packages_kg=_int(d.get("packages_kg")),
        sim=_str(d.get("simversion")),
    )


def parse_pilot(d: dict[str, Any]) -> Pilot | None:
    pilot_id = _str(d.get("id"))
    if pilot_id is None:
        return None
    cargo = [v for v in (_int(d.get("cargo_kg")), _int(d.get("packages_kg"))) if v is not None]
    picture = _str(d.get("picture"))
    return Pilot(
        id=pilot_id,
        user=_str(d.get("user")),
        name=_str(d.get("name")),
        surname=_str(d.get("surname")),
        rank=_str(d.get("rank")),
        rank_id=_int(d.get("rank_id")),
        flights=_int(d.get("flights")),
        hours=_float(d.get("hours")),
        rating_total=_float(d.get("rating")),
        distance=_float(d.get("distance")),
        location=_str(d.get("location")),
        last_active=_datetime(d.get("lastactive") or d.get("last_active")),
        callsign=_str(d.get("va_user")),
        fsa_title=_str(d.get("fsa_rating")),
        pax=_int(d.get("pax")),
        cargo_kg=sum(cargo) if cargo else None,
        picture_url=(picture if picture.startswith("http") else SITE_URL + picture.lstrip("/")) if picture else None,
    )


def parse_pilot_summary(d: dict[str, Any]) -> PilotSummary | None:
    pilot_id = _str(d.get("id"))
    if pilot_id is None:
        return None
    return PilotSummary(
        id=pilot_id,
        user=_str(d.get("user")),
        name=_str(d.get("name")),
        surname=_str(d.get("surname")),
        rank_id=_int(d.get("rank_id")),
        callsign=_str(d.get("va_user")),
    )


def parse_rank(d: dict[str, Any]) -> Rank | None:
    rank_id, name = _int(d.get("id")), _str(d.get("name"))
    if rank_id is None or not name:
        return None
    return Rank(id=rank_id, name=name, short=_str(d.get("short")))


def _records(data: Any) -> list[dict[str, Any]]:
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


# --- client -----------------------------------------------------------------


class FsaClient:
    """Async client; every failure raises ServiceError (FsaNotFound for unknown pilots)."""

    def __init__(self, session: aiohttp.ClientSession, va_id: int, api_key: str, api_url: str = API_URL) -> None:
        self._session = session
        self._va_id = va_id
        self._api_key = api_key
        self._api_url = api_url
        self._ranks: tuple[float, list[Rank]] | None = None

    async def call(self, function: str, **params: Any) -> list[dict[str, Any]]:
        """Call an XML-Interface function and return its data records."""
        query = {"function": function, "va_id": self._va_id, "format": "json", "apikey": self._api_key}
        query.update({k: v for k, v in params.items() if v is not None})
        data = await get_json(self._session, self._api_url, params=query)
        if not isinstance(data, dict):
            raise ServiceError(f"FSAirlines {function}: unexpected response type {type(data).__name__}")
        status = str(data.get("status", "")).upper()
        if status == "SUCCESS":
            return _records(data.get("data"))
        if "NOT FOUND" in status:
            raise FsaNotFound(f"FSAirlines {function}: {status}")
        raise ServiceError(f"FSAirlines {function} failed (status={status or 'missing'})")

    async def airline_stats(self) -> dict[str, Any]:
        records = await self.call("getAirlineStats")
        if not records:
            raise ServiceError("FSAirlines getAirlineStats returned no data")
        return records[0]

    async def flight_logs(self, count: int = 15, pilot_id: str | None = None) -> list[FlightLog]:
        records = await self.call("getFlightReports", count=count, pilot_id=pilot_id)
        logs = [log for r in records if (log := parse_flight_log(r))]
        return sorted(logs, key=lambda log: log.id)

    async def flight_detail(self, flight_id: int) -> FlightLog | None:
        records = await self.call("getReportDetail", report_id=flight_id)
        return parse_flight_log(records[0]) if records else None

    async def pilot(self, pilot_id: str) -> Pilot:
        records = await self.call("getPilotData", pilot_id=pilot_id)
        pilot = parse_pilot(records[0]) if records else None
        if pilot is None:
            raise FsaNotFound(f"FSAirlines pilot {pilot_id} not found")
        return pilot

    async def resolve_pilot_id(self, value: str) -> str | None:
        """Turn what a member typed into an FSA pilot ID: a numeric ID is used as-is; a callsign (FVK003)
        or FSAirlines username is looked up in the pilot list (one API call). None if unknown."""
        value = value.strip()
        if value.isdigit():
            return value
        wanted = value.casefold()
        for pilot in await self.pilot_list():
            if wanted in {(pilot.callsign or "").casefold(), (pilot.user or "").casefold()}:
                return pilot.id
        return None

    async def pilot_list(self) -> list[PilotSummary]:
        return [p for r in await self.call("getPilotList") if (p := parse_pilot_summary(r))]

    async def ranks(self) -> list[Rank]:
        if self._ranks and time.monotonic() - self._ranks[0] < RANK_CACHE_SECONDS:
            return self._ranks[1]
        ranks = [r for d in await self.call("getRankList") if (r := parse_rank(d))]
        self._ranks = (time.monotonic(), ranks)
        return ranks
