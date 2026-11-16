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

    async def toggle_fav(self, phone: str) -> bool:
        await self.conn.execute("UPDATE catches SET fav = 1 - fav, hidden = 0 WHERE phone = ?", (phone,))
        await self.conn.commit()
        async with self.conn.execute("SELECT fav FROM catches WHERE phone = ?", (phone,)) as cur:
            row = await cur.fetchone()
        return bool(row and row[0])

    async def favorites(self, limit: int = 40) -> list:
        async with self.conn.execute(
                "SELECT phone, operator, score, price, last_seen, fav, soft FROM catches WHERE fav = 1 "
                "ORDER BY last_seen DESC LIMIT ?", (limit,)) as cur:
            return await cur.fetchall()

    async def touch_catches(self, phones: list[str]) -> None:
        """Номер снова был в выдаче витрины — значит, пока доступен."""
        if not phones:
            return
        await self.conn.execute(
            f"UPDATE catches SET last_seen = ? WHERE phone IN ({','.join('?' * len(phones))})",
            [int(time.time()), *phones])
        await self.conn.commit()

    _AVAIL = ("FROM catches WHERE price BETWEEN ? AND ? AND last_seen >= ? AND hidden = 0 "
              "AND (score >= ? OR soft = 1 OR fav = 1)")

    async def available(self, price_from: int, price_to: int, min_score: int, hours: int = 12, limit: int = 30,
                        offset: int = 0) -> list:
        """Пойманные номера в ценовом диапазоне, которые были на витрине за последние N часов и проходят
        текущий порог (или «мягкие»/избранные). Строки: (phone, operator, score, price, last_seen, fav, soft)."""
        async with self.conn.execute(
                "SELECT phone, operator, score, price, last_seen, fav, soft " + self._AVAIL +
                " ORDER BY soft ASC, score DESC, last_seen DESC LIMIT ? OFFSET ?",
                (price_from, price_to, int(time.time()) - hours * 3600, min_score, limit, offset)) as cur:
            return await cur.fetchall()

    async def count_available(self, price_from: int, price_to: int, min_score: int, hours: int = 12) -> int:
        row = await self._one("SELECT COUNT(*) " + self._AVAIL,
                              (price_from, price_to, int(time.time()) - hours * 3600, min_score))
        return row[0]

    async def best_available(self, min_score: int, hours: int = 12, limit: int = 7) -> list:
        """Лучшие номера, которые сейчас есть на витрине: (phone, operator, score, price, last_seen)."""
        async with self.conn.execute(
                "SELECT phone, operator, score, price, last_seen FROM catches WHERE hidden = 0 AND last_seen >= ? "
                "AND score >= ? ORDER BY score DESC, last_seen DESC LIMIT ?",
                (int(time.time()) - hours * 3600, min_score, limit)) as cur:
            return await cur.fetchall()

    async def rescore(self, fn) -> int:
        """Пересчитать баллы всех уловов текущим скорером. fn(phone, soft) -> (score, rules)."""
        async with self.conn.execute("SELECT phone, soft FROM catches") as cur:
            rows = await cur.fetchall()
        upd = []
        for ph, soft in rows:
            sc, rules = fn(ph, soft)
            upd.append((sc, "\n".join(rules), ph))
        await self.conn.executemany("UPDATE catches SET score = ?, rules = ? WHERE phone = ?", upd)
        await self.conn.commit()
        return len(upd)

    async def hide(self, phone: str) -> None:
        await self.conn.execute("UPDATE catches SET hidden = 1, fav = 0 WHERE phone = ?", (phone,))
        await self.conn.commit()

    async def hide_range(self, price_from: int, price_to: int) -> int:
        cur = await self.conn.execute("UPDATE catches SET hidden = 1 "
                                      "WHERE hidden = 0 AND fav = 0 AND price BETWEEN ? AND ?",
                                      (price_from, price_to))
        n = cur.rowcount
        await cur.close()
        await self.conn.commit()
        return n

    # ---------- обслуживание ----------
    async def cleanup(self) -> int:
        cur = await self.conn.execute("DELETE FROM seen WHERE ts < ?", (int(time.time()) - RETENTION,))
        deleted = cur.rowcount
        await cur.close()
        await self.conn.commit()
        await self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        await self.conn.execute("PRAGMA optimize")
        return deleted

    async def checkpoint(self) -> None:
        await self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    async def backup_to(self, target: str) -> str:
        """Консистентная копия БД через SQLite Online Backup API."""
        if os.path.exists(target):
            os.remove(target)
        async with aiosqlite.connect(target) as dst:
            await self.conn.backup(dst)
        return target

    # ---------- статистика ----------
    async def _one(self, sql: str, args=()) -> tuple:
        async with self.conn.execute(sql, args) as cur:
            return await cur.fetchone()

    async def stats(self) -> dict:
        day_ago = int(time.time()) - DAY
        s_day = await self._one("SELECT COALESCE(SUM(scanned),0), COALESCE(SUM(fresh),0) FROM stats "
                                "WHERE day = date('now', '+5 hours')")
        s_all = await self._one("SELECT COALESCE(SUM(scanned),0), COALESCE(SUM(fresh),0) FROM stats")
        c_day = await self._one("SELECT COUNT(*) FROM catches WHERE ts >= ?", (day_ago,))
        c_all = await self._one("SELECT COUNT(*) FROM catches")
        jack = await self._one("SELECT COUNT(*) FROM catches WHERE score >= 90")
        async with self.conn.execute(
                "SELECT phone, operator, score, price, ts FROM catches ORDER BY score DESC, ts DESC LIMIT 3") as cur:
            top = await cur.fetchall()
        async with self.conn.execute(
                "SELECT operator, SUM(scanned), SUM(fresh) FROM stats WHERE day = date('now', '+5 hours') "
                "GROUP BY operator ORDER BY operator") as cur:
            per_op = await cur.fetchall()
        return {"scanned_day": s_day[0], "fresh_day": s_day[1], "scanned_all": s_all[0], "fresh_all": s_all[1],
                "catches_day": c_day[0], "catches_all": c_all[0], "jackpots": jack[0], "top": top,
                "per_op": per_op}
