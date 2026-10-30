"""Shared SQLite persistence for radar output and Bitrix delivery history."""
import json
import re
import sqlite3
from pathlib import Path

DB_NAME = str(Path(__file__).with_name("leads.db"))


def normalize_name(value: str) -> str:
    value = (value or "").casefold().replace("ё", "е")
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\b(тоо|ао|ип|ооо|тов|llp|jsc|гку|ку|кгп|ргп|гу|рго)\b", " ", value)
    return " ".join(value.split())


def connect():
    db = sqlite3.connect(DB_NAME, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS sync_log (id INTEGER PRIMARY KEY, company_key TEXT NOT NULL, bitrix_id TEXT, action TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, error TEXT)")
    return db


async def init_db():
    db = connect()
    db.execute("CREATE TABLE IF NOT EXISTS companies (key TEXT PRIMARY KEY, data TEXT, first_seen TEXT, last_seen TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS seen (url TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE IF NOT EXISTS tenders (url TEXT PRIMARY KEY, company_key TEXT NOT NULL, description TEXT, amount TEXT, deadline TEXT, data TEXT)")
    db.commit()
    db.close()


async def save_company(company):
    name = str(company.get("name") or "").strip()
    if not name:
        raise ValueError("У компании должно быть название")
    key = "bin:" + str(company.get("bin")) if company.get("bin") else "name:" + normalize_name(name)
    db = connect()
    try:
        row = db.execute("SELECT data,first_seen FROM companies WHERE key=?", (key,)).fetchone()
        now = str(company.get("found_at") or __import__("datetime").datetime.now().isoformat(timespec="seconds"))
        if row:
            old = json.loads(row["data"] or "{}")
            for field, value in company.items():
                if field == "tenders" and value:
                    known = {t.get("url") for t in old.get("tenders", [])}
                    old.setdefault("tenders", []).extend(t for t in value if t.get("url") not in known)
                elif value not in (None, "", []) and old.get(field) in (None, "", []):
                    old[field] = value
            db.execute("UPDATE companies SET data=?, last_seen=? WHERE key=?", (json.dumps(old, ensure_ascii=False), now, key))
            for tender in old.get("tenders", []):
                if tender.get("url"):
                    db.execute("INSERT OR IGNORE INTO tenders(url,company_key,description,amount,deadline,data) VALUES(?,?,?,?,?,?)", (tender["url"], key, tender.get("description", ""), tender.get("amount", ""), tender.get("deadline", ""), json.dumps(tender, ensure_ascii=False)))
        else:
            db.execute("INSERT INTO companies VALUES(?,?,?,?)", (key, json.dumps(company, ensure_ascii=False), now, now))
            for tender in company.get("tenders", []):
                if tender.get("url"):
                    db.execute("INSERT OR IGNORE INTO tenders(url,company_key,description,amount,deadline,data) VALUES(?,?,?,?,?,?)", (tender["url"], key, tender.get("description", ""), tender.get("amount", ""), tender.get("deadline", ""), json.dumps(tender, ensure_ascii=False)))
        db.commit()
        return key
    finally:
        db.close()


async def get_companies(limit=20):
    db = connect()
    try:
        rows = db.execute("SELECT data FROM companies ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row[0]) for row in rows]
    finally:
        db.close()


def record_sync(company_key: str, action: str, bitrix_id=None, error=None):
    db = connect()
    try:
        db.execute("INSERT INTO sync_log(company_key,bitrix_id,action,error) VALUES(?,?,?,?)", (company_key, str(bitrix_id) if bitrix_id else None, action, error))
        db.commit()
    finally:
        db.close()
