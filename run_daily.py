"""Daily radar to shared database to Bitrix dry-run/apply and local summary."""
import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv

from database import connect
from backup_database import create_backup
from instance_lock import instance_lock

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable
load_dotenv(ROOT / ".env")


def run(args):
    backup_path = create_backup()
    print(f"Резервная копия базы: {backup_path.name}")
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", dest="apply", action="store_true", help="создавать лиды в Bitrix (поведение по умолчанию)")
    mode.add_argument("--dry-run", dest="apply", action="store_false", help="только показать кандидатов, без записи в Bitrix")
    parser.set_defaults(apply=True)
    parser.add_argument("--no-radar", action="store_true", help="повторить синхронизацию сохранённых записей без нового сбора")
    parser.add_argument("--limit", type=int, default=50)
    opts = parser.parse_args(args)
    report = {"date": date.today().isoformat(), "mode": "apply" if opts.apply else "dry-run",
              "found": 0, "matched": 0, "sync_candidates": 0, "created": 0, "duplicates": 0, "errors": 0,
              "ai_mode": "unknown", "ai_reviewed": 0, "ai_promoted": 0, "ai_rejected": 0,
              "ai_errors": 0, "ai_unreviewed": 0}
    if not opts.no_radar:
        subprocess.run([PYTHON, str(ROOT / "radar.py"), "run"], cwd=ROOT, check=True)
        export = ROOT / f"leads_{report['date']}.json"
        if export.exists():
            data = json.loads(export.read_text(encoding="utf-8"))
            stats = data.get("stats", {})
            report["found"], report["matched"] = stats.get("rows_found", 0), stats.get("matched", 0)
            for key in ("ai_mode", "ai_reviewed", "ai_promoted", "ai_rejected", "ai_errors", "ai_unreviewed"):
                report[key] = stats.get(key, report[key])
        subprocess.run([PYTHON, str(ROOT / "import_json.py"), str(export)], cwd=ROOT, check=True)
    db = connect()
    try:
        pending = db.execute("""SELECT COUNT(*) FROM companies c
            WHERE NOT EXISTS (SELECT 1 FROM sync_log s WHERE s.company_key=c.key
                              AND s.action IN ('created','duplicate','partial','ignored_test'))
              AND COALESCE(json_extract(c.data, '$.source_url'), '') NOT LIKE 'https://example.com/test%'
              AND COALESCE(json_extract(c.data, '$.bin'), '') NOT IN
                  ('123456789012','123456789013','123456789014','123456789015','123456789016')""").fetchone()[0]
    finally:
        db.close()
    report["sync_candidates"] = min(pending, max(0, opts.limit))
    db = connect()
    try:
        run_start_log_id = db.execute("SELECT COALESCE(MAX(id), 0) FROM sync_log").fetchone()[0]
    finally:
        db.close()
    command = [PYTHON, str(ROOT / "bitrix_sync.py"), "--limit", str(opts.limit)]
    if opts.apply:
        # Idempotent setup: create only fields and source that are missing.
        subprocess.run([PYTHON, str(ROOT / "bitrix_setup.py"), "--apply"], cwd=ROOT, check=True)
        command.append("--apply")
    else:
        command.extend(["--preview-file", str(ROOT / f"bitrix_preview_{report['date']}.json")])
    subprocess.run(command, cwd=ROOT, check=True)
    db = connect()
    try:
        counts = dict(db.execute("SELECT action,COUNT(*) FROM sync_log WHERE id>? GROUP BY action", (run_start_log_id,)).fetchall())
    finally:
        db.close()
    report["created"] = counts.get("created", 0) + counts.get("partial", 0)
    report["duplicates"] = counts.get("duplicate", 0)
    report["errors"] = counts.get("error", 0)
    report["telegram"] = send_telegram_report(report)
    report_path = ROOT / f"daily_report_{report['date']}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Ежедневный отчёт:")
    for key, value in report.items():
        print(f"{key}: {value}")
    print(f"Файл: {report_path.name}")


def send_telegram_report(report):
    token = os.getenv("BOT_TOKEN", "")
    chat_file = ROOT / "telegram_chat_id.txt"
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "") or (chat_file.read_text(encoding="utf-8").strip() if chat_file.exists() else "")
    if not token:
        return "не отправлен: нет BOT_TOKEN"
    if not chat_id:
        return "не отправлен: напишите боту /start для регистрации чата"
    message = (f"B2B Radar — {report['date']} ({report['mode']})\n"
               f"Найдено: {report['found']}\nПодошло: {report['matched']}\n"
               f"В очереди синхронизации: {report['sync_candidates']}\n"
               f"Создано лидов: {report['created']}\nДубли: {report['duplicates']}\nОшибки: {report['errors']}")
    message += (f"\nGemini: {report.get('ai_mode', 'unknown')}; проверено: {report.get('ai_reviewed', 0)}; "
                f"отклонено: {report.get('ai_rejected', 0)}; ошибок AI: {report.get('ai_errors', 0)}; "
                f"не проверено: {report.get('ai_unreviewed', 0)}")
    try:
        response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                 json={"chat_id": chat_id, "text": message}, timeout=15)
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            return "ошибка Telegram: " + str(result.get("description", "неизвестная ошибка"))
        return "отправлен"
    except requests.RequestException as exc:
        return f"ошибка Telegram: {exc}"


if __name__ == "__main__":
    try:
        with instance_lock("run_daily"):
            run(sys.argv[1:])
    except RuntimeError as exc:
        raise SystemExit(str(exc))
