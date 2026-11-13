"""SayIntentions client. The fixture is a real getWX capture for EKCH (tools/si_sample)."""

import json
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from flyvikingbot.services.http import ServiceError
from flyvikingbot.services.sayintentions import Frequency, SayIntentionsClient, _wind, parse_weather
from flyvikingbot.tools.si_sample import scrub

GETWX = json.loads((Path(__file__).parent / "fixtures" / "sayintentions" / "getWX.json").read_text(encoding="utf-8"))


def test_parse_weather_real_capture():
    ekch = parse_weather(GETWX)["EKCH"]
    assert ekch.metar == "EKCH 132120Z AUTO 27012KT 9999 NCD 05/M00 Q1006 NOSIG"
    assert ekch.taf.startswith("TAF EKCH 132027Z")
    assert ekch.atis.startswith("COPENHAGEN KASTRUP ARPT, INFO DELTA.")
    assert (ekch.runways_arriving, ekch.runways_departing) == ("22L", "22R")
    assert ekch.wind == "270° at 12 kt"  # wind_gusting is null
    assert len(ekch.frequencies) == 22
    assert ekch.frequencies[0] == Frequency("APP", "118.455", "COPENHAGEN")
    assert ekch.frequencies[3] == Frequency("ATIS", "122.755", None)


def test_parse_weather_documented_fallbacks():
    """The docs show active_runway (not the arriving/departing pair) and an empty atis with atis_cpdlc."""
    w = parse_weather({"airports": [{"airport": "ENGM", "active_runway": "01L", "atis": "", "atis_cpdlc": "ENGM B"}]})
    engm = w["ENGM"]
    assert (engm.runways_arriving, engm.runways_departing) == ("01L", "01L") and engm.atis == "ENGM B"


@pytest.mark.parametrize("data", [None, [], "error", {"airports": "x"}, {"airports": [None, {"metar": "x"}]}])
def test_parse_weather_junk(data):
    assert parse_weather(data) == {}


def test_parse_weather_missing_fields():
    w = parse_weather({"airports": [{"airport": "ekch"}]})["EKCH"]
    assert w.metar is None and w.wind is None and w.frequencies == () and w.is_empty


@pytest.mark.parametrize(
    ("direction", "speed", "text"),
    [(240, 12, "240° at 12 kt"), ("5", "7.6", "005° at 8 kt"), (360, 4, "360° at 4 kt"), (0, 4, "360° at 4 kt"),
     ("VRB", 3, "Variable at 3 kt"), (None, 5, "5 kt"), (180, 0, "Calm"), (180, None, None), (180, "x", None)],
)
def test_wind(direction, speed, text):
    assert _wind(direction, speed) == text


@pytest.mark.parametrize(("gust", "text"), [(25, "250° at 15 kt, gusting 25 kt"), (None, "250° at 15 kt"),
                                            (15, "250° at 15 kt"), ("x", "250° at 15 kt")])
def test_wind_gusts(gust, text):
    assert _wind(250, 15, gust) == text


def test_sample_tool_scrubs_keys():
    data = {"flight": {"api_key": "secret", "Email": "a@b.c", "metar": "x"}, "list": [{"va_api_key": "s"}]}
    assert scrub(data) == {
        "flight": {"api_key": "<redacted>", "Email": "<redacted>", "metar": "x"},
        "list": [{"va_api_key": "<redacted>"}],
    }


class FakeSi:
    def __init__(self):
        self.response = (200, GETWX)
        self.requests: list[web.Request] = []

    async def getwx(self, request: web.Request) -> web.Response:
        self.requests.append(request)
        status, body = self.response
        return web.json_response(body, status=status)


@pytest.fixture
async def si():
    fake = FakeSi()
    app = web.Application()
    app.router.add_get("/sapi/getWX", fake.getwx)
    server = TestServer(app)
    await server.start_server()
    async with aiohttp.ClientSession() as session:
        yield fake, SayIntentionsClient(session, "app-key", "account-key", api_url=str(server.make_url("/sapi/")))
    await server.close()


async def test_weather_request_carries_both_keys(si):
    fake, client = si
    result = await client.weather(["EKCH", "ENGM"])
    assert set(result) == {"EKCH"}  # the capture only has EKCH, so ENGM counts as not found
    request = fake.requests[0]
    assert request.headers["X-Developer-Key"] == "app-key"
    assert request.query["api_key"] == "account-key"
    assert request.query["icao"] == "EKCH,ENGM" and request.query["with_comms"] == "1"


async def test_weather_is_cached_per_airport(si):
    fake, client = si
    await client.weather(["EKCH"])
    assert set(await client.weather(["EKCH"])) == {"EKCH"}
    assert len(fake.requests) == 1  # second call came from the cache
    await client.weather(["EKCH", "ENGM"])
    assert fake.requests[1].query["icao"] == "ENGM"  # only the uncached airport is requested


async def test_unknown_airports_are_left_out(si):
    fake, client = si
    fake.response = (200, {"airports": [{"airport": "EKXX"}], "comms": []})
    assert await client.weather(["EKXX"]) == {}  # no data at all counts as not found


@pytest.mark.parametrize("response", [(401, {"error": "Invalid developer key"}), (200, ["nope"]), (500, {})])
async def test_errors_become_service_error(si, response):
    fake, client = si
    fake.response = response
    with pytest.raises(ServiceError) as info:
        await client.weather(["EKCH"])
    assert "account-key" not in str(info.value) and "app-key" not in str(info.value)
