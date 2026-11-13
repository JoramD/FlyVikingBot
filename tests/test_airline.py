from flyvikingbot.cogs.airline import stats_embed


def test_stats_embed_formatting():
    embed = stats_embed({"flights": "672", "hours": "1156.9", "distance": 412345.6, "fuel_used": None,
                         "pax": "98765", "cargo_kg": "2608941", "packages_kg": "abc"})
    fields = {f.name: f.value for f in embed.fields}
    assert fields == {
        "Flights": "672",
        "Hours flown": "1,157",  # rounded, not cut off
        "Distance flown": "412,346 nm",
        "Fuel used": "n/a",
        "Passengers moved": "98,765",
        "Generic cargo moved": "2,608,941 kg",
        "Packages moved": "n/a",
    }
    assert embed.description is None
    assert embed.footer.text == "FSAirlines" and embed.timestamp is not None
