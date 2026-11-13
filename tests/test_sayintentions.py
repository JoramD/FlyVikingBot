"""SayIntentions client. The fixture follows the documented getWX example; replace it with a real
capture (python -m flyvikingbot.tools.si_sample) once available."""

import json
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from flyvikingbot.services.http import ServiceError
from flyvikingbot.services.sayintentions import SayIntentionsClient, _wind, parse_weather
from flyvikingbot.tools.si_sample import scrub

GETWX = json.loads((Path(__file__).parent / "fixtures" / "sayintentions" / "getWX.json").read_text(encoding="utf-8"))


def test_parse_weather_fixture():
    result = parse_weather(GETWX)
    ekch, engm = result["EKCH"], result["ENGM"]
    assert ekch.metar.startswith("EKCH 131150Z") and ekch.taf.startswith("TAF EKCH")
    assert ekch.atis.startswith("Kastrup information Alpha")
    assert ekch.active_runway == "22L" and ekch.wind == "240° at 12 kt"
    assert [(f.kind, f.freq, f.callsign) for f in ekch.frequencies] == [
        ("Tower", "118.100", "Kastrup Tower"), ("Ground", "121.900", "Kastrup Ground"),
    ]
    # Empty ATIS falls back to the CPDLC version; null TAF stays None
    assert engm.atis.startswith("ENGM ATIS B") and engm.taf is None and engm.wind == "Calm"
    assert len(engm.frequencies) == 1


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
    assert set(result) == {"EKCH", "ENGM"}
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
