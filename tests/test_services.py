import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from flyvikingbot.services.http import ServiceError
from flyvikingbot.services.website import WebsiteClient


class FakeApi:
    """Local stand-in for the IPS and FSAirlines APIs. Routes on the raw query string like IPS does."""

    def __init__(self):
        self.responses = {}  # IPS route -> (status, json)
        self.fsa = (200, {"status": "SUCCESS", "data": [{"flights": "10"}]})
        self.requests = []

    async def ips(self, request: web.Request) -> web.Response:
        self.requests.append(request.query_string)
        route = request.query_string.split("&", 1)[0]
        status, body = self.responses.get(route, (404, {"errorCode": "2S100/1", "errorMessage": "NO_ENDPOINT"}))
        return web.json_response(body, status=status)

    async def fsairlines(self, request: web.Request) -> web.Response:
        self.requests.append(request.query_string)
        status, body = self.fsa
        return web.json_response(body, status=status)


@pytest.fixture
async def api():
    fake = FakeApi()
    app = web.Application()
    app.router.add_get("/api/index.php", fake.ips)
    app.router.add_get("/va_interface2.php", fake.fsairlines)
    server = TestServer(app)
    await server.start_server()
    async with aiohttp.ClientSession() as session:
        yield fake, session, str(server.make_url(""))
    await server.close()


def client(api, key="key"):
    _, session, base = api
    return WebsiteClient(session, f"{base}/api/index.php", key)


def test_website_url_keeps_route_unencoded():
    website = WebsiteClient(None, "https://flyviking.net/api/index.php", "k&y")
    url = str(website.build_url("/core/search", q="A320 neo&x"))
    assert url.startswith("https://flyviking.net/api/index.php?/core/search&")
    assert "key=k%26y" in url and "q=A320+neo%26x" in url


async def test_search_single_match_fetches_file(api):
    fake = api[0]
    fake.responses["/core/search"] = (200, {"totalResults": 1, "results": [{"itemId": 55}]})
    fake.responses["/downloads/files/55"] = (200, {"id": 55, "title": "A320"})
    result = await client(api).search_files("a320 neo")
    assert result.total == 1 and result.file["title"] == "A320"
    assert fake.requests[0].startswith("/core/search&") and "q=a320 neo" in fake.requests[0]  # server sees decoded


@pytest.mark.parametrize("total", [0, 4])
async def test_search_zero_or_many(api, total):
    api[0].responses["/core/search"] = (200, {"totalResults": total, "results": []})
    result = await client(api).search_files("x")
    assert result.total == total and result.file is None


async def test_website_api_error(api):
    api[0].responses["/core/search"] = (401, {"errorCode": "3S290/7", "errorMessage": "INVALID_API_KEY"})
    with pytest.raises(ServiceError, match="INVALID_API_KEY"):
        await client(api).search_files("x")


async def test_network_error_does_not_leak_key():
    async with aiohttp.ClientSession() as session:
        website = WebsiteClient(session, "http://127.0.0.1:1/api/index.php", "secret-key")
        with pytest.raises(ServiceError) as info:
            await website.search_files("x")
    assert "secret-key" not in str(info.value)


@pytest.mark.parametrize("body", [{"totalResults": 1, "results": []}, {"totalResults": "x"}, {"totalResults": 1}])
async def test_search_malformed_response_is_service_error(api, body):
    api[0].responses["/core/search"] = (200, body)
    with pytest.raises(ServiceError, match="unexpected data"):
        await client(api).search_files("x")
