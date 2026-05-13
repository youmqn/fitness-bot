"""Telegram-бот: помощник по набору массы / снижению веса.

Команды:
/start, /профиль, /цель, /рацион, /меню, /съел, /сегодня, /отменить,
/вес, /прогресс, /напомнить, /напоминания, /удалить_напоминание, /спросить, /помощь

Деплой: Railway (webhook) или локально (polling).
ENV:
  TELEGRAM_TOKEN     — токен от @BotFather
  GROQ_API_KEY       — ключ Groq
  MODE               — 'webhook' (Railway) или 'polling' (локально, default)
  WEBHOOK_URL        — публичный URL Railway, например https://xxx.up.railway.app
  PORT               — порт (Railway задаёт автоматически, default 8080)
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, time as dtime
from pathlib import Path


def _load_env_file() -> None:
    """Простой loader .env (без зависимости от python-dotenv)."""
    path = Path(__file__).parent / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip("'\"")
        if k and k not in os.environ:
            os.environ[k] = v


_load_env_file()

import pytz
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db
from ai import coach_advice, generate_menu, parse_food
from charts import weight_chart
from nutrition import (
    ACTIVITY_FACTORS,
    ACTIVITY_LABELS,
    GOAL_LABELS,
    PACE_LABELS,
    compute_targets,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO
)
logger = logging.getLogger("fitness-bot")

# ──────────────────────── states ────────────────────────
GOAL, SEX, AGE, HEIGHT, WEIGHT, ACTIVITY, TRAINING, TARGET, PACE = range(9)
# Лог веса
WEIGHT_INPUT = 100
# Установка напоминания
REM_KIND, REM_TIME = 200, 201

MSK = pytz.timezone("Europe/Moscow")

# ──────────────────────── helpers ────────────────────────


def fmt_user(u: dict) -> str:
    sex = "♂️ м" if u["sex"] == "m" else "♀️ ж"
    return (
        f"<b>Профиль</b>\n"
        f"{sex} • {u['age']} лет • {u['height_cm']:.0f} см • {u['weight_kg']:.1f} кг\n"
        f"Цель: <b>{GOAL_LABELS.get(u['goal'], u['goal'])}</b>"
        + (f" → {u['target_weight_kg']:.1f} кг" if u.get("target_weight_kg") else "")
        + f"\nАктивность: {ACTIVITY_LABELS.get(u['activity'], u['activity'])}"
        + f"\nТренировок в неделю: {u.get('training_days') or 0}"
        + f"\nТемп: {PACE_LABELS.get(u.get('pace') or 'normal', '—')}"
    )


def fmt_targets(u: dict) -> str:
    return (
        f"🎯 <b>Норма на день</b>\n"
        f"Калории: <b>{u['kcal_target']}</b> ккал\n"
        f"Белки:   <b>{u['protein_g']}</b> г\n"
        f"Жиры:    <b>{u['fats_g']}</b> г\n"
        f"Углеводы: <b>{u['carbs_g']}</b> г"
    )


def progress_bar(current: float, target: float, width: int = 12) -> str:
    if target <= 0:
        return ""
    pct = max(0.0, min(1.5, current / target))
    filled = int(min(1.0, pct) * width)
    return "▰" * filled + "▱" * (width - filled)


# ──────────────────────── /start (onboarding) ────────────────────────


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    user = await db.get_user(chat_id)
    if user and user.get("onboarded"):
        await update.message.reply_text(
            "С возвращением! 💪\n\n"
            "Основные команды:\n"
            "/targets — твоя норма КБЖУ\n"
            "/menu — AI составит план питания\n"
            "/eat <что съел> — записать приём пищи\n"
            "/today — что съел сегодня\n"
            "/weight [кг] — записать вес\n"
            "/progress — график веса\n"
            "/remind — настроить напоминание\n"
            "/ask <вопрос> — спросить AI-тренера\n"
            "/profile — твои данные\n"
            "/help — все команды\n\n"
            "💡 Можно просто писать без команд: «съел 200г курицы» → запишу. "
            "Или вопрос → AI-тренер ответит."
        )
        return ConversationHandler.END

    name = update.effective_user.first_name or ""
    await update.message.reply_text(
        f"Привет, {name}! 👋\n\n"
        "Я помогу тебе достичь цели — набрать массу или сбросить вес.\n"
        "Сначала анкета (1 минута).\n\n"
        "Какая у тебя цель?",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💪 Набрать массу", callback_data="goal_gain")],
                [InlineKeyboardButton("🔥 Сбросить вес", callback_data="goal_lose")],
                [InlineKeyboardButton("⚖️ Поддерживать форму", callback_data="goal_maintain")],
            ]
        ),
    )
    return GOAL


async def on_goal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    goal = q.data.replace("goal_", "")
    ctx.user_data["goal"] = goal
    await q.edit_message_text(f"Цель: <b>{GOAL_LABELS[goal]}</b>", parse_mode=ParseMode.HTML)
    await q.message.reply_text(
        "Пол?",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("♂️ Мужской", callback_data="sex_m"),
                    InlineKeyboardButton("♀️ Женский", callback_data="sex_f"),
                ]
            ]
        ),
    )
    return SEX


async def on_sex(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    ctx.user_data["sex"] = q.data.replace("sex_", "")
    await q.edit_message_text("Сколько тебе лет? (число)")
    return AGE


async def on_age(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        age = int(re.sub(r"\D", "", update.message.text or "0"))
        if not 10 <= age <= 100:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи возраст числом (10-100):")
        return AGE
    ctx.user_data["age"] = age
    await update.message.reply_text("Рост в сантиметрах? (например 178)")
    return HEIGHT


async def on_height(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        h = float((update.message.text or "0").replace(",", "."))
        if not 100 <= h <= 230:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи рост в см (100-230):")
        return HEIGHT
    ctx.user_data["height_cm"] = h
    await update.message.reply_text("Текущий вес в кг? (например 72.5)")
    return WEIGHT


async def on_weight(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        w = float((update.message.text or "0").replace(",", "."))
        if not 30 <= w <= 250:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи вес в кг (30-250):")
        return WEIGHT
    ctx.user_data["weight_kg"] = w
    await update.message.reply_text(
        "Уровень активности?",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🪑 Сидячий (без тренировок)", callback_data="act_sedentary")],
                [InlineKeyboardButton("🚶 Лёгкая (1-3 трен/нед)", callback_data="act_light")],
                [InlineKeyboardButton("🏃 Средняя (3-5 трен/нед)", callback_data="act_moderate")],
                [InlineKeyboardButton("🏋️ Высокая (6-7 трен/нед)", callback_data="act_active")],
                [InlineKeyboardButton("🔥 Очень высокая", callback_data="act_very_active")],
            ]
        ),
    )
    return ACTIVITY


async def on_activity(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    activity = q.data.replace("act_", "")
    ctx.user_data["activity"] = activity
    await q.edit_message_text(f"Активность: {ACTIVITY_LABELS[activity]}")
    await q.message.reply_text("Сколько раз в неделю тренируешься? (0-7)")
    return TRAINING


async def on_training(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        td = int(re.sub(r"\D", "", update.message.text or "0"))
        if not 0 <= td <= 14:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Число от 0 до 7:")
        return TRAINING
    ctx.user_data["training_days"] = td

    goal = ctx.user_data["goal"]
    if goal == "maintain":
        ctx.user_data["target_weight_kg"] = ctx.user_data["weight_kg"]
        ctx.user_data["pace"] = "normal"
        return await _finish_onboarding(update, ctx)

    await update.message.reply_text(
        f"Какой вес хочешь достичь? (текущий — {ctx.user_data['weight_kg']:.1f} кг)"
    )
    return TARGET


async def on_target(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        t = float((update.message.text or "0").replace(",", "."))
        if not 30 <= t <= 250:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи целевой вес в кг (30-250):")
        return TARGET
    ctx.user_data["target_weight_kg"] = t
    await update.message.reply_text(
        "С каким темпом идём?",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🐢 Медленно (~0.25 кг/нед)", callback_data="pace_slow")],
                [InlineKeyboardButton("🚶 Обычно (~0.5 кг/нед)", callback_data="pace_normal")],
                [InlineKeyboardButton("🚀 Быстро (~0.75 кг/нед)", callback_data="pace_fast")],
            ]
        ),
    )
    return PACE


async def on_pace(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    ctx.user_data["pace"] = q.data.replace("pace_", "")
    await q.edit_message_text(f"Темп: {PACE_LABELS[ctx.user_data['pace']]}")
    return await _finish_onboarding(update, ctx)


async def _finish_onboarding(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    data = ctx.user_data
    targets = compute_targets(data)
    fields = {
        "name": update.effective_user.first_name or "",
        "sex": data["sex"],
        "age": data["age"],
        "height_cm": data["height_cm"],
        "weight_kg": data["weight_kg"],
        "activity": data["activity"],
        "training_days": data["training_days"],
        "goal": data["goal"],
        "target_weight_kg": data.get("target_weight_kg"),
        "pace": data.get("pace") or "normal",
        "kcal_target": targets["kcal_target"],
        "protein_g": targets["protein_g"],
        "fats_g": targets["fats_g"],
        "carbs_g": targets["carbs_g"],
        "onboarded": 1,
    }
    await db.upsert_user(chat_id, **fields)
    # сохраним стартовый вес
    await db.add_weight(chat_id, data["weight_kg"])

    user = await db.get_user(chat_id)
    msg = (
        "✅ Готово! Твой профиль сохранён.\n\n"
        + fmt_user(user)
        + f"\n\nBMR: {targets['bmr']} ккал, TDEE: {targets['tdee']} ккал.\n\n"
        + fmt_targets(user)
        + "\n\nТеперь можешь:\n"
        "• /menu — AI составит план на день\n"
        "• /eat — записать приём пищи\n"
        "• /remind — настроить напоминания\n"
        "• /help — все команды"
    )
    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text(msg, parse_mode=ParseMode.HTML)
    ctx.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Анкета отменена. /start — начать заново.", reply_markup=ReplyKeyboardRemove()
    )
    ctx.user_data.clear()
    return ConversationHandler.END


# ──────────────────────── обычные команды ────────────────────────


async def need_onboard(update: Update) -> dict | None:
    user = await db.get_user(update.effective_chat.id)
    if not user or not user.get("onboarded"):
        await update.message.reply_text("Сначала пройди анкету: /start")
        return None
    return user


async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = await need_onboard(update)
    if not user:
        return
    await update.message.reply_text(fmt_user(user) + "\n\n" + fmt_targets(user), parse_mode=ParseMode.HTML)


async def cmd_targets(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = await need_onboard(update)
    if not user:
        return
    await update.message.reply_text(fmt_targets(user), parse_mode=ParseMode.HTML)


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = await need_onboard(update)
    if not user:
        return
    await update.message.chat.send_action("typing")
    try:
        text = await generate_menu(
            user, user["kcal_target"], user["protein_g"], user["fats_g"], user["carbs_g"]
        )
    except Exception as e:
        logger.exception("generate_menu failed")
        await update.message.reply_text(f"AI временно недоступен: {e}")
        return
    await update.message.reply_text(text)


async def cmd_eat(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = await need_onboard(update)
    if not user:
        return
    text = " ".join(ctx.args) if ctx.args else ""
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""
    if not text:
        await update.message.reply_text(
            "Опиши что съел: /съел 200г куриной грудки и 100г риса"
        )
        return
    await update.message.chat.send_action("typing")
    try:
        parsed = await parse_food(text)
    except Exception as e:
        logger.exception("parse_food failed")
        await update.message.reply_text(f"AI ошибка: {e}")
        return
    await db.add_meal(
        update.effective_chat.id,
        parsed["description"],
        parsed["kcal"],
        parsed["protein"],
        parsed["fats"],
        parsed["carbs"],
        raw=text,
    )
    totals = await db.totals_today(update.effective_chat.id)
    bar = progress_bar(totals["kcal"], user["kcal_target"])
    await update.message.reply_text(
        f"📝 Записал: <b>{parsed['description']}</b>\n"
        f"~{round(parsed['kcal'])} ккал, "
        f"Б {round(parsed['protein'])} / Ж {round(parsed['fats'])} / У {round(parsed['carbs'])}\n\n"
        f"Сегодня: <b>{round(totals['kcal'])}/{user['kcal_target']}</b> ккал {bar}\n"
        f"Б {round(totals['protein'])}/{user['protein_g']}  "
        f"Ж {round(totals['fats'])}/{user['fats_g']}  "
        f"У {round(totals['carbs'])}/{user['carbs_g']}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = await need_onboard(update)
    if not user:
        return
    meals = await db.meals_today(update.effective_chat.id)
    totals = await db.totals_today(update.effective_chat.id)
    if not meals:
        await update.message.reply_text("Сегодня пока ничего не записано. /съел — добавить.")
        return
    lines = ["<b>Что съел сегодня:</b>"]
    for m in meals:
        ts = datetime.fromisoformat(m["ts"]).strftime("%H:%M")
        lines.append(
            f"• {ts} <b>{m['description']}</b> — {round(m['kcal'])} ккал "
            f"(Б{round(m['protein'])}/Ж{round(m['fats'])}/У{round(m['carbs'])})"
        )
    bar = progress_bar(totals["kcal"], user["kcal_target"])
    lines.append("")
    lines.append(
        f"<b>Итого:</b> {round(totals['kcal'])}/{user['kcal_target']} ккал {bar}\n"
        f"Б {round(totals['protein'])}/{user['protein_g']}  "
        f"Ж {round(totals['fats'])}/{user['fats_g']}  "
        f"У {round(totals['carbs'])}/{user['carbs_g']}"
    )
    remaining = user["kcal_target"] - totals["kcal"]
    if remaining > 0:
        lines.append(f"\nОсталось: <b>{round(remaining)}</b> ккал")
    else:
        lines.append(f"\nПревышение: <b>+{round(-remaining)}</b> ккал")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_undo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = await need_onboard(update)
    if not user:
        return
    deleted = await db.delete_last_meal(update.effective_chat.id)
    if not deleted:
        await update.message.reply_text("Нечего отменять.")
        return
    await update.message.reply_text(
        f"❌ Удалил: {deleted['description']} ({round(deleted['kcal'])} ккал)"
    )


# /вес — диалог
async def cmd_weight_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user = await need_onboard(update)
    if not user:
        return ConversationHandler.END
    if ctx.args:
        try:
            w = float(ctx.args[0].replace(",", "."))
            return await _save_weight(update, ctx, w)
        except ValueError:
            pass
    await update.message.reply_text("Текущий вес в кг?")
    return WEIGHT_INPUT


async def on_weight_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        w = float((update.message.text or "0").replace(",", "."))
        if not 30 <= w <= 250:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введи число от 30 до 250:")
        return WEIGHT_INPUT
    return await _save_weight(update, ctx, w)


async def _save_weight(update: Update, ctx: ContextTypes.DEFAULT_TYPE, w: float) -> int:
    chat_id = update.effective_chat.id
    user = await db.get_user(chat_id)
    prev = user.get("weight_kg") or w
    await db.add_weight(chat_id, w)
    # пересчитать КБЖУ
    user = await db.get_user(chat_id)
    targets = compute_targets(user)
    await db.upsert_user(
        chat_id,
        kcal_target=targets["kcal_target"],
        protein_g=targets["protein_g"],
        fats_g=targets["fats_g"],
        carbs_g=targets["carbs_g"],
    )
    diff = w - prev
    arrow = "📈" if diff > 0 else "📉" if diff < 0 else "➡️"
    sign = "+" if diff > 0 else ""
    user = await db.get_user(chat_id)
    await update.message.reply_text(
        f"✅ Записал вес: <b>{w} кг</b> {arrow} {sign}{diff:.1f} кг от прошлого замера.\n\n"
        f"Норма обновлена: {user['kcal_target']} ккал ({user['protein_g']}/"
        f"{user['fats_g']}/{user['carbs_g']} БЖУ).",
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def cmd_progress(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = await need_onboard(update)
    if not user:
        return
    history = await db.weight_history(update.effective_chat.id, limit=180)
    if len(history) < 2:
        await update.message.reply_text(
            "Пока мало данных для графика. Запиши вес ещё хотя бы раз: /вес"
        )
        return
    target = user.get("target_weight_kg")
    png = weight_chart(history, target=target)
    if not png:
        await update.message.reply_text("Не получилось построить график.")
        return
    first_w = history[0][1]
    last_w = history[-1][1]
    diff = last_w - first_w
    sign = "+" if diff > 0 else ""
    caption = (
        f"<b>Динамика веса</b>\n"
        f"Старт: {first_w:.1f} кг → Сейчас: <b>{last_w:.1f} кг</b> ({sign}{diff:.1f} кг)\n"
    )
    if target:
        delta = target - last_w
        caption += f"До цели: <b>{abs(delta):.1f} кг</b> ({'наберать' if delta > 0 else 'сбросить'})"
    await update.message.reply_photo(png, caption=caption, parse_mode=ParseMode.HTML)


# ──────────────────────── напоминания ────────────────────────

REMINDER_TEXTS = {
    "breakfast": "🍳 Время завтрака! Не пропускай — это запуск метаболизма.",
    "lunch":     "🍱 Время обеда. Не забудь добавить белок!",
    "dinner":    "🍲 Время ужина. Лёгкий ужин, но с белком.",
    "water":     "💧 Попей воды!",
    "weigh":     "⚖️ Время взвеситься. Запиши: /вес",
}

REMINDER_LABELS = {
    "breakfast": "🍳 Завтрак",
    "lunch":     "🍱 Обед",
    "dinner":    "🍲 Ужин",
    "water":     "💧 Вода",
    "weigh":     "⚖️ Взвешивание",
}


async def reminder_callback(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    job = ctx.job
    chat_id = job.chat_id
    kind = job.data.get("kind")
    text = REMINDER_TEXTS.get(kind, "Время напоминания.")
    try:
        await ctx.bot.send_message(chat_id, text)
    except Exception as e:
        logger.warning("reminder send failed for %s: %s", chat_id, e)


def schedule_reminder(
    application: Application, chat_id: int, kind: str, time_hhmm: str
) -> None:
    hh, mm = map(int, time_hhmm.split(":"))
    t = dtime(hour=hh, minute=mm, tzinfo=MSK)
    name = f"rem_{chat_id}_{kind}"
    # удалить старое
    for j in application.job_queue.get_jobs_by_name(name):
        j.schedule_removal()
    application.job_queue.run_daily(
        reminder_callback,
        time=t,
        chat_id=chat_id,
        name=name,
        data={"kind": kind},
    )


async def cmd_remind_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user = await need_onboard(update)
    if not user:
        return ConversationHandler.END
    kb = [
        [InlineKeyboardButton(REMINDER_LABELS[k], callback_data=f"remk_{k}")]
        for k in REMINDER_LABELS
    ]
    await update.message.reply_text("Какое напоминание настроить?", reply_markup=InlineKeyboardMarkup(kb))
    return REM_KIND


async def on_rem_kind(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()
    kind = q.data.replace("remk_", "")
    ctx.user_data["rem_kind"] = kind
    await q.edit_message_text(
        f"{REMINDER_LABELS[kind]} — во сколько присылать? Формат ЧЧ:ММ (МСК), например 08:30"
    )
    return REM_TIME


async def on_rem_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if not m or not (0 <= int(m.group(1)) < 24) or not (0 <= int(m.group(2)) < 60):
        await update.message.reply_text("Формат ЧЧ:ММ (например 08:30):")
        return REM_TIME
    hhmm = f"{int(m.group(1)):02d}:{m.group(2)}"
    chat_id = update.effective_chat.id
    kind = ctx.user_data["rem_kind"]
    await db.set_reminder(chat_id, kind, hhmm, enabled=True)
    schedule_reminder(ctx.application, chat_id, kind, hhmm)
    await update.message.reply_text(
        f"✅ {REMINDER_LABELS[kind]} в {hhmm} (МСК) — настроено.\n"
        f"Управление: /напоминания"
    )
    ctx.user_data.clear()
    return ConversationHandler.END


async def cmd_reminders(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    rems = await db.get_reminders(chat_id)
    if not rems:
        await update.message.reply_text("Напоминаний нет. /напомнить — добавить.")
        return
    lines = ["<b>Твои напоминания:</b>"]
    for r in rems:
        status = "✅" if r["enabled"] else "❌"
        lines.append(f"{status} {REMINDER_LABELS.get(r['kind'], r['kind'])} в {r['time_hhmm']} (МСК)")
    lines.append("\n/удалить_напоминание тип — выкл (тип: breakfast/lunch/dinner/water/weigh)")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_del_reminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text(
            "Укажи тип: /удалить_напоминание breakfast (или lunch/dinner/water/weigh)"
        )
        return
    kind = ctx.args[0].strip()
    chat_id = update.effective_chat.id
    await db.delete_reminder(chat_id, kind)
    name = f"rem_{chat_id}_{kind}"
    for j in ctx.application.job_queue.get_jobs_by_name(name):
        j.schedule_removal()
    await update.message.reply_text(f"❌ Напоминание {kind} удалено.")


# ──────────────────────── смена цели и AI-вопрос ────────────────────────


async def cmd_goal(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = await need_onboard(update)
    if not user:
        return
    await update.message.reply_text(
        "Чтобы поменять цель и параметры — пройди анкету заново: /start"
    )


async def cmd_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = await need_onboard(update)
    if not user:
        return
    question = " ".join(ctx.args) if ctx.args else ""
    if not question:
        await update.message.reply_text(
            "Задай вопрос тренеру: /спросить как ускорить набор массы?"
        )
        return
    await update.message.chat.send_action("typing")
    totals = await db.totals_today(update.effective_chat.id)
    try:
        ans = await coach_advice(user, question, totals)
    except Exception as e:
        logger.exception("coach_advice failed")
        await update.message.reply_text(f"AI ошибка: {e}")
        return
    await update.message.reply_text(ans)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "<b>Команды:</b>\n"
        "/start — начать / анкета заново\n"
        "/profile — твои данные и норма\n"
        "/targets — норма КБЖУ\n"
        "/menu — AI составит меню на день\n"
        "/eat &lt;что съел&gt; — записать приём пищи\n"
        "/today — что съел сегодня + остаток\n"
        "/undo — удалить последний приём\n"
        "/weight [кг] — записать вес\n"
        "/progress — график веса\n"
        "/remind — настроить напоминание\n"
        "/reminders — список напоминаний\n"
        "/delreminder &lt;тип&gt; — удалить (breakfast/lunch/dinner/water/weigh)\n"
        "/ask &lt;вопрос&gt; — спросить AI-тренера\n"
        "/goal — поменять цель\n\n"
        "💡 <b>Без команд:</b>\n"
        "«съел 200г курицы и 100г риса» → запишу в дневник\n"
        "«как ускорить набор массы?» → AI-тренер ответит",
        parse_mode=ParseMode.HTML,
    )


# ──────────────────────── свободный текст → AI-тренер или еда ────────────────────────


async def free_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = await need_onboard(update)
    if not user:
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    # эвристика: если текст похож на "съел/выпил/перекусил/завтракал" → еда; иначе вопрос
    eat_kw = ("съел", "сьел", "съела", "выпил", "выпила", "перекус", "поел", "поела",
              "завтрак", "обед", "ужин", "скушал", "скушала", "позавтракал")
    lower = text.lower()
    if any(k in lower for k in eat_kw):
        ctx.args = text.split()
        await cmd_eat(update, ctx)
    else:
        ctx.args = text.split()
        await cmd_ask(update, ctx)


# ──────────────────────── рестарт напоминаний при старте ────────────────────────


async def post_init(application: Application) -> None:
    await db.init_db()
    rems = await db.all_active_reminders()
    for r in rems:
        try:
            schedule_reminder(application, r["chat_id"], r["kind"], r["time_hhmm"])
        except Exception as e:
            logger.warning("could not restore reminder %s: %s", r, e)
    logger.info("Bot ready. Restored %d reminders.", len(rems))


# ──────────────────────── main ────────────────────────


def build_app() -> Application:
    token = os.environ["TELEGRAM_TOKEN"]
    app = Application.builder().token(token).post_init(post_init).build()

    # /start onboarding
    onboarding = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            GOAL: [CallbackQueryHandler(on_goal, pattern=r"^goal_")],
            SEX: [CallbackQueryHandler(on_sex, pattern=r"^sex_")],
            AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_age)],
            HEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_height)],
            WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_weight)],
            ACTIVITY: [CallbackQueryHandler(on_activity, pattern=r"^act_")],
            TRAINING: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_training)],
            TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_target)],
            PACE: [CallbackQueryHandler(on_pace, pattern=r"^pace_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    # /weight (вес)
    weight_conv = ConversationHandler(
        entry_points=[CommandHandler(["weight", "ves"], cmd_weight_start)],
        states={WEIGHT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_weight_input)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    # /remind
    rem_conv = ConversationHandler(
        entry_points=[CommandHandler("remind", cmd_remind_start)],
        states={
            REM_KIND: [CallbackQueryHandler(on_rem_kind, pattern=r"^remk_")],
            REM_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_rem_time)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(onboarding)
    app.add_handler(weight_conv)
    app.add_handler(rem_conv)

    # обычные команды (Telegram разрешает только ASCII в командах)
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler(["targets", "racion"], cmd_targets))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("eat", cmd_eat))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("undo", cmd_undo))
    app.add_handler(CommandHandler("progress", cmd_progress))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(CommandHandler("delreminder", cmd_del_reminder))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("goal", cmd_goal))
    app.add_handler(CommandHandler("help", cmd_help))

    # свободный текст
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))

    return app


def main() -> None:
    app = build_app()
    mode = os.environ.get("MODE", "polling").lower()
    if mode == "webhook":
        url = os.environ["WEBHOOK_URL"].rstrip("/")
        port = int(os.environ.get("PORT", "8080"))
        token = os.environ["TELEGRAM_TOKEN"]
        secret = token.split(":")[0]  # короткий путь
        full_url = f"{url}/{secret}"
        logger.info("Starting webhook on port %d, URL=%s", port, full_url)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=secret,
            webhook_url=full_url,
            drop_pending_updates=True,
        )
    else:
        logger.info("Starting polling…")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
