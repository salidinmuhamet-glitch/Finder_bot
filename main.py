"""Telegram interface for radar data stored in leads.db."""
import asyncio
import json
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from dotenv import load_dotenv

from database import connect
from instance_lock import instance_lock

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN в файле .env")

bot = Bot(token=TOKEN)
dp = Dispatcher()
keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🔎 Найти компании"), KeyboardButton(text="🏢 Найденные компании")],
              [KeyboardButton(text="⚙️ Критерии поиска"), KeyboardButton(text="📊 Статистика")],
              [KeyboardButton(text="📨 Последний отчёт")]],
    resize_keyboard=True,
)


def load_companies():
    db = connect()
    try:
        rows = db.execute("SELECT data FROM companies ORDER BY last_seen DESC").fetchall()
        return [json.loads(row[0] or "{}") for row in rows]
    finally:
        db.close()


def latest_report():
    reports = sorted(ROOT.glob("daily_report_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not reports:
        return None
    try:
        return json.loads(reports[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def report_text(report):
    if not report:
        return "Отчёта пока нет. Запустите run_daily.py."
    return (f"📨 Отчёт за {report.get('date', '—')} ({report.get('mode', '—')})\n\n"
            f"Найдено: {report.get('found', 0)}\nПодошло: {report.get('matched', 0)}\n"
            f"В очереди: {report.get('sync_candidates', 0)}\nСоздано лидов: {report.get('created', 0)}\n"
            f"Дубли: {report.get('duplicates', 0)}\nОшибки: {report.get('errors', 0)}\n"
            f"Gemini: {report.get('ai_mode', '—')}; проверено: {report.get('ai_reviewed', 0)}, "
            f"отклонено ИИ: {report.get('ai_rejected', 0)}, ошибок: {report.get('ai_errors', 0)}, "
            f"не проверено: {report.get('ai_unreviewed', 0)}\n"
            f"Telegram: {report.get('telegram', '—')}")


@dp.message(CommandStart())
async def start(message: Message):
    if message.chat.type == "private":
        chat_file = ROOT / "telegram_chat_id.txt"
        if not chat_file.exists():
            chat_file.write_text(str(message.chat.id), encoding="utf-8")
    await message.answer(
        "B2B Finder Kazakhstan подключён к общей базе радара.\n"
        "Команды: /report — последний запуск, /start — показать меню.\n\n" + report_text(latest_report()),
        reply_markup=keyboard,
    )


@dp.message(Command("report"))
@dp.message(F.text == "📨 Последний отчёт")
async def show_report(message: Message):
    await message.answer(report_text(latest_report()))


@dp.message(F.text == "🔎 Найти компании")
async def find_companies(message: Message):
    await message.answer(f"В общей базе радара сейчас компаний: {len(load_companies())}.")


@dp.message(F.text == "⚙️ Критерии поиска")
async def criteria(message: Message):
    try:
        data = json.loads((ROOT / "criteria.json").read_text(encoding="utf-8"))
        await message.answer(
            "⚙️ Критерии\n"
            f"Минимальный балл: {data.get('minimum_score')}\n"
            f"Регионы: {', '.join(data.get('regions', [])) or 'все'}\n"
            f"Типы заказчика: {', '.join(data.get('customer_types', [])) or 'все'}\n"
            f"Источники: BicoTender, MP.kz"
        )
    except (OSError, json.JSONDecodeError):
        await message.answer("Не удалось прочитать criteria.json.")


@dp.message(F.text == "🏢 Найденные компании")
async def companies(message: Message):
    items = load_companies()
    if not items:
        await message.answer("База пока пустая. Запустите run_daily.py.")
        return
    await message.answer(f"🏢 В базе {len(items)} компаний. Показываю последние 5:")
    for company in items[:5]:
        tenders = company.get("tenders", [])
        best = max(tenders, key=lambda t: t.get("score", 0), default={})
        text = (
            f"🏢 {company.get('name') or 'Без названия'}\n"
            f"БИН: {company.get('bin') or 'не указан'}\n"
            f"Сфера: {company.get('activity') or 'не определена'}\n"
            f"Тип: {company.get('customer_type') or 'не указан'}\n"
            f"Регион: {company.get('region') or 'не указан'}; город: {company.get('city') or 'не указан'}\n"
            f"Телефон: {company.get('phone') or 'не найден'}; email: {company.get('email') or 'не найден'}\n"
            f"Тендеров: {len(tenders)}; лучший балл: {best.get('score', 0)}\n"
        )
        if best:
            text += (f"\n📌 {best.get('description', '')[:700]}\n"
                     f"Сумма: {best.get('amount') or 'не указана'}; срок: {best.get('deadline') or 'не указан'}\n"
                     f"{best.get('url', '')}")
        await message.answer(text)


@dp.message(F.text == "📊 Статистика")
async def statistics(message: Message):
    items = load_companies()
    tenders = [t for company in items for t in company.get("tenders", [])]
    report = latest_report() or {}
    await message.answer(
        f"📊 B2B Finder\n\nКомпаний в базе: {len(items)}\nТендеров в базе: {len(tenders)}\n"
        f"С БИН: {sum(bool(x.get('bin')) for x in items)}\n"
        f"С телефоном: {sum(bool(x.get('phone')) for x in items)}\n"
        f"С email: {sum(bool(x.get('email')) for x in items)}\n\n"
        f"Последний запуск: {report.get('date', 'не запускался')}"
    )


async def main():
    print("B2B Finder bot polling started")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        with instance_lock("telegram_bot"):
            asyncio.run(main())
    except RuntimeError as exc:
        raise SystemExit(str(exc))
