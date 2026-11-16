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


