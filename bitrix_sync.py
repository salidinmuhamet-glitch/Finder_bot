"""Send pending radar companies to Bitrix24 as NEW leads. Use --apply to write."""
import argparse
import json
import logging
import os
import re
import time
import sys
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

from database import DB_NAME, connect, normalize_name

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

load_dotenv(Path(__file__).with_name(".env"))
logging.basicConfig(filename=Path(__file__).with_name("bitrix_sync.log"), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
log = logging.getLogger("bitrix_sync")
WEBHOOK = os.getenv("BITRIX_WEBHOOK_URL", "").rstrip("/") + "/"
SOURCE_NAME = "ИИ-поиск"
BIN_FIELD = os.getenv("BITRIX_BIN_FIELD", "UF_CRM_BIN")
RATE_DELAY = 0.55
BAD_WEBSITE_HOSTS = ("googletagmanager.com", "google-analytics.com", "googlesyndication.com", "doubleclick.net",
                     "enable-javascript.com", "mp.kz", "bicotender", "goszakup", "zakup.sk", "2gis.", "facebook.", "instagram.",
                     "youtube.", "twitter.", "yandex.", "schema.org")


def clean_site(value):
    if not value:
        return ""
    parsed = urlparse(value if value.startswith(("http://", "https://")) else "https://" + value)
    host = parsed.netloc.casefold()
    if not host or "." not in host or any(part in host for part in BAD_WEBSITE_HOSTS):
        return ""
    return parsed.geturl()


def clean_email(value):
    value = (value or "").strip()
    if "@" not in value:
        return ""
    domain = value.rsplit("@", 1)[1].casefold()
    bad = ("enable-javascript.com", "googletagmanager.com", "google-analytics.com", "mp.kz", "bicotender")
    return "" if any(part in domain for part in bad) else value


class Bitrix:
    def __init__(self):
        if not WEBHOOK or WEBHOOK == "/":
            raise RuntimeError("В .env не задан BITRIX_WEBHOOK_URL")
        self.session = requests.Session()
        self.last_call = 0.0

    def call(self, method, params=None):
        pause = RATE_DELAY - (time.monotonic() - self.last_call)
        if pause > 0:
            time.sleep(pause)
        error = None
        for attempt in range(4):
            try:
                response = self.session.post(WEBHOOK + method + ".json", json=params or {}, timeout=30)
                self.last_call = time.monotonic()
                response.raise_for_status()
                body = response.json()
                if "error" in body:
                    raise RuntimeError(f"Bitrix {body['error']}: {body.get('error_description', '')}")
                return body.get("result")
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                error = exc
                if attempt == 3:
                    break
                time.sleep(2 ** attempt)
        raise RuntimeError(str(error))


def field(company, name):
    mapping = {"site": company.get("site"), "website": company.get("site"), "phone": company.get("phone"), "email": company.get("email")}
    return mapping.get(name, company.get(name))


def lead_fields(company, source_id):
    tenders = company.get("tenders", [])
    best = max(tenders, key=lambda t: t.get("score", 0), default={})
    site = clean_site(company.get("site", ""))
    email = clean_email(company.get("email", ""))
    activity = company.get("activity", "")
    if not activity and best.get("description"):
        from radar import infer_activity
        activity = infer_activity(best["description"], company.get("customer_type", ""))
    score = company.get("score") or best.get("score", 0)
    priority = company.get("priority") or ("высокий" if score >= 7 else "средний" if score >= 4 else "низкий")
    comments = [company.get("summary", ""), f"Источник: {company.get('source', 'радар')}",
                f"Дата обнаружения: {company.get('found_at', '')}"]
    if tenders:
        comments.append("Тендеры:\n" + "\n".join(f"• {t.get('description','')} | {t.get('amount','')} | срок: {t.get('deadline','')} | {t.get('url','')}" for t in tenders[:20]))
    fields = {"TITLE": company.get("name", "Новый лид"), "COMPANY_TITLE": company.get("name", ""),
              "STATUS_ID": "NEW", "SOURCE_ID": source_id, "COMMENTS": "\n\n".join(x for x in comments if x),
              "ADDRESS": ", ".join(x for x in [company.get("city"), company.get("region")] if x),
              "WEB": [{"VALUE": site, "VALUE_TYPE": "WORK"}] if site else [],
              "PHONE": [{"VALUE": company["phone"], "VALUE_TYPE": "WORK"}] if company.get("phone") else [],
              "EMAIL": [{"VALUE": email, "VALUE_TYPE": "WORK"}] if email else [],
              BIN_FIELD: company.get("bin", ""), "UF_CRM_ACTIVITY": activity,
              "UF_CRM_CUSTOMER_TYPE": company.get("customer_type", ""),
              "UF_CRM_SCORE": str(score), "UF_CRM_PRIORITY": priority,
              "UF_CRM_TENDER_DEADLINE": last_date(best.get("deadline", "")), "UF_CRM_TENDER_AMOUNT": amount_number(best.get("amount", ""))}
    return {k: v for k, v in fields.items() if v not in (None, "", [])}


def last_date(value):
    dates = re.findall(r"(\d{2})\.(\d{2})\.(\d{4})", value or "")
    if not dates:
        return value
    day, month, year = dates[-1]
    return f"{day}.{month}.{year}"


def amount_number(value):
    match = re.search(r"\d[\d\s.,]*", value or "")
    if not match:
        return ""
    number = re.sub(r"\s", "", match.group()).rstrip(".,")
    if "." in number and "," in number:
        decimal = "." if number.rfind(".") > number.rfind(",") else ","
        number = re.sub(r"[.,]", "", number.replace(decimal, "§")).replace("§", ".")
    elif "," in number or "." in number:
        separator = "," if "," in number else "."
        tail = number.rsplit(separator, 1)[1]
        number = number.replace(separator, "." if len(tail) in (1, 2) else "")
    try:
        return float(number)
    except ValueError:
        return ""


def timeline_update(company):
    tenders = company.get("tenders", [])
    lines = ["Радар нашёл новые сведения.", f"Компания: {company.get('name', '')}", f"Дата: {company.get('found_at', '')}"]
    lines.extend(f"• {t.get('description', '')} | {t.get('amount', '')} | срок: {t.get('deadline', '')} | {t.get('url', '')}" for t in tenders[:20])
    return "\n".join(x for x in lines if x.strip())


def related_deals(bitrix, reference):
    kind = reference.get("_ENTITY_TYPE", "lead")
    if kind == "deal":
        return []
    company_id = reference.get("COMPANY_ID")
    contact_id = reference.get("CONTACT_ID")
    if kind == "company":
        company_id = reference.get("ID")
    elif kind == "contact":
        contact_id = reference.get("ID")
    filters = []
    if company_id:
        filters.append({"COMPANY_ID": company_id})
    if contact_id:
        filters.append({"CONTACT_ID": contact_id})
    matches = []
    seen = set()
    for filter_ in filters:
        for deal in bitrix.call("crm.deal.list", {"filter": filter_, "select": ["ID", "TITLE", "STAGE_ID", "COMPANY_ID", "CONTACT_ID"]}) or []:
            did = str(deal.get("ID"))
            if did not in seen:
                seen.add(did)
                matches.append({**deal, "_ENTITY_TYPE": "deal"})
    return matches


def exact_reference(bitrix, reference):
    reference = dict(reference)
    reference.setdefault("_ENTITY_TYPE", "lead")
    reference["_RELATED_DEALS"] = related_deals(bitrix, reference)
    return "exact", reference


def find_exact(bitrix, company):
    bin_value = str(company.get("bin") or "").strip()
    if bin_value:
        found = bitrix.call("crm.lead.list", {"filter": {BIN_FIELD: bin_value}, "select": ["ID", "TITLE", "STATUS_ID", "COMPANY_ID", "CONTACT_ID"]}) or []
        if found:
            return exact_reference(bitrix, found[0])
        found = bitrix.call("crm.company.list", {"filter": {"UF_CRM_BIN": bin_value}, "select": ["ID", "TITLE"]}) or []
        if found:
            return exact_reference(bitrix, {**found[0], "_ENTITY_TYPE": "company"})
        # Older companies may have the BIN only in statutory requisites.
        for bin_field in ("RQ_BIN", "RQ_INN"):
            requisites = bitrix.call("crm.requisite.list", {"filter": {"ENTITY_TYPE_ID": 4, bin_field: bin_value}, "select": ["ID", "ENTITY_ID", "ENTITY_TYPE_ID", bin_field]}) or []
            if requisites:
                found = bitrix.call("crm.company.get", {"id": requisites[0]["ENTITY_ID"]}) or {}
                if found:
                    return exact_reference(bitrix, {**found, "_ENTITY_TYPE": "company"})
    comms = []
    for kind in ("PHONE", "EMAIL"):
        value = company.get(kind.lower())
        if value:
            comms.append({"TYPE": kind, "VALUE": value})
    for comm in comms:
        result = bitrix.call("crm.duplicate.findbycomm", {"type": comm["TYPE"], "values": [comm["VALUE"]]}) or {}
        for entity_type, ids in (("lead", result.get("LEAD", [])), ("contact", result.get("CONTACT", [])), ("company", result.get("COMPANY", []))):
            if ids:
                method = {"lead": "crm.lead.get", "contact": "crm.contact.get", "company": "crm.company.get"}[entity_type]
                detail = bitrix.call(method, {"id": ids[0]}) or {"ID": ids[0], "TITLE": "совпадение по контакту"}
                return exact_reference(bitrix, {**detail, "_ENTITY_TYPE": entity_type})
    site = clean_site(company.get("site", ""))
    if site:
        for method, entity_type in (("crm.lead.list", "lead"), ("crm.company.list", "company"), ("crm.contact.list", "contact")):
            found = bitrix.call(method, {"filter": {"%WEB": site}, "select": ["ID", "TITLE"]}) or []
            if found:
                return exact_reference(bitrix, {**found[0], "_ENTITY_TYPE": entity_type})
    # Search names in every required CRM entity; similar names are flagged for review.
    wanted = normalize_name(company.get("name", ""))
    if wanted:
        checks = (("crm.lead.list", "lead", {"%TITLE": company.get("name", "")[:40]}, ["ID", "TITLE", "COMPANY_ID", "CONTACT_ID"]),
                  ("crm.company.list", "company", {"%TITLE": company.get("name", "")[:40]}, ["ID", "TITLE"]),
                  ("crm.contact.list", "contact", {"%NAME": company.get("name", "")[:40]}, ["ID", "NAME", "LAST_NAME"]),
                  ("crm.deal.list", "deal", {"%TITLE": company.get("name", "")[:40]}, ["ID", "TITLE", "STAGE_ID", "COMPANY_ID", "CONTACT_ID"]))
        best = None
        for method, entity_type, filter_, fields in checks:
            for item in bitrix.call(method, {"filter": filter_, "select": fields, "start": 0}) or []:
                candidate = item.get("TITLE") or " ".join(x for x in (item.get("NAME"), item.get("LAST_NAME")) if x)
                score = SequenceMatcher(None, wanted, normalize_name(candidate)).ratio()
                reference = {**item, "_ENTITY_TYPE": entity_type}
                if score >= 0.99:
                    return exact_reference(bitrix, reference)
                if score >= 0.88 and (best is None or score > best[0]):
                    best = (score, reference)
        if best:
            return "partial", best[1]
    return "new", None


def get_source_id(bitrix):
    result = bitrix.call("crm.status.list", {"filter": {"ENTITY_ID": "SOURCE"}}) or []
    entry = next((r for r in result if r.get("NAME", "").casefold() == SOURCE_NAME.casefold()), None)
    if not entry:
        raise RuntimeError("Источник «ИИ-поиск» не найден. Сначала выполните bitrix_setup.py --apply")
    return entry["STATUS_ID"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="создавать лиды в CRM (по умолчанию только dry-run)")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--preview-file", help="сохранить поля dry-run в JSON и не печатать длинные записи")
    args = parser.parse_args()
    db = connect()
    try:
        rows = db.execute("SELECT key,data FROM companies ORDER BY last_seen DESC").fetchall()
        pending = [r for r in rows if not db.execute("SELECT 1 FROM sync_log WHERE company_key=? AND action IN ('created','duplicate','partial','ignored_test')", (r["key"],)).fetchone()]
        pending = pending[:max(0, args.limit)]
        bitrix = Bitrix() if args.apply else None
        source_id = get_source_id(bitrix) if bitrix else "<ИИ-поиск>"
        previews = []
        for row in pending:
            company = json.loads(row["data"] or "{}")
            source_url = str(company.get("source_url") or "")
            bin_value = str(company.get("bin") or "")
            if source_url.startswith("https://example.com/test") or bin_value in {f"1234567890{i:02d}" for i in range(12, 17)}:
                if args.apply:
                    db.execute("INSERT INTO sync_log(company_key,action,error) VALUES(?,?,?)",
                               (row["key"], "ignored_test", "Изолирована тестовая запись; лид в CRM не создавался"))
                    db.commit()
                else:
                    item = {"key": row["key"], "action": "ignored_test"}
                    if args.preview_file:
                        previews.append(item)
                    else:
                        print(json.dumps(item, ensure_ascii=False))
                continue
            if not args.apply:
                item = {"key": row["key"], "action": "would_create", "fields": lead_fields(company, source_id)}
                if args.preview_file:
                    previews.append(item)
                else:
                    print(json.dumps(item, ensure_ascii=False))
                continue
            try:
                kind, match = find_exact(bitrix, company)
                if kind == "exact":
                    entity_type = match.get("_ENTITY_TYPE", "lead")
                    for ref in [match, *match.get("_RELATED_DEALS", [])]:
                        bitrix.call("crm.timeline.comment.add", {"fields": {"ENTITY_ID": int(ref["ID"]), "ENTITY_TYPE": ref.get("_ENTITY_TYPE", entity_type), "COMMENT": timeline_update(company)}})
                    db.execute("INSERT INTO sync_log(company_key,bitrix_id,action,error) VALUES(?,?,?,NULL)", (row["key"], str(match["ID"]), "duplicate"))
                else:
                    data = lead_fields(company, source_id)
                    if kind == "partial":
                        data["UF_CRM_DUPLICATE_REVIEW"] = "Y"
                        data["COMMENTS"] += f"\n\nВозможный дубль: {match.get('TITLE')} (Bitrix ID {match.get('ID')})"
                    lead_id = bitrix.call("crm.lead.add", {"fields": data})
                    # This portal converts incoming leads automatically; restore the requested NEW stage.
                    bitrix.call("crm.lead.update", {"id": lead_id, "fields": {"STATUS_ID": "NEW"}})
                    db.execute("INSERT INTO sync_log(company_key,bitrix_id,action,error) VALUES(?,?,?,NULL)", (row["key"], str(lead_id), "partial" if kind == "partial" else "created"))
                db.commit()
            except Exception as exc:
                log.exception("Сбой передачи %s", row["key"])
                db.execute("INSERT INTO sync_log(company_key,action,error) VALUES(?,?,?)", (row["key"], "error", str(exc)))
                db.commit()
                print(f"Ошибка {row['key']}: {exc}")
        if args.preview_file and not args.apply:
            preview_path = Path(args.preview_file)
            preview_path.write_text(json.dumps(previews, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Предпросмотр сохранён: {preview_path}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
