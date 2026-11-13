import pytest

from flyvikingbot.storage import Storage


@pytest.fixture
async def storage(tmp_path):
    s = Storage(tmp_path / "test.db")
    await s.init()
    return s


async def link(storage, discord_id, ips_id, pilot, source="auto"):
    return await storage.save_link(
        discord_id=discord_id, ips_member_id=ips_id, ips_profile_url=f"https://x/profile/{ips_id}/",
        discord_name=f"user{discord_id}", service="fsa", external_id=pilot, source=source,
    )


async def test_save_and_lookup(storage):
    assert await link(storage, 1, 10, "100") == []
    by_discord = await storage.link_by_discord(1, "fsa")
    assert by_discord.external_id == "100" and by_discord.ips_member_id == 10
    assert (await storage.link_by_ips_member(10, "fsa")).discord_id == 1
    assert (await storage.link_by_external("fsa", "100")).discord_id == 1
    assert await storage.link_by_discord(1, "sayintentions") is None


async def test_relink_same_discord_updates(storage):
    await link(storage, 1, 10, "100")
    await link(storage, 1, 10, "200", source="staff")
    assert (await storage.link_by_discord(1, "fsa")).external_id == "200"
    assert await storage.link_by_external("fsa", "100") is None


async def test_conflicts_are_displaced(storage):
    await link(storage, 1, 10, "100")
    await link(storage, 2, 20, "200")
    # Discord 3 takes over pilot 100 and website member 20
    displaced = await link(storage, 3, 20, "100", source="staff")
    assert displaced == [1, 2]
    assert [x.discord_id for x in await storage.all_links("fsa")] == [3]


async def test_remove_member_cascades(storage):
    await link(storage, 1, 10, "100")
    await storage.remove_member(1)
    assert await storage.all_links("fsa") == []


async def test_kv(storage):
    assert await storage.kv_get("a") is None
    await storage.kv_set("a", "1")
    await storage.kv_set("a", "2")
    await storage.kv_set("review:5", "x")
    assert await storage.kv_get("a") == "2"
    assert await storage.kv_keys("review:") == ["review:5"]
    await storage.kv_delete("a")
    assert await storage.kv_get("a") is None
