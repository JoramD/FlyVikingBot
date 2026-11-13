"""SQLite persistence for account links and small bits of bot state.

Every call runs in a worker thread with its own short-lived connection, which
keeps the event loop free and avoids sharing a connection across threads.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    discord_id      INTEGER PRIMARY KEY,
    ips_member_id   INTEGER UNIQUE,
    ips_profile_url TEXT,
    discord_name    TEXT          -- the username as entered on the website profile
);
CREATE TABLE IF NOT EXISTS accounts (
    discord_id  INTEGER NOT NULL REFERENCES members(discord_id) ON DELETE CASCADE,
    service     TEXT    NOT NULL,  -- e.g. 'fsa'; future: 'sayintentions'
    external_id TEXT    NOT NULL,
    linked_at   INTEGER NOT NULL,
    source      TEXT    NOT NULL,  -- 'auto' or 'staff'
    PRIMARY KEY (discord_id, service),
    UNIQUE (service, external_id)
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Link:
    """A Discord member linked to their website account and one external service account."""

    discord_id: int
    ips_member_id: int | None
    ips_profile_url: str | None
    discord_name: str | None
    service: str
    external_id: str
    source: str


_LINK_SELECT = """
SELECT m.discord_id, m.ips_member_id, m.ips_profile_url, m.discord_name, a.service, a.external_id, a.source
FROM accounts a JOIN members m ON m.discord_id = a.discord_id
"""


class Storage:
    """Async wrapper around data/flyviking.db. Tables: members, accounts (one row per linked service)
    and kv (small bot state: last posted flight, reported problems, pending reviews, callsign cache)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    async def _run(self, fn, *args):
        def task():
            # `with conn` only commits (or rolls back); closing() also closes the connection
            with closing(self._connect()) as conn, conn:
                return fn(conn, *args)

        return await asyncio.to_thread(task)

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def create(conn: sqlite3.Connection) -> None:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)

        await self._run(create)

    # --- links ---------------------------------------------------------------

    async def _one(self, where: str, params: tuple) -> Link | None:
        def q(conn):
            row = conn.execute(f"{_LINK_SELECT} WHERE {where}", params).fetchone()
            return Link(*row) if row else None

        return await self._run(q)

    async def link_by_discord(self, discord_id: int, service: str) -> Link | None:
        return await self._one("a.discord_id = ? AND a.service = ?", (discord_id, service))

    async def link_by_ips_member(self, ips_member_id: int, service: str) -> Link | None:
        return await self._one("m.ips_member_id = ? AND a.service = ?", (ips_member_id, service))

    async def link_by_external(self, service: str, external_id: str) -> Link | None:
        return await self._one("a.service = ? AND a.external_id = ?", (service, external_id))

    async def discord_id_for_ips_member(self, ips_member_id: int) -> int | None:
        """The Discord account linked to a website member (any service), or None."""

        def q(conn):
            row = conn.execute("SELECT discord_id FROM members WHERE ips_member_id = ?", (ips_member_id,)).fetchone()
            return row[0] if row else None

        return await self._run(q)

    async def all_links(self, service: str) -> list[Link]:
        def q(conn):
            rows = conn.execute(f"{_LINK_SELECT} WHERE a.service = ? ORDER BY m.discord_id", (service,)).fetchall()
            return [Link(*r) for r in rows]

        return await self._run(q)

    async def save_link(
        self,
        *,
        discord_id: int,
        ips_member_id: int | None,
        ips_profile_url: str | None,
        discord_name: str | None,
        service: str,
        external_id: str,
        source: str,
    ) -> list[int]:
        """Create or replace a link. Conflicting links (same website member, Discord account or
        external account) are removed first. Returns the Discord IDs whose link was removed."""

        def q(conn):
            displaced: set[int] = set()
            for sql, params in (
                (
                    "SELECT discord_id FROM members WHERE ips_member_id = ? AND discord_id != ?",
                    (ips_member_id, discord_id),
                ),
                (
                    "SELECT discord_id FROM accounts WHERE service = ? AND external_id = ? AND discord_id != ?",
                    (service, external_id, discord_id),
                ),
            ):
                if params[0] is not None:
                    displaced.update(r[0] for r in conn.execute(sql, params))
            for other in displaced:
                conn.execute("DELETE FROM members WHERE discord_id = ?", (other,))
            conn.execute(
                """INSERT INTO members (discord_id, ips_member_id, ips_profile_url, discord_name) VALUES (?, ?, ?, ?)
                   ON CONFLICT(discord_id) DO UPDATE SET ips_member_id = excluded.ips_member_id,
                       ips_profile_url = excluded.ips_profile_url, discord_name = excluded.discord_name""",
                (discord_id, ips_member_id, ips_profile_url, discord_name),
            )
            conn.execute(
                """INSERT INTO accounts (discord_id, service, external_id, linked_at, source) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(discord_id, service) DO UPDATE SET external_id = excluded.external_id,
                       linked_at = excluded.linked_at, source = excluded.source""",
                (discord_id, service, external_id, int(time.time()), source),
            )
            return sorted(displaced)

        return await self._run(q)

    async def remove_member(self, discord_id: int) -> None:
        await self._run(lambda conn: conn.execute("DELETE FROM members WHERE discord_id = ?", (discord_id,)))

    # --- key/value state --------------------------------------------------------

    async def kv_get(self, key: str) -> str | None:
        def q(conn):
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

        return await self._run(q)

    async def kv_set(self, key: str, value: str) -> None:
        await self._run(
            lambda conn: conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        )

    async def kv_delete(self, key: str) -> None:
        await self._run(lambda conn: conn.execute("DELETE FROM kv WHERE key = ?", (key,)))

    async def kv_keys(self, prefix: str) -> list[str]:
        def q(conn):
            return [r[0] for r in conn.execute("SELECT key FROM kv WHERE key LIKE ? ESCAPE '\\'", (_like(prefix),))]

        return await self._run(q)


def _like(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
