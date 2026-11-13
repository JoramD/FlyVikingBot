"""The preview tool covers every automatic post (the RULE in flyvikingbot/tools/preview_posts.py)."""

import importlib
import inspect
import pkgutil
import zlib

import pytest

from flyvikingbot import cogs
from flyvikingbot.tools import preview_posts as pp

CTX = pp.Context(site="https://flyviking.net", avatar_url=None, mention="<@1>")


def message_builders():
    """Every module-level *_message function in the cogs."""
    found = set()
    for info in pkgutil.iter_modules(cogs.__path__):
        module = importlib.import_module(f"{cogs.__name__}.{info.name}")
        for name, obj in inspect.getmembers(module, inspect.isfunction):
            if name.endswith("_message") and obj.__module__ == module.__name__:
                found.add(obj)
    return found


def test_every_message_builder_has_a_preview():
    builders = message_builders()
    assert builders, "no *_message builders found; did the naming convention change?"
    missing = {f.__qualname__ for f in builders - {builder for builder, _ in pp.PREVIEWS.values()}}
    assert not missing, f"add these to PREVIEWS in tools/preview_posts.py: {sorted(missing)}"


@pytest.mark.parametrize("name", list(pp.PREVIEWS))
def test_preview_renders_without_pings(name):
    kwargs = pp.render(name, CTX)
    assert kwargs["allowed_mentions"].everyone is False
    assert kwargs.get("content") or kwargs.get("embed") or kwargs.get("file")
    if (embed := kwargs.get("embed")) and embed.image.url:
        assert kwargs["file"].filename == pp.SAMPLE_IMAGE  # the embed's attachment:// image is attached


def test_announcement_preview_shows_everyone_text_but_cannot_ping():
    kwargs = pp.render("announcement", CTX)
    assert kwargs["content"] == "@everyone"
    assert kwargs["allowed_mentions"].everyone is False


def test_sample_png_is_valid():
    data = pp.sample_png(8, 4)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    idat = data.index(b"IDAT")
    length = int.from_bytes(data[idat - 4:idat], "big")
    assert len(zlib.decompress(data[idat + 4:idat + 4 + length])) == 4 * (1 + 8 * 3)


def test_cli_rejects_unknown_names(capsys):
    with pytest.raises(SystemExit):
        pp.run(["nope"])
    assert pp.run(["--list"]) == 0
    assert "announcement" in capsys.readouterr().out


def test_cli_needs_the_channel(monkeypatch, tmp_path, capsys):
    from .conftest import BASE_ENV
    for key, value in {**BASE_ENV, "DATA_DIR": str(tmp_path), "CHANNEL_BOT_DEVELOPMENT": ""}.items():
        monkeypatch.setenv(key, value)
    assert pp.run(["gallery"]) == 1
    assert "CHANNEL_BOT_DEVELOPMENT" in capsys.readouterr().out
