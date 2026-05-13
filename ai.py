"""AI-помощник для рациона и распознавания КБЖУ из текста.

Использует Groq (бесплатно, ~300 мс).
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

from groq import AsyncGroq

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")

_client: Optional[AsyncGroq] = None


def client() -> AsyncGroq:
    global _client
    if _client is None:
        if not GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY не задан")
        _client = AsyncGroq(api_key=GROQ_API_KEY)
    return _client


def _strip_json(s: str) -> str:
    s = s.strip()
    # вытащить json из ```json ... ```
    m = re.search(r"\{[\s\S]*\}", s)
    return m.group(0) if m else s


async def parse_food(text: str) -> dict:
    """Парсит произвольное описание еды → КБЖУ.

    Пример: "съел 200г куриной грудки и 100г риса" →
    {description, kcal, protein, fats, carbs}
    """
    prompt = (
        "Ты — нутрициолог. Пользователь описал что съел. Оцени общие КБЖУ.\n"
        "Верни СТРОГО JSON без комментариев, в формате:\n"
        '{"description": "краткое название блюда", "kcal": число, "protein": число, "fats": число, "carbs": число}\n'
        "Все значения числа в граммах (для белков/жиров/углеводов) и ккал.\n"
        "Если данных мало — оцени по средним значениям, но всегда верни JSON.\n\n"
        f"Описание: {text}"
    )
    resp = await client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "Ты возвращаешь только JSON, никогда текст вокруг."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=300,
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(_strip_json(raw))
    except Exception:
        data = {}
    return {
        "description": data.get("description") or text[:80],
        "kcal": float(data.get("kcal") or 0),
        "protein": float(data.get("protein") or 0),
        "fats": float(data.get("fats") or 0),
        "carbs": float(data.get("carbs") or 0),
        "raw": raw,
    }


async def generate_menu(user: dict, kcal: int, p: int, f: int, c: int) -> str:
    """Генерация плана питания на день."""
    goal_ru = {"gain": "набор массы", "lose": "снижение веса", "maintain": "поддержание"}.get(
        user.get("goal", "gain"), "набор массы"
    )
    system = (
        "Ты — фитнес-нутрициолог из России. Составляешь реальные, вкусные меню "
        "из обычных продуктов из Пятёрочки/Магнита. НИКОГДА не выдумывай странные "
        "сочетания типа «куриный рис с гречкой» или «творожная курица». Используй "
        "стандартные блюда: овсянка с творогом и бананом, омлет с овощами, гречка с "
        "куриной грудкой, рис с говядиной, рыба с картофелем, салаты, протеиновые "
        "коктейли, и т.д. Каждое блюдо должно быть привычным и понятным."
    )
    prompt = (
        f"Параметры: вес {user['weight_kg']:.0f} кг, рост {user['height_cm']:.0f} см, "
        f"возраст {user['age']}, тренировок в неделю {user.get('training_days') or 3}, "
        f"цель — {goal_ru}.\n"
        f"Дневная норма: {kcal} ккал | Б {p}г | Ж {f}г | У {c}г.\n\n"
        "Составь меню на ОДИН день — 5 приёмов пищи (завтрак, перекус-1, обед, перекус-2, ужин). "
        "Используй СТРОГО такой формат для каждого приёма (без отступов и markdown заголовков):\n\n"
        "🍳 Завтрак (~XXX ккал):\n"
        "• Продукт 1 — Xг\n"
        "• Продукт 2 — Xг\n"
        "КБЖУ: XXX ккал / БXX / ЖXX / УXX\n\n"
        "Все продукты — обычные русские (курица, индейка, говядина, треска, лосось, рис, "
        "гречка, овсянка, макароны твёрдых сортов, картофель, творог 5%, яйца, кефир, "
        "молоко, бананы, яблоки, орехи, авокадо, овощи, оливковое масло).\n"
        "Суммарно должно сходиться с дневной нормой (±10%). "
        "Никаких комментариев и пояснений в конце — только меню."
    )
    resp = await client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        temperature=0.5,
        max_tokens=1200,
    )
    return resp.choices[0].message.content or "Не получилось сгенерировать меню."


async def coach_advice(user: dict, question: str, totals_today: dict) -> str:
    """Свободный вопрос — фитнес-коуч AI."""
    goal_ru = {"gain": "набор массы", "lose": "снижение веса", "maintain": "поддержание"}.get(
        user.get("goal", "gain"), "поддержание"
    )
    sys = (
        "Ты — персональный фитнес-тренер и нутрициолог. Отвечай конкретно, по-русски, "
        "без воды, используй эмодзи умеренно."
    )
    ctx = (
        f"Пользователь: цель — {goal_ru}, вес {user['weight_kg']} кг, рост {user['height_cm']} см, "
        f"возраст {user['age']}.\n"
        f"Норма на день: {user.get('kcal_target')} ккал ({user.get('protein_g')}/"
        f"{user.get('fats_g')}/{user.get('carbs_g')} БЖУ).\n"
        f"Съедено сегодня: {round(totals_today['kcal'])} ккал ("
        f"{round(totals_today['protein'])}/{round(totals_today['fats'])}/"
        f"{round(totals_today['carbs'])} БЖУ).\n\n"
        f"Вопрос: {question}"
    )
    resp = await client().chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": sys},
            {"role": "user", "content": ctx},
        ],
        temperature=0.6,
        max_tokens=600,
    )
    return resp.choices[0].message.content or "Не получилось ответить."
