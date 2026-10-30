# B2B Radar Kazakhstan

Сервис собирает публичные закупки, отбирает тендеры для строительных и ремонтных компаний, сохраняет результаты в SQLite и может создавать лиды в Bitrix24. Критерии поиска настраиваются отдельно в `criteria.json`.

## Требования

- Python 3.10 или новее.
- REST webhook Bitrix24 с правами на CRM.
- Токен Telegram-бота, если нужны бот и отчёты в Telegram.
- API key Gemini, если нужна AI-классификация. При недоступной квоте обработка продолжается по правилам.

## Установка

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Заполните значения в локальном `.env`:

```dotenv
BOT_TOKEN=
BITRIX_WEBHOOK_URL=
GEMINI_API_KEY=
# Optional: TELEGRAM_CHAT_ID=
```

Не публикуйте `.env`. Перед первым запуском настройте Bitrix-поля и источник:

```powershell
.\.venv\Scripts\python.exe bitrix_setup.py
.\.venv\Scripts\python.exe bitrix_setup.py --apply
```

Первая команда только показывает план; вторая создаёт недостающие CRM-поля и источник «ИИ-поиск».

Для дополнительных парсеров, которым нужен браузер, установите `requirements-browser.txt` и Chromium: `python -m playwright install chromium`.

## Запуск

Проверьте Gemini небольшим запросом:

```powershell
.\.venv\Scripts\python.exe check_gemini.py
```

Собрать закупки и обновить локальную базу без записи в CRM:

```powershell
.\.venv\Scripts\python.exe radar.py run
```

Посмотреть, какие лиды синхронизатор подготовил бы:

```powershell
.\.venv\Scripts\python.exe bitrix_sync.py --limit 5
```

Полная цепочка с реальным созданием лидов:

```powershell
.\.venv\Scripts\python.exe run_daily.py --apply --limit 5
```

`run_daily.py --dry-run` выполняет поиск и предварительную синхронизацию без записи лидов в CRM, но отправляет отчёт в Telegram, если настроены `BOT_TOKEN` и ID чата. Повторная обработка уже сохранённой очереди без нового поиска: `run_daily.py --no-radar --dry-run`.

Telegram-бот запускается отдельно:

```powershell
.\.venv\Scripts\python.exe main.py
```

Запускайте только один экземпляр бота. Напишите ему `/start`, чтобы сохранить ID чата; `/report` показывает последний отчёт.

## Настройки и данные

- `criteria.json` — ключевые слова, веса, исключения, регионы, типы заказчика, лимит лидов и параметры Gemini.
- `leads.db` — локальная база компаний, закупок и журнал синхронизации.
- `daily_report_YYYY-MM-DD.json` — отчёт последнего запуска.
- `radar.log`, `bitrix_sync.log` — журналы ошибок и обработки.
- `backups/` — резервные копии базы.

Локальные базы, журналы, секреты и выгрузки исключены из Git через `.gitignore`. Информация об источниках и текущих ограничениях находится в `SOURCE_STATUS.md`.

## Ограничения

Сейчас включены HTML-парсеры BicoTender и MP.kz. Goszakup отключён; для Samruk-Kazyna и 2GIS нет работающего автоматического адаптера. Перечень и ограничения источников указаны в `SOURCE_STATUS.md`.
