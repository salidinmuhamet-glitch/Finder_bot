"""Create a consistent SQLite backup and retain the latest 30 daily copies."""
from datetime import datetime
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "leads.db"
BACKUP_DIR = ROOT / "backups"


def create_backup() -> Path:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")
    BACKUP_DIR.mkdir(exist_ok=True)
    target = BACKUP_DIR / f"leads_{datetime.now():%Y-%m-%d_%H-%M-%S}.db"
    with sqlite3.connect(DB_PATH) as source, sqlite3.connect(target) as destination:
        source.backup(destination)
    backups = sorted(BACKUP_DIR.glob("leads_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[30:]:
        old.unlink()
    return target


if __name__ == "__main__":
    print(create_backup())
