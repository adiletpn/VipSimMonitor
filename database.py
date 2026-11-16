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

    async def close(self) -> None:
        if self.conn is None:
            return
        try:
            await self.conn.commit()
            await self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            await self.conn.close()
            self.conn = None
            log.info("БД закрыта, WAL сброшен")

    # ---------- settings ----------
    async def get_settings(self) -> dict:
        async with self.conn.execute("SELECT key, value FROM settings") as cur:
            return {k: v for k, v in await cur.fetchall()}

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        await self.conn.commit()

    # ---------- номера ----------
    async def register(self, operator: str, items: list[tuple[str, int | None, int]]) -> set[str]:
        """items: [(phone, price, score)]. Пишет в историю, возвращает множество впервые увиденных номеров."""
        if not items:
            return set()
        now = int(time.time())
        async with self._lock:
            phones = [it[0] for it in items]
            known = set()
            for i in range(0, len(phones), 500):
                chunk = phones[i:i + 500]
                sql = f"SELECT phone FROM seen WHERE phone IN ({','.join('?' * len(chunk))})"
                async with self.conn.execute(sql, chunk) as cur:
                    known.update(r[0] for r in await cur.fetchall())
            fresh = [(ph, operator, sc, pr, now) for ph, pr, sc in items if ph not in known]
            if fresh:
                await self.conn.executemany(
                    "INSERT OR IGNORE INTO seen(phone, operator, score, price, ts) VALUES(?,?,?,?,?)", fresh)
            await self.conn.execute(
                "INSERT INTO stats(day, operator, scanned, fresh) VALUES(date('now', '+5 hours'), ?, ?, ?) "
                "ON CONFLICT(day, operator) DO UPDATE SET scanned = scanned + excluded.scanned, "
                "fresh = fresh + excluded.fresh", (operator, len(items), len(fresh)))
            await self.conn.commit()
        return {f[0] for f in fresh}

    async def add_catch(self, phone: str, operator: str, score: int, price: int | None, rules: list[str]) -> bool:
        """True — номер записан впервые (его ещё не присылали)."""
        now = int(time.time())
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO catches(phone, operator, score, price, rules, ts, last_seen) "
            "VALUES(?,?,?,?,?,?,?)", (phone, operator, score, price, "\n".join(rules), now, now))
        added = cur.rowcount == 1
        await cur.close()
        await self.conn.commit()
        return added

    async def add_soft(self, operator: str, items: list[tuple[str, int, int | None, list[str]]]) -> int:
        """Пачка «мягких» находок [(phone, score, price, rules)] одной транзакцией. Возвращает число новых."""
        if not items:
            return 0
        now = int(time.time())
        before = self.conn.total_changes
        await self.conn.executemany(
            "INSERT OR IGNORE INTO catches(phone, operator, score, price, rules, ts, last_seen, soft) "
            "VALUES(?,?,?,?,?,?,?,1)", [(ph, operator, sc, pr, "\n".join(r), now, now) for ph, sc, pr, r in items])
        await self.conn.commit()
        return self.conn.total_changes - before

    async def soft_since(self, ts: int) -> list:
        """[(price, count)] новых «мягких» находок с момента ts."""
        async with self.conn.execute(
                "SELECT price, COUNT(*) FROM catches WHERE soft = 1 AND hidden = 0 AND ts >= ? GROUP BY price",
                (ts,)) as cur:
            return await cur.fetchall()

