"""Регистрация меню команд бота через setMyCommands."""
import json
import os
import urllib.request
from pathlib import Path


def _load_env():
    p = Path(__file__).parent / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"\''))


_load_env()
TOKEN = os.environ["TELEGRAM_TOKEN"]

commands = [
    ("start",       "Начать / пройти анкету"),
    ("profile",     "Твой профиль и норма КБЖУ"),
    ("targets",     "Норма КБЖУ на день"),
    ("menu",        "AI составит меню на день"),
    ("eat",         "Записать приём пищи"),
    ("today",       "Что съел сегодня"),
    ("undo",        "Отменить последний приём"),
    ("weight",      "Записать вес"),
    ("progress",    "График веса"),
    ("remind",      "Настроить напоминание"),
    ("reminders",   "Список напоминаний"),
    ("ask",         "Спросить AI-тренера"),
    ("goal",        "Поменять цель"),
    ("help",        "Все команды"),
]

payload = json.dumps(
    {"commands": [{"command": c, "description": d} for c, d in commands]},
    ensure_ascii=False,
).encode("utf-8")

req = urllib.request.Request(
    f"https://api.telegram.org/bot{TOKEN}/setMyCommands",
    data=payload,
    headers={"Content-Type": "application/json; charset=utf-8"},
)
with urllib.request.urlopen(req) as r:
    print(r.read().decode("utf-8"))
