"""SQLite-обёртка через aiosqlite — пользователи, приём пищи, вес, напоминания."""
from __future__ import annotations

import os
from datetime import datetime, date
from typing import Any, Optional

import aiosqlite

DB_PATH = os.environ.get("DB_PATH", "fitness.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER PRIMARY KEY,
    name TEXT,
    sex TEXT,                  -- 'm' / 'f'
    age INTEGER,
    height_cm REAL,
    weight_kg REAL,
    activity TEXT,             -- 'sedentary'|'light'|'moderate'|'active'|'very_active'
    training_days INTEGER,
    goal TEXT,                 -- 'gain'|'lose'|'maintain'
    target_weight_kg REAL,
    pace TEXT,                 -- 'slow'|'normal'|'fast'
    kcal_target INTEGER,
    protein_g INTEGER,
    fats_g INTEGER,
    carbs_g INTEGER,
    onboarded INTEGER DEFAULT 0,
    timezone TEXT DEFAULT 'Europe/Moscow',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    ts TEXT NOT NULL,           -- ISO datetime
    day TEXT NOT NULL,          -- YYYY-MM-DD (для агрегаций)
    description TEXT,
    kcal REAL,
    protein REAL,
    fats REAL,
    carbs REAL,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_meals_chat_day ON meals(chat_id, day);

CREATE TABLE IF NOT EXISTS weight_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    weight_kg REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weight_chat ON weight_logs(chat_id, ts);

CREATE TABLE IF NOT EXISTS reminders (
    chat_id INTEGER NOT NULL,
    kind TEXT NOT NULL,        -- 'breakfast'|'lunch'|'dinner'|'water'|'weigh'
    time_hhmm TEXT NOT NULL,   -- '08:30'
    enabled INTEGER DEFAULT 1,
    PRIMARY KEY (chat_id, kind)
);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def get_user(chat_id: int) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def upsert_user(chat_id: int, **fields) -> None:
    fields["updated_at"] = datetime.utcnow().isoformat()
    existing = await get_user(chat_id)
    async with aiosqlite.connect(DB_PATH) as db:
        if not existing:
            fields.setdefault("created_at", datetime.utcnow().isoformat())
            cols = ["chat_id"] + list(fields.keys())
            placeholders = ",".join(["?"] * len(cols))
            values = [chat_id] + list(fields.values())
            await db.execute(
                f"INSERT INTO users ({','.join(cols)}) VALUES ({placeholders})", values
            )
        else:
            sets = ",".join(f"{k}=?" for k in fields.keys())
            values = list(fields.values()) + [chat_id]
            await db.execute(f"UPDATE users SET {sets} WHERE chat_id=?", values)
        await db.commit()


async def add_meal(
    chat_id: int,
    description: str,
    kcal: float,
    protein: float,
    fats: float,
    carbs: float,
    raw: str = "",
) -> int:
    now = datetime.now()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO meals (chat_id, ts, day, description, kcal, protein, fats, carbs, raw) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                chat_id,
                now.isoformat(),
                now.date().isoformat(),
                description,
                kcal,
                protein,
                fats,
                carbs,
                raw,
            ),
        )
        await db.commit()
        return cur.lastrowid


async def meals_today(chat_id: int) -> list[dict[str, Any]]:
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM meals WHERE chat_id=? AND day=? ORDER BY ts", (chat_id, today)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def totals_today(chat_id: int) -> dict[str, float]:
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(kcal),0), COALESCE(SUM(protein),0), COALESCE(SUM(fats),0), COALESCE(SUM(carbs),0) "
            "FROM meals WHERE chat_id=? AND day=?",
            (chat_id, today),
        ) as cur:
            row = await cur.fetchone()
    return {"kcal": row[0], "protein": row[1], "fats": row[2], "carbs": row[3]}


async def delete_last_meal(chat_id: int) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM meals WHERE chat_id=? ORDER BY id DESC LIMIT 1", (chat_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        await db.execute("DELETE FROM meals WHERE id=?", (row["id"],))
        await db.commit()
        return dict(row)


async def add_weight(chat_id: int, weight_kg: float) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO weight_logs (chat_id, ts, weight_kg) VALUES (?,?,?)",
            (chat_id, datetime.now().isoformat(), weight_kg),
        )
        await db.execute("UPDATE users SET weight_kg=? WHERE chat_id=?", (weight_kg, chat_id))
        await db.commit()


async def weight_history(chat_id: int, limit: int = 90) -> list[tuple[str, float]]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT ts, weight_kg FROM weight_logs WHERE chat_id=? ORDER BY ts DESC LIMIT ?",
            (chat_id, limit),
        ) as cur:
            rows = await cur.fetchall()
    return list(reversed(rows))


async def set_reminder(chat_id: int, kind: str, time_hhmm: str, enabled: bool = True) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO reminders (chat_id, kind, time_hhmm, enabled) VALUES (?,?,?,?) "
            "ON CONFLICT(chat_id, kind) DO UPDATE SET time_hhmm=excluded.time_hhmm, enabled=excluded.enabled",
            (chat_id, kind, time_hhmm, 1 if enabled else 0),
        )
        await db.commit()


async def get_reminders(chat_id: int) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM reminders WHERE chat_id=? ORDER BY time_hhmm", (chat_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def delete_reminder(chat_id: int, kind: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM reminders WHERE chat_id=? AND kind=?", (chat_id, kind))
        await db.commit()


async def all_active_reminders() -> list[dict[str, Any]]:
    """Для воссоздания scheduler-задач при рестарте."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM reminders WHERE enabled=1") as cur:
            return [dict(r) for r in await cur.fetchall()]
