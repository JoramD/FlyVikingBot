from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from flyvikingbot.cogs import weather as weather_cog
from flyvikingbot.cogs.weather import (
    FIELD_LIMIT,
    InvalidAirports,
    Weather,
    code_block,
    parse_airports,
    weather_embed,
)
from flyvikingbot.services.http import ServiceError
from flyvikingbot.services.sayintentions import AirportWeather, Frequency, parse_weather

from .test_sayintentions import GETWX

WX = parse_weather(GETWX)


@pytest.mark.parametrize(
    ("text", "codes"),
    [("ekch", ["EKCH"]), ("EKCH ENGM", ["EKCH", "ENGM"]), (" ekch, engm;kjfk ", ["EKCH", "ENGM", "KJFK"]),
     ("EKCH ekch", ["EKCH"])],
)
def test_parse_airports(text, codes):
    assert parse_airports(text) == codes


@pytest.mark.parametrize(
    ("text", "error"),
    [("", "at least one"), ("XYZ12", '"XYZ12" is not a valid ICAO'), ("EK-H", "not a valid"),
     ("EKCH ENGM KJFK EGLL", "at most 3")],
)
def test_parse_airports_rejects(text, error):
    with pytest.raises(InvalidAirports, match=error):
        parse_airports(text)


def test_weather_embed_fields():
    embed = weather_embed(WX["EKCH"])
    fields = {f.name: f.value for f in embed.fields}
    assert embed.title == "EKCH" and embed.footer.text == "SayIntentions" and embed.timestamp
    assert list(fields) == ["METAR", "TAF", "Active runway", "Wind", "ATIS"]  # no frequencies unless asked
    assert fields["METAR"] == "```\nEKCH 131150Z 24012KT 9999 FEW030 09/04 Q1013 NOSIG\n```"
    assert fields["Active runway"] == "22L" and fields["Wind"] == "240° at 12 kt"


def test_weather_embed_frequencies_and_missing_fields():
    embed = weather_embed(WX["ENGM"], frequencies=True)
    fields = {f.name: f.value for f in embed.fields}
    assert "TAF" not in fields
    assert fields["Frequencies"] == "Tower 118.300 (Gardermoen Tower)"
    many = AirportWeather("EKCH", metar="x", frequencies=tuple(Frequency(None, f"1{i}", None) for i in range(15)))
    lines = {f.name: f.value for f in weather_embed(many, True).fields}["Frequencies"].splitlines()
    assert len(lines) == 10 and lines[0] == "10"


def test_code_block_fits_a_field():
    block = code_block("word " * 400)
    assert len(block) <= FIELD_LIMIT and block.endswith("…\n```")
    assert code_block("a```b") == "```\na'''b\n```"


def make(result=None, error=None):
    client = SimpleNamespace(weather=AsyncMock(return_value=result, side_effect=error))
    cog = Weather(SimpleNamespace(sayintentions=client))
    message = SimpleNamespace(delete=AsyncMock())
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), is_done=MagicMock(return_value=True)),
        followup=SimpleNamespace(send=AsyncMock(return_value=message)),
        delete_original_response=AsyncMock(),
    )
    return cog, interaction, client, message


async def test_weather_posts_one_embed_per_airport():
    cog, interaction, client, _ = make(WX)
    await cog.weather.callback(cog, interaction, "ENGM ekch")
    client.weather.assert_awaited_once_with(["ENGM", "EKCH"])
    call = interaction.followup.send.await_args
    assert call.args == (None,) and [e.title for e in call.kwargs["embeds"]] == ["ENGM", "EKCH"]
    interaction.delete_original_response.assert_not_awaited()


async def test_partial_result_notes_the_missing_airport():
    cog, interaction, _, _ = make({"EKCH": WX["EKCH"]})
    await cog.weather.callback(cog, interaction, "EKCH EKXX")
    call = interaction.followup.send.await_args
    assert call.args == ("-# No weather found for EKXX.",) and len(call.kwargs["embeds"]) == 1


async def test_invalid_airport_error_stays():
    cog, interaction, client, _ = make(WX)
    interaction.response.is_done = MagicMock(return_value=False)
    await cog.weather.callback(cog, interaction, "XYZ12")
    client.weather.assert_not_awaited()
    call = interaction.response.send_message.await_args
    assert call.args[0] == ':x: ERROR: "XYZ12" is not a valid ICAO code.' and call.kwargs["delete_after"] is None


@pytest.mark.parametrize(
    ("result", "error", "text"),
    [({}, None, ":x: ERROR: No weather found for EKXX."),
     (None, ServiceError("down"), "Could not get weather from SayIntentions")],
)
async def test_errors_replace_the_thinking_message_and_stay(result, error, text):
    cog, interaction, _, message = make(result, error)
    await cog.weather.callback(cog, interaction, "EKXX")
    interaction.delete_original_response.assert_awaited_once()
    call = interaction.followup.send.await_args
    assert text in call.args[0] and call.kwargs["ephemeral"] is True
    message.delete.assert_not_awaited()  # errors don't disappear


async def test_cog_not_loaded_without_keys():
    bot = SimpleNamespace(sayintentions=None, add_cog=AsyncMock())
    await weather_cog.setup(bot)
    bot.add_cog.assert_not_awaited()
