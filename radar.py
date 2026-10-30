import csv
import json
import logging
import random
import re
import sqlite3
import sys
import time
import os
from collections import Counter
from datetime import datetime
from urllib.parse import quote_plus, urljoin
from pathlib import Path

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from gemini_reviewer import GeminiReviewer

load_dotenv(Path(__file__).with_name(".env"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="backslashreplace")


CONFIG_PATH = Path(__file__).with_name("criteria.json")
DEFAULT_CRITERIA = {
    "keywords": ["реконструкция", "капитальный ремонт", "благоустройство", "строительство", "текущий ремонт"],
    "business_focus": ["новое строительство", "реконструкция", "ремонт", "благоустройство", "дороги", "инженерные сети"],
    "keyword_weights": {"реконструкц": 3, "капитальн": 3, "капремонт": 3, "благоустро": 3, "строительств": 2, "ремонт": 2},
    "minimum_score": 2, "exclude": [], "amount_bonus": [[50000000, 2], [10000000, 1]],
    "customer_bonus": {"квазигос": 2, "госорган": 1, "коммерческая": 0}, "customer_types": [],
    "regions": [], "high_score": 7, "mid_score": 4, "expired_policy": "mark",
    "expired_penalty": 3, "auto_bin_lookup": False, "fetch_contacts": True, "required_contacts": [],
    "max_leads_per_run": 50, "gemini_enabled": True, "gemini_model": "gemini-3.8-flash", "gemini_batch_size": 40,
}


def load_criteria() -> dict:
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open(encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError("criteria.json должен содержать JSON-объект")
        return {**DEFAULT_CRITERIA, **loaded}
    return DEFAULT_CRITERIA.copy()


CRITERIA = load_criteria()
KEYWORDS = CRITERIA["keywords"]
KEYWORD_WEIGHTS = CRITERIA["keyword_weights"]
MIN_SCORE = int(CRITERIA["minimum_score"])
EXCLUDE = CRITERIA["exclude"]
AMOUNT_BONUS = [(float(x[0]), int(x[1])) for x in CRITERIA["amount_bonus"]]
CUSTOMER_BONUS = CRITERIA["customer_bonus"]
HIGH_SCORE, MID_SCORE = int(CRITERIA["high_score"]), int(CRITERIA["mid_score"])
EXPIRED_POLICY = CRITERIA["expired_policy"]
EXPIRED_PENALTY = int(CRITERIA["expired_penalty"])
AUTO_BIN_LOOKUP = bool(CRITERIA["auto_bin_lookup"])
CUSTOMER_TYPES = CRITERIA["customer_types"]
REGIONS = CRITERIA["regions"]
FETCH_DETAIL = bool(CRITERIA["fetch_contacts"])
MAX_LEADS_PER_RUN = int(CRITERIA["max_leads_per_run"])

BIN_SOURCES = [
    {"name": "statsnet", "url": "https://statsnet.co/search/kz/{q}", "use_browser": True},
    {"name": "duckduckgo", "url": "https://html.duckduckgo.com/html/?q={q}+БИН", "use_browser": False},
]
SAVE_CSV = False
MAX_PAGES = 5
DELAY = (2.0, 4.5)
DB_PATH = "leads.db"


SOURCES = {
    "bicotender": {
        "enabled": True,
        "parser": "table",


        "first_page_url": "https://www.bicotender.ru/tags/goszakupki-respubliki-kazahstan.html",
        "search_url": "https://www.bicotender.ru/tags/goszakupki-respubliki-kazahstan/page/{page}/",
        "per_keyword": False,
        "max_pages": 10,
        "use_browser": False,
        "table_sel": "table",
        "columns": {
            "description": ["название"],
            "amount":      ["цена"],
            "deadline":    ["период"],
            "region":      ["регион"],
        },
        "link_col": "description",
        "customer_from_meta": True,
        "require_text": "казахстан",
    },
    "mp_kz": {
        "enabled": True,
        "parser": "anchors_mp",
        "first_page_url": "https://mp.kz/tenders/?category_id=15&sort=2&page=1",
        "search_url": "https://mp.kz/tenders/?category_id=15&sort=2&page={page}",
        "per_keyword": False,
        "max_pages": 10,
        "use_browser": False,
    },
    "goszakup": {
        "enabled": False,
        "parser": "table",
        "search_url": "https://goszakup.gov.kz/ru/search/announce?filter[name]={q}&count_record=50&page={page}",
        "use_browser": True,
        "table_sel": "table",
        "columns": {
            "company":     ["организатор", "заказчик"],
            "description": ["наименование", "предмет"],
            "amount":      ["сумма"],
            "deadline":    ["окончан", "срок"],
        },
        "link_col": "description",
        "company_link_col": "company",
    },
}


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(Path(__file__).with_name("radar.log"), encoding="utf-8"), logging.StreamHandler()])
log = logging.getLogger("parser")

USAGE = """python kz_lead_parser.py run [источник]
python kz_lead_parser.py bins <leads.json>
python kz_lead_parser.py probe <URL> [js]"""

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9",
}
RE_BIN = re.compile(r"(?:БИН|BIN|ИИН)\D{0,12}(\d{12})", re.I)


def valid_bin(b: str) -> bool:
    if not re.fullmatch(r"\d{12}", b or ""):
        return False
    d = [int(x) for x in b]
    r = sum(d[i] * (i + 1) for i in range(11)) % 11
    if r == 10:
        r = sum(d[i] * w for i, w in enumerate([3, 4, 5, 6, 7, 8, 9, 10, 11, 1, 2])) % 11
    return r != 10 and r == d[11]


RE_PHONE = re.compile(r"(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
RE_SITE = re.compile(r"https?://[\w.-]+\.[a-z]{2,}[^\s\"'<>]*", re.I)

OWN_PHONES = ("7273309977",)
OWN_EMAIL_DOMAINS = ("@mp.kz", "@bicotender.ru", "@enable-javascript.com", "@googletagmanager.com", "@google-analytics.com")
BAD_SITE_PARTS = ("w3.org", "mp.kz", "bicotender", "goszakup", "zakup.sk", "egov.kz", "gov.kz",
                  "facebook.", "instagram.", "twitter.", "whatsapp.", "t.me", "youtube.", "vk.com",
                  "google.", "googletagmanager.com", "google-analytics.com", "googlesyndication.com",
                  "doubleclick.net", "googletagservices.com", "enable-javascript.com", "gtag/js", "2gis.", "yandex.", "schema.org", "apple.com")


COMPANY_CACHE: dict = {}


class Fetcher:
    def __init__(self):
        self.client = httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True)
        self._pw = self._browser = None

    def get(self, url: str, use_browser: bool = False) -> str:
        last = None
        for attempt in range(3):
            try:
                time.sleep(random.uniform(*DELAY))
                if use_browser:
                    return self._get_browser(url)
                r = self.client.get(url)
                r.raise_for_status()
                return r.text
            except Exception as e:
                last = e
                log.warning("попытка %s: %s -> %s", attempt + 1, url, e)
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"не удалось загрузить {url}: {last}")

    def _get_browser(self, url: str) -> str:
        if not self._browser:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        page = self._browser.new_page(extra_http_headers=HEADERS)
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)
        html = page.content()
        page.close()
        return html

    def close(self):
        self.client.close()
        if self._browser:
            self._browser.close()
            self._pw.stop()


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def map_columns(header_cells: list, spec: dict) -> dict:
    heads = [h.lower() for h in header_cells]
    idx = {}
    for field, keys in spec.items():
        for i, h in enumerate(heads):
            if any(k in h for k in keys):
                idx[field] = i
                break
    return idx


def parse_tables(html: str, src: dict, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    rows_out = []
    for table in soup.select(src["table_sel"]):
        trs = table.find_all("tr")
        if len(trs) < 2:
            continue
        head_tr = next((tr for tr in trs if tr.find("th")), trs[0])
        heads = [_text(c) for c in head_tr.find_all(["th", "td"])]
        idx = map_columns(heads, src["columns"])
        if "company" not in idx and "description" not in idx:
            continue
        for tr in trs:
            if tr is head_tr:
                continue
            tds = tr.find_all("td")
            if len(tds) < len(heads) - 1:
                continue
            cells = [_text(td) for td in tds]
            row = {f: (cells[i] if i < len(cells) else "") for f, i in idx.items()}
            row["_text"] = " ".join(cells)

            def link(col_field):
                i = idx.get(src.get(col_field))
                if i is None or i >= len(tds):
                    return ""
                a = tds[i].find("a", href=True)
                return urljoin(page_url, a["href"]) if a else ""

            row["tender_url"] = link("link_col") or page_url
            row["company_url"] = link("company_link_col")
            rows_out.append(row)
    return rows_out


RE_AMOUNT = re.compile(r"(?<![\d:])(\d{1,3}(?:\s\d{3})*,\d{2})\s*тг")
RE_DATE = re.compile(r"\d{2}\.\d{2}\.\d{4}")


def parse_anchors_mp(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    rows, pending, current, multi_next, multi_url = [], None, None, False, ""
    for a in soup.find_all("a", href=True):
        href, txt, full = a["href"], _text(a), urljoin(page_url, a["href"])
        if "/company/" in href:
            sib = a.next_sibling
            region = re.split(r"\n", sib.strip(" ,\n"))[0].strip(" ,") if isinstance(sib, str) else ""
            seg = []
            for el in a.next_elements:
                if getattr(el, "name", None) == "a" and "/company/" in (el.get("href") or "") and el is not a:
                    break
                if isinstance(el, str):
                    seg.append(el)
                if len(seg) > 80:
                    break
            blob = " ".join(seg)
            am = RE_AMOUNT.search(blob)
            dm = RE_DATE.search(blob)
            current = {
                "company": txt, "company_url": full, "region": region,
                "description": pending[0] if pending else "",
                "tender_url": pending[1] if pending else (multi_url or page_url),
                "amount": (am.group(1).strip() + " тг") if am else "",
                "deadline": dm.group() if dm else "",
                "_multi": multi_next, "_lots": 1 if pending else 0,
            }
            rows.append(current)
            pending, multi_next, multi_url = None, False, ""
        elif re.search(r"/tender/\d+", href):
            if current and current["_multi"]:
                if current["_lots"] < 5:
                    current["description"] = (current["description"] + " | " + txt).strip(" |")
                current["_lots"] += 1
            else:
                pending = (txt, full)
        elif re.search(r"/tenders/t\d+", href):
            if txt.startswith("Тендер №") or "лотов" in txt:
                multi_next, pending, multi_url = True, None, full
            elif current:
                current["_multi"] = False
    for r in rows:
        r["_text"] = " ".join([r["company"], r["description"], r["region"]])
    return rows


def parse_rows(html: str, src: dict, url: str) -> list[dict]:
    if src.get("parser") == "anchors_mp":
        return parse_anchors_mp(html, url)
    return parse_tables(html, src, url)


RE_META_CUSTOMER = re.compile(r"(?:KZT|тенге|тг|руб\w*)\s+от\s+(.{3,250})$", re.I)
RE_META_LAST_OT = re.compile(r".*\sот\s+(.{3,250})$")


def get_customer_meta(fetcher: "Fetcher", url: str, src: dict) -> str:
    try:
        soup = BeautifulSoup(fetcher.get(url, src.get("use_browser", False)), "lxml")
    except Exception as e:
        log.error("карточка тендера %s: %s", url, e)
        return ""
    m = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    txt = (m.get("content", "") if m else "").strip()
    r = RE_META_CUSTOMER.search(txt) or RE_META_LAST_OT.match(txt)
    return r.group(1).strip(" .") if r else ""


RE_DMY = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")


def parse_latest_date(text: str):
    dates = []
    for d, m_, y in RE_DMY.findall(text or ""):
        try:
            dates.append(datetime(int(y), int(m_), int(d)).date())
        except ValueError:
            pass
    return max(dates) if dates else None


def parse_amount(text: str) -> float:
    m = re.search(r"\d[\d\s.,]*", text or "")
    if not m:
        return 0.0
    try:
        value = re.sub(r"\s", "", m.group()).rstrip(".,")
        if "." in value and "," in value:
            decimal = "." if value.rfind(".") > value.rfind(",") else ","
            value = re.sub(r"[.,]", "", value.replace(decimal, "§")).replace("§", ".")
        elif "," in value or "." in value:
            sep = "," if "," in value else "."
            tail = value.rsplit(sep, 1)[1]
            value = value.replace(sep, "." if len(tail) in (1, 2) else "")
        return float(value)
    except ValueError:
        return 0.0


def clean_deadline(text: str) -> str:
    dates = RE_DMY.findall(text or "")
    if not dates:
        return (text or "").strip()
    # Tender windows often contain a duration and both endpoints; the final date is the closing date.
    d, m, y = dates[-1]
    return f"{d}.{m}.{y}"


def evaluate(row: dict, src: dict | None = None) -> dict:
    src = src or {}
    desc = (row.get("description") or row.get("_text") or "").lower()
    all_text = (desc + " " + row.get("company", "") + " " + row.get("_text", "")).lower()
    res = {"ok": False, "score": 0, "reasons": [], "reject": "", "expired": False}

    if src.get("require_text") and src["require_text"] not in (row.get("region", "") + " " + all_text).lower():
        res["reject"] = f'не подходит страна/регион: нет «{src["require_text"]}»'
        return res
    for w in EXCLUDE:
        if w in all_text:
            res["reject"] = f"исключающее слово: {w.strip()}"
            return res
    reg = row.get("region", "").lower()
    if REGIONS and reg and not any(r.lower() in reg for r in REGIONS):
        res["reject"] = f'регион вне списка: {row.get("region", "")[:40]}'
        return res

    deadline = parse_latest_date(row.get("deadline", ""))
    if EXPIRED_POLICY != "keep" and deadline and deadline < datetime.now().date():
        res["expired"] = True
        if EXPIRED_POLICY == "drop":
            res["reject"] = f"тендер просрочен: {deadline.isoformat()}"
            return res

    score = 2 if src.get("trust_category") else 0
    if score:
        res["reasons"].append("+2 строительная категория источника")
    for w, pts in KEYWORD_WEIGHTS.items():
        if w in desc:
            score += pts
            res["reasons"].append(f"{pts:+d} {w}")
    amount = parse_amount(row.get("amount", ""))
    for threshold, pts in AMOUNT_BONUS:
        if amount >= threshold:
            score += pts
            res["reasons"].append(f"+{pts} сумма от {threshold:,}".replace(",", " "))
            break
    if score < MIN_SCORE:
        res["score"] = score
        res["reject"] = f"низкий балл: {score} (нужно от {MIN_SCORE})"
        return res
    if res["expired"]:
        score -= EXPIRED_PENALTY
        res["reasons"].append(f"-{EXPIRED_PENALTY} тендер просрочен")
    res.update(ok=True, score=score)
    return res


def matches_criteria(row: dict, src: dict | None = None) -> bool:
    return evaluate(row, src)["ok"]


def detect_region(text: str) -> str:
    for r in REGIONS:
        if r.lower() in text.lower():
            return r
    return ""


def clean_phone(text: str) -> str:
    for m in RE_PHONE.finditer(text or ""):
        num = re.sub(r"[^\d+]", "", m.group())
        if not any(num.endswith(o) for o in OWN_PHONES):
            return num
    return ""


def pick_email(text: str) -> str:
    for m in RE_EMAIL.finditer(text or ""):
        if not m.group().lower().endswith(OWN_EMAIL_DOMAINS):
            return m.group()
    return ""


def pick_site(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    candidates = [a.get("href", "") for a in soup.find_all("a", href=True)]
    candidates.extend(m.group() for m in RE_SITE.finditer(html or ""))
    for candidate in candidates:
        u = candidate.strip().lower()
        if u.startswith("//"):
            u = "https:" + u
        if not u.startswith(("http://", "https://")) or any(b in u for b in BAD_SITE_PARTS):
            continue
        host = re.sub(r"^https?://", "", u).split("/", 1)[0]
        if "." in host and not host.endswith((".js", ".css", ".png", ".jpg")):
            return u.rstrip(".,;)")
    return ""


def infer_activity(description: str, customer_type: str = "") -> str:
    text = (description or "").lower()
    groups = [
        ("Строительство", ("строительств", "строительно-монтаж", "смр")),
        ("Ремонт и реконструкция", ("ремонт", "реконструкц", "капремонт")),
        ("Благоустройство и дороги", ("благоустро", "асфальт", "дорог", "тротуар")),
        ("Инженерные сети", ("электроснабж", "водоснабж", "теплоснабж", "газопровод", "канализац")),
        ("Проектирование", ("проектирован", "проектно-сметн", "изыскани")),
    ]
    for label, keys in groups:
        if any(k in text for k in keys):
            return label
    return "Государственный заказчик" if customer_type == "госорган" else "Квазигосударственный заказчик" if customer_type == "квазигос" else "Строительные и подрядные работы"


def norm_name(s: str) -> str:
    s = re.sub(r"[«»\"'.,()]", " ", s.lower())
    s = re.sub(r"\b(тоо|ао|ип|ооо|тов|llp|jsc|гку|ку|кгп|рггп|гу|рго)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def classify_customer(name: str) -> str:
    n = name.lower()
    gov = r"(?<!\w)(гу|кгу|ргу|рго|гкп|гккп|кгкп|кгпнпхв|ргп|гкоп)(?!\w)|акимат|аппарат акима|управлени|отдел |департамент|комитет|министерств|государственн|администрация|мектеп|школ|поликлиник"
    quasi = r"самрук|байтерек|казмунай|казатомпром|казпочта|қазақстан темір|теміржол|темир|қамқор|кегок|казахстан\s+темир|некоммерческое ао|университет|нац(иональн)?\w* (компания|холдинг|управляющ)|(?<!\w)нк(?!\w)"
    if re.search(gov, n):
        return "госорган"
    if re.search(quasi, n):
        return "квазигос"
    return "коммерческая"


def clean_region(raw: str) -> tuple[str, str]:
    parts = [x.strip() for x in re.sub(r"^\s*Закупки\s*", "", raw or "").split("/") if x.strip()]
    parts = [x for x in parts if x.lower() != "казахстан"]
    parts = [re.sub(r"^(г\.|город)\s*", "", x, flags=re.I) for x in parts]
    if not parts:
        return "", ""
    return parts[0], (parts[-1] if len(parts) > 1 else "")


def fix_name(name: str) -> str:
    return re.sub(r"(?<=[А-ЯЁ])(ИИ|ОИ|ЫИ|ЕИ|АИ)(?!\w)", lambda m: m.group()[0] + "Й", name) if name.isupper() else name


def clean_desc(text: str) -> str:
    return re.sub(r"^\s*Тендер\s*№\s*\d+\s*", "", text or "").strip()


def make_company(row: dict, source: str, keyword: str, fetcher: Fetcher, src: dict) -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    text = row["_text"]
    c = {
        "name": fix_name(row.get("company", "").strip()),
        "bin": next((m for m in RE_BIN.findall(text) if valid_bin(m)), ""),
        "region": clean_region(row.get("region", ""))[0] or detect_region(text),
        "city": clean_region(row.get("region", ""))[1],
        "customer_type": classify_customer(row.get("company", "")),
        "activity": row.get("_ai_activity") or infer_activity(row.get("description", ""), classify_customer(row.get("company", ""))),
        "site": "", "phone": "", "email": "",
        "contact_search_links": ["https://2gis.kz/search/" + quote_plus(row.get("company", "").strip())] if row.get("company") else [],
        "company_url": row.get("company_url", ""),
        "source": source, "keyword": keyword, "found_at": now,
        "tenders": [{
            "description": clean_desc(row.get("description", "")),
            "amount": row.get("amount", ""),
            "deadline": clean_deadline(row.get("deadline", "")),
            "url": row.get("tender_url", ""),
            "found_at": now,
        }],
    }
    if FETCH_DETAIL and c["company_url"]:
        cu = c["company_url"]
        if cu not in COMPANY_CACHE:
            info = {"bin": "", "phone": "", "email": "", "site": ""}
            try:
                dsoup = BeautifulSoup(fetcher.get(cu, src.get("use_browser", False)), "lxml")
                dtext = dsoup.get_text(" ", strip=True)
                info["bin"] = next((m for m in RE_BIN.findall(dtext) if valid_bin(m)), "")
                info["phone"] = clean_phone(dtext)
                info["email"] = pick_email(dtext)
                info["site"] = pick_site(dsoup.decode())
            except Exception as e:
                log.error("страница заказчика %s: %s", cu, e)
            COMPANY_CACHE[cu] = info
        info = COMPANY_CACHE[cu]
        c["bin"] = c["bin"] or info["bin"]
        c["phone"], c["email"], c["site"] = info["phone"], info["email"], info["site"]
        c["contact_search_links"].insert(0, info["site"] or "")
        c["contact_search_links"] = [u for u in c["contact_search_links"] if u]
    if FETCH_DETAIL and c["site"] and (not c["phone"] or not c["email"]):
        try:
            site_html = fetcher.get(c["site"])
            site_text = BeautifulSoup(site_html, "lxml").get_text(" ", strip=True)
            c["phone"] = c["phone"] or clean_phone(site_text)
            c["email"] = c["email"] or pick_email(site_text)
        except Exception as e:
            log.info("контакты сайта %s недоступны: %s", c["site"], e)
    return c


def dedupe_key(c: dict) -> str:
    return f"bin:{c['bin']}" if c.get("bin") else f"name:{norm_name(c['name'])}"


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS companies(
            key TEXT PRIMARY KEY, data TEXT, first_seen TEXT, last_seen TEXT)""")
        self.db.execute("CREATE TABLE IF NOT EXISTS seen(url TEXT PRIMARY KEY)")
        self.db.execute("CREATE TABLE IF NOT EXISTS tenders (url TEXT PRIMARY KEY, company_key TEXT NOT NULL, description TEXT, amount TEXT, deadline TEXT, data TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS sync_log (id INTEGER PRIMARY KEY, company_key TEXT NOT NULL, bitrix_id TEXT, action TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, error TEXT)")

    def is_seen(self, url: str) -> bool:
        return bool(url) and self.db.execute("SELECT 1 FROM seen WHERE url=?", (url,)).fetchone() is not None

    def mark_seen(self, url: str):
        if url:
            self.db.execute("INSERT OR IGNORE INTO seen VALUES(?)", (url,))
            self.db.commit()

    def upsert(self, c: dict) -> str:
        key = dedupe_key(c)
        row = self.db.execute("SELECT data FROM companies WHERE key=?", (key,)).fetchone()
        if not row:
            self.db.execute("INSERT INTO companies VALUES(?,?,?,?)",
                            (key, json.dumps(c, ensure_ascii=False), c["found_at"], c["found_at"]))
            self.db.commit()
            for tender in c.get("tenders", []):
                self._save_tender(key, tender)
            return "new"
        old = json.loads(row[0])
        changed = False
        for k in ("bin", "region", "city", "customer_type", "activity", "site", "phone", "email", "company_url"):
            if k == "site" and old.get(k):
                cleaned = pick_site(old[k])
                if cleaned != old[k]:
                    old[k] = cleaned
                    changed = True
            if c.get(k) and not old.get(k):
                old[k] = c[k]
                changed = True
            elif not c.get(k) and old.get(k):
                c[k] = old[k]
        known_urls = {t["url"] for t in old["tenders"]}
        for t in c["tenders"]:
            if t["url"] not in known_urls or not t["url"]:
                old["tenders"].append(t)
                changed = True
            self._save_tender(key, t)
        self.db.execute("UPDATE companies SET data=?, last_seen=? WHERE key=?",
                        (json.dumps(old, ensure_ascii=False), c["found_at"], key))
        self.db.commit()
        return "updated" if changed else "known"

    def _save_tender(self, key: str, tender: dict):
        url = tender.get("url")
        if not url:
            return
        self.db.execute("INSERT OR IGNORE INTO tenders(url,company_key,description,amount,deadline,data) VALUES(?,?,?,?,?,?)",
                        (url, key, tender.get("description", ""), tender.get("amount", ""), tender.get("deadline", ""), json.dumps(tender, ensure_ascii=False)))
        self.db.commit()


def aggregate_by_company(rows: list[dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.get(r["_key"])
        if not a:
            a = agg[r["_key"]] = {
                "status": r["status"], "name": r["name"], "bin": r["bin"],
                "customer_type": r["customer_type"], "activity": r.get("activity", ""), "region": r["region"], "city": r["city"],
                "site": r["site"], "phone": r["phone"], "email": r["email"], "source": r["source"],
                "tenders_count": 0, "tenders": [], "found_at": r["found_at"],
                "score": 0, "priority": "",
            }
        a["tenders_count"] += 1
        if r["status"] == "new":
            a["status"] = "new"
        a["tenders"].append({"description": r["object"], "amount": r["amount"], "deadline": r["deadline"],
                             "url": r["source_url"], "score": r.get("score", 0),
                             "expired": r.get("expired", False), "why": r.get("why", [])})
        a["region"] = a["region"] or r["region"]
        a["city"] = a["city"] or r["city"]
    for a in agg.values():
        a["tenders"].sort(key=lambda t: -t["score"])
        a["tenders"] = a["tenders"][:20]
        live = [t["score"] for t in a["tenders"] if not t["expired"]] or [t["score"] for t in a["tenders"]]
        a["score"] = max(live) + min(a["tenders_count"] - 1, 2)
        a["priority"] = "высокий" if a["score"] >= HIGH_SCORE else "средний" if a["score"] >= MID_SCORE else "низкий"
    for a in agg.values():
        a["summary"] = "; ".join(
            f'{t["description"][:140]}' + (f' ({t["amount"]})' if t["amount"] else "") for t in a["tenders"][:5])
    return sorted(agg.values(), key=lambda a: -a["score"])


def write_outputs(leads: list[dict], no_customer: list[dict], stats: dict, stamp: str, rejected: list[dict] | None = None):
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stats": stats,
        "leads": leads,
        "tenders_without_customer": no_customer,
    }
    with open(f"leads_{stamp}.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    if rejected:
        with open(f"rejected_{stamp}.json", "w", encoding="utf-8") as f:
            json.dump(rejected, f, ensure_ascii=False, indent=2)
    if SAVE_CSV and leads:
        flat = [{**{k: v for k, v in l.items() if k != "tenders"},
                 "tenders": " || ".join(f'{t["description"][:120]} {t["amount"]} {t["url"]}'.strip() for t in l["tenders"][:5])}
                for l in leads]
        with open(f"leads_{stamp}.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(flat[0].keys()))
            w.writeheader()
            w.writerows(flat)


def run(only: str | None = None):
    fetcher, store = Fetcher(), Store(DB_PATH)
    gemini = GeminiReviewer(model=CRITERIA.get("gemini_model", "gemini-3.8-flash")) if CRITERIA.get("gemini_enabled", True) else None
    ai_mode = "gemini" if gemini and gemini.enabled else "rules_no_key" if CRITERIA.get("gemini_enabled", True) else "rules_disabled"
    gemini_available = bool(gemini and gemini.enabled)
    stats = {"rows_found": 0, "matched": 0, "new": 0, "updated": 0, "known": 0, "no_customer": 0, "errors": 0,
             "ai_mode": ai_mode, "ai_reviewed": 0, "ai_promoted": 0, "ai_rejected": 0, "ai_errors": 0, "ai_unreviewed": 0}
    if ai_mode == "rules_no_key":
        log.warning("Gemini отключён до появления GEMINI_API_KEY; применяется фильтр по правилам")
    out_rows, no_customer, seen_now = [], [], set()
    rejected, rejected_seen = [], set()

    def reject(row: dict, source: str, reason: str, score: int = 0):
        key = (row.get("tender_url", ""), reason)
        if key in rejected_seen or len(rejected) >= 3000:
            return
        rejected_seen.add(key)
        rejected.append({"source": source, "reason": reason, "score": score,
                         "object": (row.get("description") or row.get("_text", ""))[:200],
                         "company": row.get("company", ""), "amount": row.get("amount", ""),
                         "region": row.get("region", ""), "deadline": row.get("deadline", ""),
                         "url": row.get("tender_url", "")})

    for name, src in SOURCES.items():
        if only and name != only:
            continue
        if not only and not src.get("enabled", True):
            continue
        keywords = KEYWORDS if src.get("per_keyword", True) else [""]
        for kw in keywords:
            for page in range(1, src.get("max_pages", MAX_PAGES) + 1):
                if page == 1 and src.get("first_page_url"):
                    url = src["first_page_url"]
                else:
                    url = src["search_url"].format(q=quote_plus(kw), page=page)
                try:
                    html = fetcher.get(url, src.get("use_browser", False))
                except Exception as e:
                    log.error("%s", e)
                    stats["errors"] += 1
                    break
                rows = parse_rows(html, src, url)
                log.info("%s | %s | стр.%s | строк: %s", name, kw or "-", page, len(rows))
                if not rows:
                    break
                ai_results = {}
                if gemini_available:
                    ai_candidate_indices = [idx for idx, r in enumerate(rows)
                                            if (lambda ev: ev["ok"] or ev["reject"].startswith("низкий балл"))(evaluate(r, src))]
                    ai_candidates = [rows[idx] for idx in ai_candidate_indices]
                    batch_size = max(1, int(CRITERIA.get("gemini_batch_size", 20)))
                    for start in range(0, len(ai_candidates), batch_size):
                        batch = ai_candidates[start:start + batch_size]
                        try:
                            result_map = gemini.analyze_batch(batch, CRITERIA)
                            ai_results.update({ai_candidate_indices[start + local]: result for local, result in result_map.items()})
                            stats["ai_reviewed"] += len(batch)
                        except Exception as e:
                            log.warning("Gemini-анализ не выполнен (%s, стр.%s): %s; для страницы оставлены правила", name, page, e)
                            stats["ai_errors"] += 1
                            stats["ai_unreviewed"] += len(ai_candidates) - start
                            gemini_available = False
                            stats["ai_mode"] = "rules_gemini_unavailable"
                            break
                else:
                    ai_results = {}
                for row_index, row in enumerate(rows):
                    stats["rows_found"] += 1
                    try:
                        ev = evaluate(row, src)
                        ai_result = ai_results.get(row_index)
                        if ai_result:
                            if not ai_result["relevant"]:
                                stats["ai_rejected"] += 1
                                reject(row, name, "Gemini: " + (ai_result["reason"] or "не соответствует критериям"), ev["score"])
                                continue
                            if not ev["ok"] and ev["reject"].startswith("низкий балл"):
                                stats["ai_promoted"] += 1
                                ev.update(ok=True, score=MIN_SCORE, reject="")
                                ev["reasons"].append("+Gemini подтвердил соответствие критериям")
                            if ai_result.get("reason"):
                                ev["reasons"].append("Gemini: " + ai_result["reason"])
                            row["_ai_activity"] = ai_result.get("activity", "")
                        if not ev["ok"]:
                            reject(row, name, ev["reject"], ev["score"])
                            continue
                        stats["matched"] += 1
                        turl = row.get("tender_url", "")
                        if turl and turl != url and store.is_seen(turl):
                            stats["known"] += 1
                            continue
                        if not row.get("company") and src.get("customer_from_meta") and turl:
                            row["company"] = get_customer_meta(fetcher, turl, src)
                        if not row.get("company"):
                            stats["no_customer"] += 1
                            no_customer.append({"source": name, "object": row.get("description", ""),
                                                "amount": row.get("amount", ""), "region": row.get("region", ""),
                                                "source_url": turl})
                            continue
                        c = make_company(row, name, kw, fetcher, src)
                        missing_contacts = [field for field in CRITERIA.get("required_contacts", []) if not c.get(field)]
                        if missing_contacts:
                            stats["skipped_missing_contacts"] = stats.get("skipped_missing_contacts", 0) + 1
                            reject(row, name, "нет обязательного контакта: " + ", ".join(missing_contacts), ev["score"])
                            continue
                        if CUSTOMER_TYPES and c["customer_type"] not in CUSTOMER_TYPES:
                            stats["skipped_by_type"] = stats.get("skipped_by_type", 0) + 1
                            reject(row, name, f'тип заказчика не выбран: {c["customer_type"]}', ev["score"])
                            continue
                        bonus = CUSTOMER_BONUS.get(c["customer_type"], 0)
                        t0 = c["tenders"][0]
                        t0["score"] = ev["score"] + bonus
                        t0["expired"] = ev["expired"]
                        t0["why"] = ev["reasons"] + ([f"+{bonus} тип заказчика: {c['customer_type']}"] if bonus else [])
                        sig = (dedupe_key(c), c["tenders"][0]["url"])
                        if sig in seen_now:
                            continue
                        seen_now.add(sig)
                        status = store.upsert(c)
                        store.mark_seen(turl)
                        stats[status] += 1
                        if status in ("new", "updated"):
                            out_rows.append({
                                "status": status, "name": c["name"], "bin": c["bin"],
                                "region": c["region"], "city": c["city"], "customer_type": c["customer_type"], "activity": c["activity"],
                                "site": c["site"], "phone": c["phone"], "email": c["email"], "source": name,
                                "object": c["tenders"][0]["description"],
                                "amount": c["tenders"][0]["amount"],
                                "deadline": c["tenders"][0]["deadline"],
                                "score": t0["score"], "expired": t0["expired"], "why": t0["why"],
                                "source_url": c["tenders"][0]["url"],
                                "found_at": c["found_at"], "_key": dedupe_key(c),
                            })
                    except Exception as e:
                        log.exception("ошибка строки: %s", e)
                        stats["errors"] += 1

    leads = aggregate_by_company(out_rows)
    if AUTO_BIN_LOOKUP:
        stats["bin_lookup"] = enrich_bins(leads, fetcher, store)
    fetcher.close()
    stamp = datetime.now().strftime("%Y-%m-%d")
    stats["companies_in_output"] = len(leads)
    if len(leads) > MAX_LEADS_PER_RUN:
        stats["limited_by_daily_cap"] = len(leads) - MAX_LEADS_PER_RUN
        leads = leads[:MAX_LEADS_PER_RUN]
    stats["companies_in_output"] = len(leads)
    stats["rejected_total"] = len(rejected)
    stats["rejected_by_reason"] = dict(Counter(r["reason"].split(":")[0] for r in rejected).most_common())
    write_outputs(leads, no_customer, stats, stamp, rejected)
    log.info("ИТОГ: %s", stats)


def _squash(t: str) -> str:
    return norm_name(t).replace("й", "и").replace("ё", "е")


def lookup_bin(name: str, fetcher: "Fetcher") -> tuple[str, str, list]:
    tokens = [t for t in _squash(name).split() if len(t) > 2][:8]
    if not tokens:
        return "", "not_found", []
    q = quote_plus(" ".join(name.split()[:10]))
    for src in BIN_SOURCES:
        try:
            html = fetcher.get(src["url"].format(q=q), src.get("use_browser", False))
        except Exception as e:
            log.warning("поиск БИН (%s): %s", src["name"], e)
            continue
        text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
        found: dict[str, float] = {}
        prev_end = 0
        for m in re.finditer(r"(?:БИН|BIN)\D{0,6}(\d{12})", text, re.I):
            b, start, prev_end = m.group(1), m.start(), prev_end
            span_from = max(prev_end, start - 200)
            prev_end = m.end()
            if not valid_bin(b):
                continue
            ctx = _squash(text[span_from:start])
            score = sum(1 for t in tokens if t in ctx) / len(tokens)
            if score >= 0.8:
                found[b] = max(found.get(b, 0), score)
        if len(found) == 1:
            return next(iter(found)), "found", [src["name"]]
        if len(found) > 1:
            return "", "ambiguous", sorted(found)
    return "", "not_found", []


def enrich_bins(leads: list[dict], fetcher: "Fetcher", store: "Store") -> dict:
    store.db.execute("CREATE TABLE IF NOT EXISTS bin_cache(key TEXT PRIMARY KEY, bin TEXT, status TEXT, cand TEXT)")
    res = {"found": 0, "ambiguous": 0, "not_found": 0}
    for l in leads:
        if l.get("bin"):
            l["bin_status"] = "from_source"
            continue
        key = norm_name(l["name"])
        row = store.db.execute("SELECT bin,status,cand FROM bin_cache WHERE key=?", (key,)).fetchone()
        if row and row[1] == "found":
            b, st, cand = row[0], row[1], json.loads(row[2])
        else:
            b, st, cand = lookup_bin(l["name"], fetcher)
            store.db.execute("INSERT OR REPLACE INTO bin_cache VALUES(?,?,?,?)", (key, b, st, json.dumps(cand)))
            store.db.commit()
        l["bin"] = b
        l["bin_status"] = st
        if st == "ambiguous":
            l["bin_candidates"] = cand
        if st != "found":
            l["bin_search_link"] = "https://www.google.com/search?q=" + quote_plus(f'{l["name"]} БИН')
        res[st] += 1
    return res


def cmd_bins(path: str):
    data = json.load(open(path, encoding="utf-8"))
    fetcher, store = Fetcher(), Store(DB_PATH)
    res = enrich_bins(data["leads"], fetcher, store)
    fetcher.close()
    data.setdefault("stats", {})["bin_lookup"] = res
    out = re.sub(r"\.json$", "", path) + "_bins.json"
    json.dump(data, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    log.info("БИН: %s -> %s", res, out)


def probe(url: str, use_browser: bool = False):
    fetcher = Fetcher()
    html = fetcher.get(url, use_browser)
    fetcher.close()
    fname = f"probe_{datetime.now().strftime('%H%M%S')}.html"
    open(fname, "w", encoding="utf-8").write(html)
    soup = BeautifulSoup(html, "lxml")
    print(f"HTML сохранён: {fname} ({len(html)} байт), title: {_text(soup.title)}")
    tables = soup.find_all("table")
    print(f"таблиц: {len(tables)}")
    for i, t in enumerate(tables):
        trs = t.find_all("tr")
        head = next((tr for tr in trs if tr.find("th")), trs[0] if trs else None)
        print(f"  [{i}] строк: {len(trs)} | заголовки: {[_text(c) for c in head.find_all(['th', 'td'])] if head else []}")
    cls = Counter()
    for el in soup.find_all(["div", "li", "article"], class_=True):
        cls[(el.name, " ".join(el["class"]))] += 1
    print("повторяющиеся блоки (кандидаты для CSS-режима):")
    for (tag, c), n in cls.most_common(8):
        if n >= 5:
            print(f"  {tag}.{c.replace(' ', '.')}  x{n}")
    if len(html) < 5000 or not tables:
        print("ВНИМАНИЕ: таблиц нет/страница короткая — вероятно, данные подгружает JS: попробуй `probe URL js`")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "run":
        run(args[1] if len(args) > 1 else None)
    elif args[0] == "bins" and len(args) > 1:
        cmd_bins(args[1])
    elif args[0] == "probe" and len(args) > 1:
        probe(args[1], len(args) > 2 and args[2] == "js")
    else:
        print(USAGE)
