"""Parsing and display against real FSAirlines responses (names anonymised)."""

import json
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from flyvikingbot.cogs.flight_logs import flight_log_embed
from flyvikingbot.cogs.pilots import pilot_embed
from flyvikingbot.linking import names_match
from flyvikingbot.services.fsairlines import FsaClient, parse_flight_log, parse_pilot, parse_rank

FIXTURES = Path(__file__).parent / "fixtures" / "fsa"


def fixture(function):
    return json.loads((FIXTURES / f"{function}.json").read_text(encoding="utf-8"))


@pytest.fixture
async def fsa():
    async def handler(request: web.Request) -> web.Response:
        return web.json_response(fixture(request.query["function"]))

    app = web.Application()
    app.router.add_get("/va_interface2.php", handler)
    server = TestServer(app)
    await server.start_server()
    async with aiohttp.ClientSession() as session:
        yield FsaClient(session, 42982, "key", api_url=str(server.make_url("/va_interface2.php")))
    await server.close()


async def test_flight_log_from_real_list_and_detail(fsa):
    flights = await fsa.flight_logs()
    assert [f.id for f in flights] == [4702575, 4704049]
    flight = flights[-1].merged(await fsa.flight_detail(4704049))
    assert (flight.dep, flight.arr, flight.aircraft, flight.callsign) == ("CYYZ", "EGLL", "MD11", "FVK004")
    assert flight.hours == 7.4 and flight.landing_rate == -272 and flight.packages_kg == 91400
    assert flight.completed_at.year == 2026 and flight.sim == "FS2024"

    embed = flight_log_embed(flight)
    fields = {f.name: f.value for f in embed.fields}
    assert embed.title == "Charter · CYYZ → EGLL"
    assert fields["Pilot"] == "Kari Nordmann (FVK004)" and fields["Block time"] == "7:24"
    assert fields["Rating"] == "100%" and fields["Landing rate"] == "-272 fpm"
    assert fields["Cargo"] == "562 kg" and fields["Packages"] == "91,400 kg" and "Passengers" not in fields
    assert embed.footer.text == "Completed flight · FS2024"


async def test_pilot_from_real_data(fsa):
    pilot = await fsa.pilot("97396")
    assert pilot.callsign == "FVK001" and pilot.rank == "CEO" and pilot.rank_id == 185472
    assert pilot.average_rating == pytest.approx(66970 / 672)  # FSA's "rating" is a running total
    assert pilot.cargo_kg == 2608941 + 2522280
    assert pilot.picture_url == "https://www.fsairlines.net/pics/pilots/97396.jpg"

    embed = pilot_embed(pilot, "<@1>", None, rank_name="Chief Executive Officer")
    fields = {f.name: f.value for f in embed.fields}
    assert embed.title == "Ola Hansen · FVK001"
    assert fields["Rank"] == "Chief Executive Officer" and fields["Avg. rating"] == "99.7%"
    assert fields["Hours"] == "1156:54" and fields["Flights"] == "672"
    assert embed.footer.text == "FSAirlines pilot 97396"  # not FSA's site-wide "Senior Flight Captain" title


async def test_ranks_and_callsign_resolution(fsa):
    ranks = await fsa.ranks()
    assert {r.name for r in ranks} >= {"First Officer", "Trainee Pilot", "Chief Executive Officer"}
    assert await fsa.resolve_pilot_id("fvk003") == "97401"
    assert await fsa.resolve_pilot_id("PieterJ") == "97401"
    assert await fsa.resolve_pilot_id(" 97401 ") == "97401"
    assert await fsa.resolve_pilot_id("FVK999") is None


def test_names_match_accepts_callsign():
    pilot = parse_pilot(fixture("getPilotData")["data"][0])
    assert names_match(pilot, "FVK001") and names_match(pilot, "OlaH") and names_match(pilot, "Ola Hansen")


def test_parse_real_records():
    assert parse_rank(fixture("getRankList")["data"][4]).short == "Trainee Pi"
    assert parse_flight_log(fixture("getFlightReports")["data"][0]).planned_arr == "EGLL"
