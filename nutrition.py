"""Расчёт BMR / TDEE / макронутриентов по Mifflin-St Jeor."""
from __future__ import annotations

from typing import Literal

ACTIVITY_FACTORS = {
    "sedentary":   1.2,    # сидячий образ жизни
    "light":       1.375,  # 1-3 тренировки в неделю
    "moderate":    1.55,   # 3-5 тренировок
    "active":      1.725,  # 6-7 тренировок
    "very_active": 1.9,    # тяжёлый физический труд + тренировки
}

GOAL_LABELS = {
    "gain":     "набор массы",
    "lose":     "снижение веса",
    "maintain": "поддержание формы",
}

ACTIVITY_LABELS = {
    "sedentary":   "сидячий (без тренировок)",
    "light":       "лёгкая (1-3 трен/нед)",
    "moderate":    "средняя (3-5 трен/нед)",
    "active":      "высокая (6-7 трен/нед)",
    "very_active": "очень высокая (тяжёлый труд)",
}

PACE_KCAL_DELTA = {
    # surplus / deficit от TDEE
    "slow":   250,
    "normal": 400,
    "fast":   600,
}

PACE_LABELS = {"slow": "медленно", "normal": "обычно", "fast": "быстро"}


def bmr_mifflin(sex: str, weight_kg: float, height_cm: float, age: int) -> float:
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + (5 if sex == "m" else -161)


def tdee(bmr: float, activity: str) -> float:
    return bmr * ACTIVITY_FACTORS.get(activity, 1.2)


def adjust_for_goal(tdee_value: float, goal: str, pace: str) -> int:
    delta = PACE_KCAL_DELTA.get(pace, 400)
    if goal == "gain":
        return round(tdee_value + delta)
    if goal == "lose":
        return round(tdee_value - delta)
    return round(tdee_value)


def macros_for_kcal(
    kcal: int, weight_kg: float, goal: str
) -> tuple[int, int, int]:
    """Возвращает (protein_g, fats_g, carbs_g)."""
    # Белок: 1.6-2.2 г/кг для атлета. Берём 2.0 при наборе, 2.2 при сушке, 1.8 при поддержании.
    protein_per_kg = {"gain": 2.0, "lose": 2.2, "maintain": 1.8}.get(goal, 2.0)
    protein_g = round(weight_kg * protein_per_kg)

    # Жиры: 25% калорий = 0.25 * kcal / 9
    fats_g = round(kcal * 0.25 / 9)

    protein_kcal = protein_g * 4
    fats_kcal = fats_g * 9
    carbs_kcal = max(0, kcal - protein_kcal - fats_kcal)
    carbs_g = round(carbs_kcal / 4)
    return protein_g, fats_g, carbs_g


def compute_targets(user: dict) -> dict:
    """Принимает строку из БД, возвращает {kcal_target, protein_g, fats_g, carbs_g}."""
    bmr = bmr_mifflin(user["sex"], user["weight_kg"], user["height_cm"], user["age"])
    tdee_v = tdee(bmr, user["activity"])
    kcal = adjust_for_goal(tdee_v, user["goal"], user.get("pace") or "normal")
    protein_g, fats_g, carbs_g = macros_for_kcal(kcal, user["weight_kg"], user["goal"])
    return {
        "kcal_target": kcal,
        "protein_g": protein_g,
        "fats_g": fats_g,
        "carbs_g": carbs_g,
        "bmr": round(bmr),
        "tdee": round(tdee_v),
    }
