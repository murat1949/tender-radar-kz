# -*- coding: utf-8 -*-
"""
MITWORK Collector v2
Production-oriented public collector for eep.mitwork.kz

Features:
- scans multiple public list pages
- fetches detail cards
- normalizes announcement + lot data
- computes procurement total as sum of lot totals
- optional keyword filter
- saves announcements and lots separately
- creates SEND_ME_MITWORK_V2.zip automatically

No login, password, EDS or token required.
"""

import csv, json, re, sys, time, zipfile
from pathlib import Path
from urllib.parse import urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: requests / beautifulsoup4 are not installed")
    sys.exit(2)

BASE = "https://eep.mitwork.kz"
LIST_URL = BASE + "/ru/publics/buys"
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)

# ---------------- SETTINGS ----------------
MAX_PAGES = 10              # up to 500 announcements
PER_PAGE = 50
DETAIL_DELAY = 0.12         # polite delay between detail requests

# Leave empty to collect ALL announcements.
# Example for cartridges:
# KEYWORDS = ["картридж", "тонер", "заправка картридж", "расходные материалы"]
KEYWORDS = []

# If True, filter by keywords after detail parsing.
USE_KEYWORD_FILTER = False
# ------------------------------------------

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
})

def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()

def get(url, params=None, timeout=35):
    return S.get(url, params=params, timeout=timeout, allow_redirects=True)

def money_to_float(s):
    s = clean(s).replace("\xa0", " ")
    m = re.search(r"(-?[\d\s]+(?:[.,]\d+)?)", s)
    if not m:
        return None
    try:
        return float(m.group(1).replace(" ", "").replace(",", "."))
    except:
        return None

def find_value_by_label(soup, label):
    q = label.lower()
    for tr in soup.find_all("tr"):
        cells = [clean(x.get_text(" ", strip=True)) for x in tr.find_all(["th","td"])]
        if len(cells) >= 2 and cells[0].lower().startswith(q):
            return cells[1]
    for dt in soup.find_all("dt"):
        if clean(dt.get_text(" ", strip=True)).lower().startswith(q):
            dd = dt.find_next_sibling("dd")
            if dd:
                return clean(dd.get_text(" ", strip=True))
    return ""

def parse_list_page(html):
    soup = BeautifulSoup(html, "html.parser")
    items, seen = [], set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"/ru/publics/buy/(\d+)", href)
        if not m:
            continue
        buy_id = m.group(1)
        if buy_id in seen:
            continue
        seen.add(buy_id)

        title = clean(a.get_text(" ", strip=True))
        tr = a.find_parent("tr")
        cells = []
        if tr:
            cells = [clean(td.get_text(" ", strip=True)) for td in tr.find_all(["td","th"])]

        number_text = cells[0] if cells else buy_id
        items.append({
            "buy_id": buy_id,
            "number_text": number_text,
            "title_hint": title,
            "url": urljoin(BASE, href)
        })
    return items

def extract_announcement_number(heading, seed):
    # Most reliable: heading/card text
    patterns = [
        r"Объявлени[ея]\s*№?\s*([0-9]+(?:-[0-9]+)?)",
        r"\b([0-9]{5,}-[0-9]+)\b",
    ]
    for p in patterns:
        m = re.search(p, heading, re.I)
        if m:
            return m.group(1)
    # Fallback only if the list text itself is number-like
    m = re.search(r"\b([0-9]{5,}(?:-[0-9]+)?)\b", seed.get("number_text",""))
    return m.group(1) if m else seed["buy_id"]

def parse_lots(soup):
    lots = []
    for tr in soup.find_all("tr"):
        cells = [clean(x.get_text(" ", strip=True)) for x in tr.find_all(["td","th"])]
        if len(cells) < 5:
            continue

        # Detect likely lot rows by numeric first cell or ZCP/AUK/etc-style lot number
        first = cells[0]
        looks_lot = first.isdigit() or bool(re.search(r"\d+[-–][А-ЯA-Z0-9]+", first))
        if not looks_lot:
            continue

        # Candidate money cells
        money_cells = []
        for c in cells:
            if "KZT" in c.upper():
                v = money_to_float(c)
                if v is not None:
                    money_cells.append((c, v))

        # Lot total is usually the last KZT value in MITWORK tables
        unit_price = money_cells[0][1] if money_cells else None
        total_price = money_cells[-1][1] if money_cells else None

        # Guess quantity: first numeric decimal before money columns
        quantity = None
        for c in cells[1:]:
            if "KZT" in c.upper():
                break
            cc = c.replace(" ", "").replace(",", ".")
            if re.fullmatch(r"\d+(?:\.\d+)?", cc):
                try:
                    quantity = float(cc)
                except:
                    pass

        lot = {
            "lot_number": first,
            "subject": cells[1] if len(cells) > 1 else "",
            "description": cells[2] if len(cells) > 2 else "",
            "quantity": quantity,
            "unit_price_kzt": unit_price,
            "total_price_kzt": total_price,
            "row_text": " | ".join(cells),
        }
        lots.append(lot)

    # Remove duplicate rows by lot_number + row_text
    unique, seen = [], set()
    for x in lots:
        k = (x["lot_number"], x["row_text"])
        if k not in seen:
            seen.add(k)
            unique.append(x)
    return unique

def parse_detail(html, url, seed):
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    heading = clean(h1.get_text(" ", strip=True)) if h1 else seed.get("title_hint","")

    ann_no = extract_announcement_number(heading, seed)

    title = heading
    if ":" in heading:
        title = clean(heading.split(":",1)[1])
    if not title:
        title = seed.get("title_hint","")

    organizer = find_value_by_label(soup, "Организатор")
    method = find_value_by_label(soup, "Способ закупки")
    purchase_type = find_value_by_label(soup, "Тип закупки")
    status = find_value_by_label(soup, "Статус")
    start_date = find_value_by_label(soup, "Дата начала приема заявок")
    end_date = find_value_by_label(soup, "Дата окончания приема заявок")
    rules = find_value_by_label(soup, "Правила закупок")

    lots = parse_lots(soup)

    totals = [x["total_price_kzt"] for x in lots if isinstance(x.get("total_price_kzt"), (int,float))]
    procurement_total = round(sum(totals), 2) if totals else None

    documents = []
    for a in soup.find_all("a", href=True):
        txt = clean(a.get_text(" ", strip=True))
        href = a["href"]
        low = href.lower()
        if any(ext in low for ext in (".pdf",".doc",".docx",".xls",".xlsx",".zip",".rar")):
            documents.append({
                "name": txt or href.split("/")[-1],
                "url": urljoin(BASE, href)
            })

    searchable = " ".join([
        title, organizer, method, purchase_type,
        " ".join((x.get("subject","") + " " + x.get("description","")) for x in lots)
    ]).lower()

    matched_keywords = [kw for kw in KEYWORDS if kw.lower() in searchable]
    relevant = (not USE_KEYWORD_FILTER) or bool(matched_keywords)

    return {
        "source": "MITWORK",
        "source_site": "eep.mitwork.kz",
        "external_id": seed["buy_id"],
        "announcement_number": ann_no,
        "title": title,
        "organizer": organizer,
        "procurement_method": method,
        "procurement_type": purchase_type,
        "status": status,
        "start_date": start_date,
        "end_date": end_date,
        "amount_kzt": procurement_total,
        "rules": rules,
        "public_url": url,
        "documents_count": len(documents),
        "lots_count": len(lots),
        "matched_keywords": matched_keywords,
        "relevant": relevant,
        "documents": documents,
        "lots": lots,
    }

def save_announcements_csv(rows, path):
    fields = [
        "source","external_id","announcement_number","title","organizer",
        "procurement_method","procurement_type","status","start_date","end_date",
        "amount_kzt","lots_count","documents_count","matched_keywords","public_url"
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            item = {k:r.get(k,"") for k in fields}
            item["matched_keywords"] = ", ".join(r.get("matched_keywords",[]))
            w.writerow(item)

def save_lots_csv(rows, path):
    fields = [
        "source","announcement_external_id","announcement_number","announcement_title",
        "lot_number","lot_subject","lot_description","quantity",
        "unit_price_kzt","total_price_kzt","organizer","status","end_date","public_url"
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            for lot in r.get("lots",[]):
                w.writerow({
                    "source": "MITWORK",
                    "announcement_external_id": r.get("external_id",""),
                    "announcement_number": r.get("announcement_number",""),
                    "announcement_title": r.get("title",""),
                    "lot_number": lot.get("lot_number",""),
                    "lot_subject": lot.get("subject",""),
                    "lot_description": lot.get("description",""),
                    "quantity": lot.get("quantity",""),
                    "unit_price_kzt": lot.get("unit_price_kzt",""),
                    "total_price_kzt": lot.get("total_price_kzt",""),
                    "organizer": r.get("organizer",""),
                    "status": r.get("status",""),
                    "end_date": r.get("end_date",""),
                    "public_url": r.get("public_url",""),
                })

print("MITWORK Collector v2")
print("=" * 60)

report = {
    "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "pages_scanned": 0,
    "list_announcements_found": 0,
    "detail_pages_ok": 0,
    "detail_errors": 0,
    "rows_saved": 0,
    "lots_saved": 0,
    "keyword_filter_enabled": USE_KEYWORD_FILTER,
    "keywords": KEYWORDS,
    "errors": [],
}

all_seeds = []
seed_ids = set()

for page in range(1, MAX_PAGES + 1):
    print("List page", page)
    try:
        r = get(LIST_URL, params={"page": page, "per-page": PER_PAGE})
        if r.status_code != 200:
            report["errors"].append("List page %d HTTP %s" % (page, r.status_code))
            break
        seeds = parse_list_page(r.text)
        report["pages_scanned"] += 1
        print("  found:", len(seeds))
        if not seeds:
            break

        new_count = 0
        for s in seeds:
            if s["buy_id"] not in seed_ids:
                seed_ids.add(s["buy_id"])
                all_seeds.append(s)
                new_count += 1

        if new_count == 0:
            break
        if len(seeds) < PER_PAGE:
            break
    except Exception as e:
        report["errors"].append("List page %d: %r" % (page, e))
        break

report["list_announcements_found"] = len(all_seeds)
print("Unique announcements:", len(all_seeds))

rows = []
for i, seed in enumerate(all_seeds, 1):
    print("Detail %d/%d: %s" % (i, len(all_seeds), seed["buy_id"]))
    try:
        r = get(seed["url"])
        if r.status_code != 200:
            report["detail_errors"] += 1
            report["errors"].append("%s HTTP %s" % (seed["url"], r.status_code))
            continue
        item = parse_detail(r.text, r.url, seed)
        report["detail_pages_ok"] += 1

        if item["relevant"]:
            rows.append(item)

        time.sleep(DETAIL_DELAY)
    except Exception as e:
        report["detail_errors"] += 1
        report["errors"].append("%s: %r" % (seed["url"], e))

# Sort newest IDs first
rows.sort(key=lambda x: int(x["external_id"]) if str(x["external_id"]).isdigit() else 0, reverse=True)

json_path = OUT / "mitwork_announcements.json"
csv_path = OUT / "mitwork_announcements.csv"
lots_path = OUT / "mitwork_lots.csv"
report_path = OUT / "mitwork_v2_report.json"
summary_path = OUT / "mitwork_v2_summary.txt"

json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
save_announcements_csv(rows, csv_path)
save_lots_csv(rows, lots_path)

lot_count = sum(len(x.get("lots",[])) for x in rows)
report["rows_saved"] = len(rows)
report["lots_saved"] = lot_count
report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

summary = [
    "MITWORK COLLECTOR V2",
    "=" * 70,
    "Pages scanned: %d" % report["pages_scanned"],
    "Unique announcements found: %d" % report["list_announcements_found"],
    "Detail pages parsed: %d" % report["detail_pages_ok"],
    "Detail errors: %d" % report["detail_errors"],
    "Announcements saved: %d" % report["rows_saved"],
    "Lots saved: %d" % report["lots_saved"],
    "Keyword filter: %s" % ("ON" if USE_KEYWORD_FILTER else "OFF"),
    "",
    "Files:",
    " mitwork_announcements.csv",
    " mitwork_lots.csv",
    " mitwork_announcements.json",
    " mitwork_v2_report.json",
]
summary_path.write_text("\n".join(summary), encoding="utf-8")

# One convenient ZIP to send back
send_zip = ROOT / "SEND_ME_MITWORK_V2.zip"
with zipfile.ZipFile(send_zip, "w", zipfile.ZIP_DEFLATED) as z:
    for p in [csv_path, lots_path, json_path, report_path, summary_path]:
        z.write(p, p.name)

print()
print("\n".join(summary))
print()
print("READY TO SEND:", send_zip.name)
