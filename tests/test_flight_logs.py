from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from flyvikingbot.cogs import flight_logs as fl
from flyvikingbot.services.fsairlines import FlightLog
from flyvikingbot.services.http import ServiceError
from flyvikingbot.storage import Storage


def flight(fid, **kw):
    base = dict(number=f"FV{fid}", dep="BIKF", arr="EGLL", planned_arr="EGLL", aircraft="B738", pilot_id="7",
                pilot_name="Jan Jansen", hours=2.5, distance=1032.4, rating=9.5, pax=150, cargo_kg=0,
                landing_rate=None, completed_at=None)
    return FlightLog(id=fid, **{**base, **kw})


class FakeFsa:
    def __init__(self, flights):
        self.flights = flights
        self.detail_calls = []

    async def flight_logs(self, count=15, pilot_id=None):
        return sorted(self.flights, key=lambda f: f.id)[-count:]

    async def flight_detail(self, fid):
        self.detail_calls.append(fid)
        return flight(fid, landing_rate=-120)


@pytest.fixture
async def feed(tmp_path):
    storage = Storage(tmp_path / "db.sqlite")
    await storage.init()
    channel = AsyncMock()
    fsa = FakeFsa([flight(1), flight(2)])
    bot = SimpleNamespace(storage=storage, fsa=fsa, get_channel=lambda cid: channel, fetch_channel=AsyncMock())
    cog = fl.FlightLogs.__new__(fl.FlightLogs)  # skip __init__ (it starts the task loop)
    cog.bot, cog.channel_id = bot, 123
    return cog, fsa, channel, storage


async def test_first_run_seeds_without_posting(feed):
    cog, _, channel, storage = feed
    assert await cog.check_new_flights() == 0
    channel.send.assert_not_awaited()
    assert await storage.kv_get(fl.LAST_ID_KEY) == "2"


async def test_posts_only_new_flights_in_order(feed):
    cog, fsa, channel, storage = feed
    await cog.check_new_flights()
    fsa.flights += [flight(4), flight(3)]
    assert await cog.check_new_flights() == 2
    titles = [c.kwargs["embed"].title for c in channel.send.await_args_list]
    assert titles == ["FV3 · BIKF → EGLL", "FV4 · BIKF → EGLL"]
    assert fsa.detail_calls == [3, 4]
    assert await storage.kv_get(fl.LAST_ID_KEY) == "4"
    assert await cog.check_new_flights() == 0  # nothing new


async def test_failed_send_does_not_advance(feed):
    cog, fsa, channel, storage = feed
    await cog.check_new_flights()
    fsa.flights.append(flight(3))
    channel.send.side_effect = discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")
    with pytest.raises(discord.HTTPException):
        await cog.check_new_flights()
    assert await storage.kv_get(fl.LAST_ID_KEY) == "2"


async def test_cap_per_cycle(feed):
    cog, fsa, channel, _ = feed
    await cog.check_new_flights()
    fsa.flights += [flight(i) for i in range(3, 20)]
    assert await cog.check_new_flights() == fl.MAX_POSTS_PER_CYCLE


async def test_detail_failure_still_posts_and_linked_pilot_is_mentioned(feed):
    cog, fsa, channel, storage = feed
    await cog.check_new_flights()
    await storage.save_link(discord_id=55, ips_member_id=1, ips_profile_url=None, discord_name="jan",
                            service="fsa", external_id="7", source="auto")
    fsa.flight_detail = AsyncMock(side_effect=ServiceError("down"))
    fsa.flights.append(flight(3))
    await cog.check_new_flights()
    embed = channel.send.await_args.kwargs["embed"]
    assert {f.name: f.value for f in embed.fields}["Pilot"] == "<@55>"


def test_embed_contents():
    embed = fl.flight_log_embed(flight(9, landing_rate=-142, arr="BIRK", planned_arr="BIKF", cargo_kg=500))
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Pilot"] == "Jan Jansen" and fields["Block time"] == "2:30" and fields["Distance"] == "1,032 nm"
    assert fields["Landing rate"] == "-142 fpm" and fields["Cargo"] == "500 kg"
    assert "Diverted" in embed.description
