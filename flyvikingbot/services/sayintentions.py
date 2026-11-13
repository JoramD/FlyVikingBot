"""SayIntentions.AI SAPI client (https://p2.sayintentions.ai/p2/docs/).

SayIntentions needs two keys on every call:
- the app key (sent as the X-Developer-Key header) identifies this bot as an application;
- the account key (sent as the `api_key` parameter) identifies the SayIntentions account whose
  subscription is used. Because the bot only ever uses its own account, a personal app key is enough.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from .http import ServiceError, get_json

log = logging.getLogger(__name__)

API_URL = "https://apipri.sayintentions.ai/sapi/"
WEATHER_CACHE_SECONDS = 300  # the docs ask integrations to cache weather


@dataclass(frozen=True)
class Frequency:
    kind: str | None  # SayIntentions type code, e.g. "TWR", "GND", "APP", "AIR"
    freq: str  # e.g. "118.58"
    callsign: str | None  # e.g. "KASTRUP"; often missing for ATIS and ground


@dataclass(frozen=True)
class AirportWeather:
    icao: str
    metar: str | None = None
    taf: str | None = None
    atis: str | None = None
    runways_arriving: str | None = None
    runways_departing: str | None = None
    wind: str | None = None
    frequencies: tuple[Frequency, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not any((self.metar, self.taf, self.atis, self.runways_arriving, self.runways_departing, self.wind))


def _str(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    return text or None


def _knots(value: Any) -> int | None:
    text = _str(value)
    try:
        return round(float(text)) if text else None
    except ValueError:
        return None


def _wind(direction: Any, speed: Any, gust: Any = None) -> str | None:
    """'240° at 12 kt', '250° at 15 kt, gusting 25 kt', 'Variable at 3 kt', 'Calm';
    None when SayIntentions gives nothing usable."""
    knots = _knots(speed)
    if knots is None:
        return None
    if knots == 0:
        return "Calm"
    dir_text = _str(direction)
    try:
        heading = f"{(round(float(dir_text)) % 360) or 360:03d}°" if dir_text else None  # north is 360
    except ValueError:
        heading = "Variable" if dir_text.upper().startswith("VR") else None
    text = f"{heading} at {knots} kt" if heading else f"{knots} kt"
    gusts = _knots(gust)
    return f"{text}, gusting {gusts} kt" if gusts and gusts > knots else text


def parse_weather(data: Any) -> dict[str, AirportWeather]:
    """getWX response -> weather per ICAO. Unknown or missing fields become None."""
    if not isinstance(data, dict):
        return {}
    comms: dict[str, list[Frequency]] = {}
    for c in data.get("comms") or []:
        if isinstance(c, dict) and (icao := _str(c.get("airport"))) and (freq := _str(c.get("freq"))):
            comms.setdefault(icao.upper(), []).append(Frequency(_str(c.get("type")), freq, _str(c.get("callsign"))))
    result: dict[str, AirportWeather] = {}
    for a in data.get("airports") or []:
        if not isinstance(a, dict) or not (icao := _str(a.get("airport"))):
            continue
        icao = icao.upper()
        result[icao] = AirportWeather(
            icao=icao,
            metar=_str(a.get("metar")),
            taf=_str(a.get("taf")),
            atis=_str(a.get("atis")) or _str(a.get("atis_cpdlc")),
            # The live API sends separate arriving/departing runways; the docs only mention active_runway
            runways_arriving=_str(a.get("active_runways_arriving")) or _str(a.get("active_runway")),
            runways_departing=_str(a.get("active_runways_departing")) or _str(a.get("active_runway")),
            wind=_wind(a.get("wind_direction"), a.get("wind_speed"), a.get("wind_gusting")),
            frequencies=tuple(comms.get(icao, ())),
        )
    return result


def _error_text(data: Any) -> str:
    """A short description of an error body, for the log (never contains our keys)."""
    if isinstance(data, dict):
        for key in ("error", "message", "msg", "status"):
            if text := _str(data.get(key)):
                return text[:200]
    return f"unexpected response ({type(data).__name__})"


class SayIntentionsClient:
    """Async client; every failure raises ServiceError."""

    def __init__(
        self, session: aiohttp.ClientSession, app_key: str, account_key: str, api_url: str = API_URL
    ) -> None:
        self._session = session
        self._app_key = app_key
        self._account_key = account_key
        self._api_url = api_url
        self._weather_cache: dict[str, tuple[float, AirportWeather]] = {}

    async def call(self, endpoint: str, **params: Any) -> Any:
        """GET a SAPI endpoint and return the decoded JSON as-is."""
        query = {"api_key": self._account_key, **{k: v for k, v in params.items() if v is not None}}
        return await get_json(
            self._session, self._api_url + endpoint, params=query, headers={"X-Developer-Key": self._app_key}
        )

    async def weather(self, icaos: list[str]) -> dict[str, AirportWeather]:
        """Weather for these airports (missing from the result = SayIntentions knows nothing about it).
        Airports fetched in the last few minutes come from the cache; the rest share one request."""
        now = time.monotonic()
        cached: dict[str, AirportWeather] = {}
        for icao in icaos:
            hit = self._weather_cache.get(icao)
            if hit and now - hit[0] < WEATHER_CACHE_SECONDS:
                cached[icao] = hit[1]
        wanted = [i for i in icaos if i not in cached]
        if not wanted:
            return cached
        data = await self.call("getWX", icao=",".join(wanted), with_comms=1)
        if not isinstance(data, dict) or not isinstance(data.get("airports"), list):
            # Bad keys, an expired subscription and unknown airports all end up here
            raise ServiceError(f"SayIntentions getWX failed: {_error_text(data)}")
        fresh = {i: w for i, w in parse_weather(data).items() if i in wanted and not w.is_empty}
        for icao, w in fresh.items():
            self._weather_cache[icao] = (now, w)
        return {**cached, **fresh}
