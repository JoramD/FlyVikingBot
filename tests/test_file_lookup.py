from types import SimpleNamespace
from unittest.mock import AsyncMock

from flyvikingbot.cogs.website import Website
from flyvikingbot.services.http import ServiceError
from flyvikingbot.services.website import FileSearchResult
from flyvikingbot.webhooks import ips_events as ev

from .conftest import load_fixture


def make(result=None, error=None):
    website = SimpleNamespace(search_files=AsyncMock(return_value=result, side_effect=error))
    cog = Website(SimpleNamespace(website=website))
    interaction = SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        delete_original_response=AsyncMock(),
    )
    return cog, interaction


async def test_single_match_uses_the_standard_file_embed():
    data = load_fixture("real_downloadsFile_create")
    data["primaryScreenshot"] = {"url": "https://flyviking.net/uploads/shot.png"}  # the REST API's object form
    cog, interaction = make(FileSearchResult(total=1, file=data))
    await cog.file_lookup.callback(cog, interaction, "test")
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert embed.title == "Test" and embed.url == "https://flyviking.net/files/file/34-test/"
    assert embed.author.name == "JoramD"
    assert embed.image.url == "https://flyviking.net/uploads/shot.png"
    assert embed.footer.text == "GSX Configurations · flyviking.net"
    interaction.delete_original_response.assert_not_awaited()


async def test_errors_are_private():
    for result, error, text in (
        (FileSearchResult(total=7), None, "7 files match"),
        (FileSearchResult(total=0), None, "No files match"),
        (None, ServiceError("down"), "Could not reach the website"),
    ):
        cog, interaction = make(result, error)
        await cog.file_lookup.callback(cog, interaction, "x")
        call = interaction.followup.send.await_args
        assert text in call.args[0] and call.kwargs["ephemeral"] is True
        interaction.delete_original_response.assert_awaited_once()


def test_screenshot_url_or_object():
    data = load_fixture("real_downloadsFile_create")
    data["screenshots"] = [{"url": "https://flyviking.net/uploads/a.png"}]
    assert ev.parse_downloads_file(data).image_url == "https://flyviking.net/uploads/a.png"
    data["primaryScreenshot"] = "https://flyviking.net/uploads/main.png"
    assert ev.parse_downloads_file(data).image_url == "https://flyviking.net/uploads/main.png"

