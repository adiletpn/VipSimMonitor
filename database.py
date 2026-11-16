"""Асинхронная SQLite (aiosqlite): WAL, дедупликация, уловы, суточная статистика, настройки."""
import asyncio
import logging
import os
import time

import aiosqlite

log = logging.getLogger("db")
DAY = 86400
RETENTION = 30 * DAY

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    phone TEXT PRIMARY KEY,
    operator TEXT NOT NULL,
    score INTEGER NOT NULL,
    price INTEGER,
    ts INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_seen_ts ON seen(ts);
CREATE TABLE IF NOT EXISTS catches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL UNIQUE,
    operator TEXT NOT NULL,
    score INTEGER NOT NULL,
    price INTEGER,
    rules TEXT NOT NULL,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_catches_ts ON catches(ts);
CREATE TABLE IF NOT EXISTS stats (
    day TEXT NOT NULL,
    operator TEXT NOT NULL,
    scanned INTEGER NOT NULL DEFAULT 0,
    fresh INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, operator)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;
"""


class Database:
    __slots__ = ("path", "conn", "_lock")

    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        for pragma in ("journal_mode = WAL", "synchronous = NORMAL", "cache_size = -2000",
                       "temp_store = MEMORY", "busy_timeout = 5000", "mmap_size = 0"):
            await self.conn.execute("PRAGMA " + pragma)
        await self.conn.executescript(SCHEMA)
        async with self.conn.execute("PRAGMA table_info(catches)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        if "last_seen" not in cols:  # миграция: когда номер последний раз был на витрине
            await self.conn.execute("ALTER TABLE catches ADD COLUMN last_seen INTEGER")
            await self.conn.execute("UPDATE catches SET last_seen = ts")
        if "hidden" not in cols:  # миграция: номер скрыт кнопкой «Не интересно»
            await self.conn.execute("ALTER TABLE catches ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
        if "fav" not in cols:  # миграция: кнопка «❤️ Нравится»
            await self.conn.execute("ALTER TABLE catches ADD COLUMN fav INTEGER NOT NULL DEFAULT 0")
        if "soft" not in cols:  # миграция: «мягкий» улов (двойки в Cool) — без отдельного уведомления
            await self.conn.execute("ALTER TABLE catches ADD COLUMN soft INTEGER NOT NULL DEFAULT 0")
        await self.conn.commit()
        log.info("БД открыта: %s", os.path.abspath(self.path))

