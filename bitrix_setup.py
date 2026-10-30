"""Prepare Bitrix lead fields and the «ИИ-поиск» source. Writes require --apply."""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

load_dotenv(Path(__file__).with_name(".env"))
WEBHOOK = os.getenv("BITRIX_WEBHOOK_URL", "").rstrip("/") + "/"
FIELDS = [
    ("UF_CRM_BIN", "БИН", "string"),
    ("UF_CRM_ACTIVITY", "Сфера деятельности", "string"),
    ("UF_CRM_CUSTOMER_TYPE", "Тип заказчика", "string"),
    ("UF_CRM_SCORE", "Балл радара", "double"),
    ("UF_CRM_PRIORITY", "Приоритет радара", "string"),
    ("UF_CRM_DUPLICATE_REVIEW", "Требует проверки дубля", "boolean"),
    ("UF_CRM_TENDER_DEADLINE", "Срок подачи тендера", "string"),
    ("UF_CRM_TENDER_AMOUNT", "Сумма тендера", "double"),
]
COMPANY_FIELDS = [("UF_CRM_BIN", "БИН")]


def call(method, params=None):
    response = requests.post(WEBHOOK + method + ".json", json=params or {}, timeout=30)
    response.raise_for_status()
    body = response.json()
    if body.get("error"):
        raise RuntimeError(f"{body['error']}: {body.get('error_description', '')}")
    return body.get("result")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="создать отсутствующие поля и источник")
    args = parser.parse_args()
    if not WEBHOOK or WEBHOOK == "/":
        raise SystemExit("В .env не задан BITRIX_WEBHOOK_URL")
    if not args.apply:
        print("Dry-run: план настройки (для записи повторите с --apply)")
        for name, label, kind in FIELDS:
            print(f"Поле лида {name}: {label} ({kind})")
        for name, label in COMPANY_FIELDS:
            print(f"Поле компании {name}: {label} (string)")
        print("Источник лида: ИИ-поиск")
        return
    existing = call("crm.lead.userfield.list") or []
    existing_names = {item.get("FIELD_NAME") for item in existing}
    for name, label, kind in FIELDS:
        if name in existing_names:
            print(f"Уже существует: {name}")
            continue
        call("crm.lead.userfield.add", {"fields": {"FIELD_NAME": name, "EDIT_FORM_LABEL": {"ru": label}, "LIST_COLUMN_LABEL": {"ru": label}, "USER_TYPE_ID": kind, "XML_ID": "AI_RADAR_" + name}})
        print(f"Создано поле: {name}")
    company_fields = call("crm.company.userfield.list") or []
    company_names = {item.get("FIELD_NAME") for item in company_fields}
    for name, label in COMPANY_FIELDS:
        if name in company_names:
            print(f"Поле компании уже существует: {name}")
            continue
        call("crm.company.userfield.add", {"fields": {"FIELD_NAME": name, "EDIT_FORM_LABEL": {"ru": label}, "LIST_COLUMN_LABEL": {"ru": label}, "USER_TYPE_ID": "string", "XML_ID": "AI_RADAR_" + name}})
        print(f"Создано поле компании: {name}")
    statuses = call("crm.status.list", {"filter": {"ENTITY_ID": "SOURCE"}}) or []
    if not any(s.get("NAME", "").casefold() == "ии-поиск" for s in statuses):
        call("crm.status.add", {"fields": {"ENTITY_ID": "SOURCE", "STATUS_ID": "AI_SEARCH", "NAME": "ИИ-поиск", "SORT": 500}})
        print("Создан источник: ИИ-поиск")
    else:
        print("Источник уже существует: ИИ-поиск")


if __name__ == "__main__":
    main()
