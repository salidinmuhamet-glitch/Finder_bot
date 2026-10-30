"""Import a radar JSON export into the shared leads.db."""
import argparse
import asyncio
import json
from pathlib import Path

from database import DB_NAME, get_companies, init_db, save_company


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", help="Файл экспорта радара (если не указан, берётся последний leads_*.json)")
    args = parser.parse_args()
    exports = sorted(Path(__file__).resolve().parent.glob("leads_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    path = Path(args.file) if args.file else (exports[0] if exports else Path("leads.json"))
    if not path.exists():
        raise SystemExit(f"Файл не найден: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("leads", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise SystemExit("Ожидался список или объект радара с массивом leads")
    await init_db()
    imported = skipped = 0
    for item in items:
        if not isinstance(item, dict) or not item.get("name"):
            skipped += 1
            continue
        try:
            await save_company(item)
            imported += 1
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"Пропущена запись {item.get('name', '<без имени>')}: {exc}")
            skipped += 1
    print(f"База: {DB_NAME}\nИмпортировано: {imported}\nПропущено: {skipped}\nВ базе: {len(await get_companies(100000))}")


if __name__ == "__main__":
    asyncio.run(main())
