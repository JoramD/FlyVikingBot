from datetime import UTC, datetime

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from flyvikingbot.services.fsairlines import FsaClient, FsaNotFound, parse_flight_log, parse_pilot
from flyvikingbot.services.http import ServiceError

KEY = "secret-fsa-key"


class FakeFsa:
    def __init__(self):
        self.responses = {}  # function -> json body
        self.calls = []

    async def handle(self, request: web.Request) -> web.Response:
        q = request.query
        assert q["apikey"] == KEY and q["format"] == "json" and q["va_id"] == "42982"
        self.calls.append(dict(q))
        body = self.responses.get(q["function"], {"status": f"UNKNOWN FUNCTION {q['function'].upper()}", "data": None})
        return web.json_response(body)


@pytest.fixture
async def fsa():
    fake = FakeFsa()
    app = web.Application()
    app.router.add_get("/va_interface2.php", fake.handle)
    server = TestServer(app)
    await server.start_server()
    async with aiohttp.ClientSession() as session:
        yield fake, FsaClient(session, 42982, KEY, api_url=str(server.make_url("/va_interface2.php")))
    await server.close()


async def test_airline_stats(fsa):
    fake, client = fsa
    fake.responses["getAirlineStats"] = {"status": "SUCCESS", "data": [{"flights": "10"}]}
    assert (await client.airline_stats())["flights"] == "10"


async def test_flight_logs_sorted_and_parsed(fsa):
    fake, client = fsa
    fake.responses["getFlightReports"] = {
        "status": "SUCCESS",
        "data": [
            {"id": "12", "dep": "BIKF", "arr": "EGLL", "hours": "2.5", "pax": "150", "pilot_id": "7", "pic": "Jan"},
            {"id": "10", "dep": "EGLL", "arr": "BIKF", "hours": "02:45", "ts": "1794500000"},
        ],
    }
    logs = await client.flight_logs(count=2, pilot_id="7")
    assert [f.id for f in logs] == [10, 12]
    assert logs[0].hours == pytest.approx(2.75) and logs[0].completed_at.year == 2026
    assert logs[1].pax == 150 and logs[1].pilot_name == "Jan"
    assert fake.calls[-1]["count"] == "2" and fake.calls[-1]["pilot_id"] == "7"


async def test_pilot_not_found(fsa):
    fake, client = fsa
    fake.responses["getPilotData"] = {"status": "NOT FOUND", "data": None}
    with pytest.raises(FsaNotFound):
        await client.pilot("999")


async def test_error_status_does_not_leak_key(fsa):
    _, client = fsa
    with pytest.raises(ServiceError) as info:
        await client.pilot_list()
    assert "UNKNOWN FUNCTION" in str(info.value) and KEY not in str(info.value)


async def test_ranks_cached(fsa):
    fake, client = fsa
    fake.responses["getRankList"] = {
        "status": "SUCCESS",
        "data": [{"id": "1", "name": "Cadet", "short": "CDT"}, {"id": "2", "name": "Captain"}],
    }
    first = await client.ranks()
    second = await client.ranks()
    assert [r.name for r in first] == ["Cadet", "Captain"] and first is second
    assert sum(c["function"] == "getRankList" for c in fake.calls) == 1


def test_parsers_are_defensive():
    assert parse_flight_log({"dep": "BIKF"}) is None  # no id
    log = parse_flight_log({"id": 5, "vs": "-142", "hours": "bad", "ts": "2026-11-12 18:00:00"})
    assert log.landing_rate == -142 and log.hours is None
    assert log.completed_at == datetime(2026, 11, 12, 18, 0, tzinfo=UTC)
    pilot = parse_pilot({"id": 3, "name": "Jöram", "surname": "Davids", "rank_id": "4", "hours": "1203.5"})
    assert pilot.full_name == "Jöram Davids" and pilot.rank_id == 4 and pilot.hours == 1203.5
