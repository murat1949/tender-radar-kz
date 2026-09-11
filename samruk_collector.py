# -*- coding: utf-8 -*-
"""
Tender Radar KZ — Samruk LOT collector

Главный принцип:
    Samruk -> ЛОТ -> Tender Radar

Сборщик:
- работает только с вкладкой "Лоты";
- ищет товарные лоты по ключевым словам;
- исключает услуги;
- сохраняет source_lot_id как номер ЛОТА;
- формирует прямую ссылку именно на карточку ЛОТА;
- по возможности открывает карточку лота и добирает:
  заказчика, место поставки, краткую характеристику,
  точные даты, документы и номер родительской закупки;
- НЕ пишет в Supabase сам. Это делает общий cloud-sync.

Выход:
    output/samruk_tenders.json
    output/samruk_tenders.csv
    output/samruk_all_results.json
"""

import csv
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


BASE_URL = "https://zakup.sk.kz"

KEYWORDS = [
    "картридж",
    "тонер",
    "расходные материалы для оргтехники",
]

MAX_PAGES_PER_KEYWORD = int(os.getenv("SAMRUK_MAX_PAGES", "12"))
DETAIL_LIMIT = int(os.getenv("SAMRUK_DETAIL_LIMIT", "120"))
WAIT_SECONDS = int(os.getenv("SAMRUK_WAIT_SECONDS", "40"))

OUT = Path("output")
OUT.mkdir(exist_ok=True)

KZ_TZ = timezone(timedelta(hours=5))

RU_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

SERVICE_PATTERNS = [
    r"\bуслуг",
    r"\bработ",
    r"заправк",
    r"ремонт",
    r"обслуживан",
    r"восстановлен",
    r"утилизац",
    r"диагностик",
]

GOODS_PATTERNS = [
    r"картридж",
    r"\bтонер\b",
    r"тонерн",
    r"фотобарабан",
    r"драм[-\s]?картридж",
    r"расходн.*оргтех",
    r"печатающ.*устройств",
]


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def lines_of(text):
    return [clean_text(x) for x in str(text or "").splitlines() if clean_text(x)]


def money_to_float(value):
    s = str(value or "")
    s = s.replace("\xa0", " ").replace("₸", "").strip()
    # Сохраняем десятичную часть и убираем разделители тысяч.
    s = re.sub(r"(?<=\d)\s+(?=\d{3}(?:\D|$))", "", s)
    s = s.replace(" ", "").replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def number_to_float(value):
    if value is None:
        return None
    s = clean_text(value).replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def parse_ru_datetime(text):
    """
    Пример Samruk:
        17 сентября 2026 г., 10:00
        11 сентября 2026 г., 16:59
    """
    s = clean_text(text).lower()
    m = re.search(
        r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})\s*г?\.?,?\s*(\d{1,2}):(\d{2})",
        s,
        re.I,
    )
    if not m:
        return None

    day = int(m.group(1))
    month = RU_MONTHS.get(m.group(2))
    year = int(m.group(3))
    hour = int(m.group(4))
    minute = int(m.group(5))

    if not month:
        return None

    dt = datetime(year, month, day, hour, minute, tzinfo=KZ_TZ)
    return dt.isoformat()


def remaining_to_expires_at(text):
    """
    Резервный вариант, если точная дата не считалась из карточки лота.
    """
    s = clean_text(text).lower()
    delta = timedelta()

    m = re.search(r"(\d+)\s*д", s)
    if m:
        delta += timedelta(days=int(m.group(1)))

    m = re.search(r"(\d+)\s*час", s)
    if m:
        delta += timedelta(hours=int(m.group(1)))

    m = re.search(r"(\d+)\s*мин", s)
    if m:
        delta += timedelta(minutes=int(m.group(1)))

    if delta.total_seconds() <= 0:
        return None

    return (datetime.now(timezone.utc) + delta).replace(microsecond=0).isoformat()


def is_service(text):
    low = clean_text(text).lower()
    return any(re.search(p, low, re.I) for p in SERVICE_PATTERNS)


def is_relevant_goods(text):
    low = clean_text(text).lower()
    if is_service(low):
        return False
    return any(re.search(p, low, re.I) for p in GOODS_PATTERNS)


def lot_search_url(keyword, page=1):
    return (
        f"{BASE_URL}/#/ext?"
        f"tabs=lot&q={quote(keyword)}&adst=ALL&lst=ALL&page={int(page)}"
    )


def lot_public_url(lot_id, keyword="картридж", page=1):
    # Формат подтвержден вручную на реальной карточке Samruk:
    # .../#/ext(popup:item/4530311/lot)?tabs=lot&q=картридж&...
    return (
        f"{BASE_URL}/#/ext(popup:item/{lot_id}/lot)?"
        f"tabs=lot&q={quote(keyword)}&adst=ALL&lst=ALL&page={int(page)}"
    )


def make_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=ru-RU")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-notifications")
    return webdriver.Chrome(options=options)


def wait_for_cards(driver, timeout=WAIT_SECONDS):
    wait = WebDriverWait(driver, timeout)
    try:
        wait.until(
            lambda d: (
                len(d.find_elements(
                    By.CSS_SELECTOR,
                    "div.m-sidebar__layout--found-item"
                )) > 0
                or "Найдено 0" in (d.find_element(By.TAG_NAME, "body").text or "")
            )
        )
    except Exception:
        pass

    return driver.find_elements(
        By.CSS_SELECTOR,
        "div.m-sidebar__layout--found-item"
    )


def extract_card_title(card):
    selectors = [
        ".m-found-item__title",
        ".m-found-item__title--name",
        "h3",
        "h4",
    ]
    for selector in selectors:
        try:
            value = clean_text(card.find_element(By.CSS_SELECTOR, selector).text)
            if value:
                return value
        except Exception:
            pass

    ls = lines_of(card.text)
    for line in ls:
        if re.fullmatch(r"№\s*\d+", line):
            continue
        if line.lower().startswith(("осталось:", "стоимость:")):
            continue
        if len(line) >= 3:
            return line

    return ""


def parse_list_card(card, keyword, page):
    text = card.text or ""
    ls = lines_of(text)

    lot_match = re.search(r"№\s*(\d{5,})", text)
    lot_id = lot_match.group(1) if lot_match else None
    if not lot_id:
        return None

    title = extract_card_title(card)

    method = None
    remaining = None
    amount_text = None
    status_name = None

    for line in ls:
        low = line.lower()

        if not method and (
            "запрос ценовых" in low
            or "открытый тендер" in low
            or "тендер на понижение" in low
            or "электронный магазин" in low
            or "из одного источника" in low
            or "аукцион" in low
        ):
            method = line

        if line.startswith("Осталось:"):
            remaining = clean_text(line.split(":", 1)[1])

        if line.startswith("Стоимость:"):
            amount_text = clean_text(line.split(":", 1)[1])

        if (
            "опубликован" in low
            or "обсуждени" in low
            or "прием заяв" in low
            or "приём заяв" in low
        ):
            status_name = line

    blob = f"{title} {text}"

    return {
        "source_code": "samruk",
        "source_tender_id": None,       # родительская закупка добирается из карточки лота
        "source_lot_id": lot_id,        # КЛЮЧЕВОЕ: здесь номер ЛОТА
        "public_url": lot_public_url(lot_id, keyword, page),
        "title": title or f"Лот Samruk №{lot_id}",
        "description": clean_text(text),
        "customer_name": None,
        "customer_bin": None,
        "region": None,
        "procurement_method": method,
        "status_code": None,
        "status_name": status_name or "Опубликовано",
        "amount": money_to_float(amount_text),
        "currency": "KZT",
        "quantity": None,
        "unit": None,
        "category": keyword,
        "published_at": None,
        "started_at": None,
        "expires_at": remaining_to_expires_at(remaining),
        "is_active": True,
        "raw": {
            "entity_level": "lot",
            "keyword": keyword,
            "page": page,
            "remaining_text": remaining,
            "amount_text": amount_text,
            "list_text": clean_text(text),
            "is_service": is_service(blob),
            "is_relevant_goods": is_relevant_goods(blob),
            "documents_count": 0,
            "document_urls": [],
            "has_documents": False,
            "has_techspec": False,
            "parent_tender_id": None,
        },
    }


def value_after_label(text, labels):
    ls = lines_of(text)
    wanted = [x.lower() for x in labels]

    for i, line in enumerate(ls):
        low = line.lower()
        if any(low == w or low.startswith(w) for w in wanted):
            if i + 1 < len(ls):
                nxt = ls[i + 1]
                # Не возвращаем следующий заголовок как значение.
                if nxt:
                    return nxt
    return None


def date_after_label(text, label_starts):
    ls = lines_of(text)
    wanted = [x.lower() for x in label_starts]

    for i, line in enumerate(ls):
        low = line.lower()
        if any(low.startswith(w) for w in wanted):
            for j in range(i + 1, min(i + 4, len(ls))):
                parsed = parse_ru_datetime(ls[j])
                if parsed:
                    return parsed
    return None


def extract_parent_tender_id(driver):
    candidates = []
    xpaths = [
        "//a[contains(normalize-space(.),'Перейти на закупку')]",
        "//button[contains(normalize-space(.),'Перейти на закупку')]",
        "//*[contains(normalize-space(.),'Перейти на закупку')]",
    ]

    for xp in xpaths:
        try:
            candidates.extend(driver.find_elements(By.XPATH, xp))
        except Exception:
            pass

    # 1) Сначала пробуем извлечь номер без клика.
    for el in candidates:
        try:
            pieces = [
                el.get_attribute("href"),
                el.get_attribute("onclick"),
                el.get_attribute("outerHTML"),
            ]
            blob = " ".join(clean_text(x) for x in pieces if x)
            m = re.search(r"item/(\d{5,})/advert", blob)
            if m:
                return m.group(1)
        except Exception:
            pass

    # 2) Если href нет — кликаем по ссылке и читаем route.
    for el in candidates[:3]:
        try:
            driver.execute_script("arguments[0].click();", el)
            WebDriverWait(driver, 8).until(
                lambda d: "/advert" in (d.current_url or "")
            )
            m = re.search(r"item/(\d{5,})/advert", driver.current_url or "")
            if m:
                return m.group(1)
        except Exception:
            pass

    return None


def extract_documents(driver):
    doc_count = 0
    urls = []
    names = []

    controls = []
    try:
        controls = driver.find_elements(
            By.XPATH,
            "//*[contains(normalize-space(.),'Документы')]"
        )
    except Exception:
        controls = []

    for el in controls:
        try:
            txt = clean_text(el.text)
            m = re.search(r"Документы\s*(\d+)", txt, re.I)
            if m:
                doc_count = max(doc_count, int(m.group(1)))
        except Exception:
            pass

    # Пробуем раскрыть меню документов.
    for el in controls[:2]:
        try:
            driver.execute_script("arguments[0].click();", el)
            time.sleep(0.5)
        except Exception:
            pass

    try:
        anchors = driver.find_elements(By.TAG_NAME, "a")
        for a in anchors:
            try:
                txt = clean_text(a.text)
                href = clean_text(a.get_attribute("href"))
                blob = f"{txt} {href}".lower()

                if not href:
                    continue

                if (
                    "документ" in blob
                    or "download" in blob
                    or "file" in blob
                    or "attachment" in blob
                    or "spec" in blob
                ):
                    if href not in urls:
                        urls.append(href)
                        names.append(txt or href.rsplit("/", 1)[-1])
            except Exception:
                pass
    except Exception:
        pass

    has_techspec = any(
        re.search(r"технич|техспец|специф", name, re.I)
        for name in names
    )

    return doc_count, urls, names, has_techspec


def enrich_lot_detail(driver, row, save_sample=False):
    """
    Открывает карточку конкретного лота и добавляет детали.
    Ошибка одной карточки не ломает весь сбор.
    """
    url = row["public_url"]
    driver.get(url)

    wait = WebDriverWait(driver, WAIT_SECONDS)
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    time.sleep(1.2)

    body = driver.find_element(By.TAG_NAME, "body").text or ""

    # Проверка, что мы действительно на ЛОТЕ.
    current_url = driver.current_url or ""
    if f"/{row['source_lot_id']}/lot" not in current_url:
        raise RuntimeError(
            f"Samruk opened unexpected entity for lot {row['source_lot_id']}: "
            f"{current_url}"
        )

    customer = value_after_label(body, ["ЗАКАЗЧИК"])
    delivery_place = value_after_label(body, ["МЕСТО ПОСТАВКИ"])
    procurement_place = value_after_label(body, ["МЕСТО ПРОВЕДЕНИЯ ЗАКУПОК"])
    ens_tru = value_after_label(body, ["КОД ЕНС ТРУ"])
    characteristic = value_after_label(body, ["КРАТКАЯ ХАРАКТЕРИСТИКА"])
    quantity_text = value_after_label(body, ["КОЛИЧЕСТВО"])
    unit = value_after_label(body, ["ЕД. ИЗМЕРЕНИЯ", "ЕДИНИЦА ИЗМЕРЕНИЯ"])

    started_at = date_after_label(
        body,
        [
            "НАЧАЛО ОБСУЖДЕНИЯ",
            "НАЧАЛО ПРИЕМА ЗАЯВОК",
            "НАЧАЛО ПРИЁМА ЗАЯВОК",
        ],
    )
    expires_at = date_after_label(
        body,
        [
            "КОНЕЦ ОБСУЖДЕНИЯ",
            "КОНЕЦ ПРИЕМА ЗАЯВОК",
            "КОНЕЦ ПРИЁМА ЗАЯВОК",
        ],
    )

    # Документы читаем ДО клика "Перейти на закупку".
    doc_count, doc_urls, doc_names, has_techspec = extract_documents(driver)

    # "Перейти на закупку" дает родительский номер закупки.
    parent_tender_id = extract_parent_tender_id(driver)

    if customer:
        row["customer_name"] = customer
    if delivery_place:
        row["region"] = delivery_place
    if quantity_text:
        row["quantity"] = number_to_float(quantity_text)
    if unit:
        row["unit"] = unit
    if started_at:
        row["started_at"] = started_at
        row["published_at"] = row.get("published_at") or started_at
    if expires_at:
        row["expires_at"] = expires_at
    if parent_tender_id:
        row["source_tender_id"] = parent_tender_id

    detail_parts = [
        row.get("description"),
        characteristic,
        f"Место поставки: {delivery_place}" if delivery_place else None,
        f"Код ЕНС ТРУ: {ens_tru}" if ens_tru else None,
    ]
    row["description"] = clean_text(
        " | ".join(x for x in detail_parts if x)
    )

    row["raw"].update({
        "detail_checked": True,
        "detail_url": url,
        "parent_tender_id": parent_tender_id,
        "delivery_place": delivery_place,
        "procurement_place": procurement_place,
        "ens_tru": ens_tru,
        "short_characteristic": characteristic,
        "quantity_text": quantity_text,
        "documents_count": doc_count,
        "document_urls": doc_urls,
        "document_names": doc_names,
        "has_documents": bool(doc_count or doc_urls),
        "has_techspec": bool(has_techspec),
    })

    if save_sample:
        try:
            driver.save_screenshot(str(OUT / "samruk_lot_sample.png"))
        except Exception:
            pass

    return row


def collect_list_rows(driver):
    all_rows = {}
    seen_page_signatures = set()

    for keyword in KEYWORDS:
        print("")
        print("SEARCH LOTS:", keyword)

        for page in range(1, MAX_PAGES_PER_KEYWORD + 1):
            url = lot_search_url(keyword, page)
            print("  PAGE:", page, url)

            driver.get(url)
            WebDriverWait(driver, WAIT_SECONDS).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            time.sleep(1.2)

            cards = wait_for_cards(driver)
            print("  CARDS:", len(cards))

            if not cards:
                break

            page_ids = []
            new_on_page = 0

            for card in cards:
                row = parse_list_card(card, keyword, page)
                if not row:
                    continue

                lot_id = row["source_lot_id"]
                page_ids.append(lot_id)

                # Берем только товары нужного профиля.
                if row["raw"]["is_service"]:
                    print("    SKIP SERVICE:", lot_id, row["title"])
                    continue

                if not row["raw"]["is_relevant_goods"]:
                    print("    SKIP IRRELEVANT:", lot_id, row["title"])
                    continue

                if lot_id not in all_rows:
                    all_rows[lot_id] = row
                    new_on_page += 1
                    print("    LOT:", lot_id, "|", row["title"])
                else:
                    # Если тот же лот встретился по другому ключевому слову,
                    # сохраняем список совпавших поисковых фраз.
                    raw = all_rows[lot_id]["raw"]
                    matched = raw.setdefault("matched_keywords", [])
                    if keyword not in matched:
                        matched.append(keyword)

            signature = tuple(page_ids)

            if signature in seen_page_signatures:
                print("  STOP: repeated page signature")
                break
            seen_page_signatures.add(signature)

            if new_on_page == 0 and page > 1:
                print("  STOP: no new relevant lots")
                break

    return list(all_rows.values())


def save_results(all_rows, goods_rows):
    all_json = OUT / "samruk_all_results.json"
    json_path = OUT / "samruk_tenders.json"
    csv_path = OUT / "samruk_tenders.csv"

    all_json.write_text(
        json.dumps(all_rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    json_path.write_text(
        json.dumps(goods_rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    fields = [
        "source_tender_id",
        "source_lot_id",
        "title",
        "customer_name",
        "region",
        "procurement_method",
        "amount",
        "currency",
        "quantity",
        "unit",
        "started_at",
        "expires_at",
        "public_url",
    ]

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in goods_rows:
            writer.writerow({k: row.get(k) for k in fields})

    return all_json, json_path, csv_path


def main():
    print("=" * 76)
    print("TENDER RADAR KZ - SAMRUK LOT COLLECTOR")
    print("ENTITY LEVEL: LOT")
    print("MODE: READ ONLY, NO SUPABASE WRITE")
    print("=" * 76)

    list_driver = make_driver()
    detail_driver = None

    try:
        rows = collect_list_rows(list_driver)

        if not rows:
            raise RuntimeError("No relevant Samruk LOT rows were extracted")

        print("")
        print("RELEVANT UNIQUE LOTS:", len(rows))

        # Обогащение деталей.
        detail_driver = make_driver()
        detail_count = min(len(rows), DETAIL_LIMIT)

        for idx, row in enumerate(rows[:detail_count], 1):
            lot_id = row["source_lot_id"]
            try:
                print(
                    f"DETAIL {idx}/{detail_count}: LOT {lot_id}"
                )
                enrich_lot_detail(
                    detail_driver,
                    row,
                    save_sample=(idx == 1),
                )
                print(
                    "  OK | tender:",
                    row.get("source_tender_id"),
                    "| customer:",
                    row.get("customer_name"),
                    "| expires:",
                    row.get("expires_at"),
                )
            except Exception as e:
                row["raw"]["detail_checked"] = False
                row["raw"]["detail_error"] = repr(e)
                print("  WARNING detail failed:", lot_id, repr(e))

        # Если деталей больше лимита, строки все равно остаются валидными:
        # номер лота, название, сумма, срок и прямая ссылка уже есть из списка.
        for row in rows[detail_count:]:
            row["raw"]["detail_checked"] = False
            row["raw"]["detail_error"] = "DETAIL_LIMIT reached"

        # Финальная защита: только лоты и только товары.
        goods_rows = []
        for row in rows:
            lot_id = clean_text(row.get("source_lot_id"))
            url = clean_text(row.get("public_url"))
            blob = f"{row.get('title','')} {row.get('description','')}"

            if not lot_id:
                continue
            if f"/{lot_id}/lot" not in url:
                continue
            if is_service(blob):
                continue

            goods_rows.append(row)

        all_json, json_path, csv_path = save_results(rows, goods_rows)

        print("")
        print("ALL RELEVANT LOTS:", len(rows))
        print("GOODS SAVED:", len(goods_rows))
        print("DETAILS CHECKED:", detail_count)
        print("ALL JSON:", all_json)
        print("JSON:", json_path)
        print("CSV :", csv_path)

        if not goods_rows:
            raise RuntimeError("Final Samruk LOT dataset is empty")

        # Контрольная проверка архитектуры.
        bad = [
            r for r in goods_rows
            if "/lot" not in clean_text(r.get("public_url"))
            or not clean_text(r.get("source_lot_id"))
        ]
        if bad:
            raise RuntimeError(
                f"Architecture check failed: {len(bad)} rows are not LOT-level"
            )

        print("")
        print("SUCCESS: Samruk LOT-level collection completed")

    finally:
        try:
            list_driver.quit()
        except Exception:
            pass
        if detail_driver is not None:
            try:
                detail_driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()
