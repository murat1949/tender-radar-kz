# -*- coding: utf-8 -*-
"""
Tender Radar KZ — Samruk LOT collector

Единая архитектура:
    Samruk -> ЛОТ -> Tender Radar

Что изменено относительно предыдущего варианта:
- поиск остаётся строго по вкладке "Лоты";
- подробная карточка открывается КЛИКОМ по найденному лоту,
  а не прямым переходом по popup-URL;
- прямой URL сохраняется уже после успешного клика;
- даты ищутся устойчивее: и в той же строке, и на соседних строках;
- после каждой карточки возврат идёт в тот же список/страницу;
- результаты сохраняются до и во время подробной проверки,
  поэтому даже прерванный запуск не теряет уже найденные лоты;
- услуги исключаются;
- сам сборщик НЕ пишет в Supabase. Это делает общий cloud-sync.

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
WAIT_SECONDS = int(os.getenv("SAMRUK_WAIT_SECONDS", "25"))

# 0 = проверить подробно все найденные лоты.
# В тестовом workflow сейчас SAMRUK_DETAIL_LIMIT=3.
DETAIL_LIMIT = int(os.getenv("SAMRUK_DETAIL_LIMIT", "0"))

OUT = Path("output")
OUT.mkdir(exist_ok=True)

# Время портала Samruk отображается как локальное время Казахстана.
# Для наших задач достаточно фиксированного UTC+5.
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
    s = str(value or "").replace("\xa0", " ").replace("₸", "").strip()
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
    s = clean_text(text).lower()
    m = re.search(
        r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})\s*г?\.?,?\s*(\d{1,2}):(\d{2})",
        s,
        re.I,
    )
    if not m:
        return None

    month = RU_MONTHS.get(m.group(2))
    if not month:
        return None

    dt = datetime(
        int(m.group(3)),
        month,
        int(m.group(1)),
        int(m.group(4)),
        int(m.group(5)),
        tzinfo=KZ_TZ,
    )
    return dt.isoformat()


def remaining_to_expires_at(text):
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


def lot_fallback_url(lot_id, keyword="картридж", page=1):
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
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: (
                len(d.find_elements(By.CSS_SELECTOR, "div.m-sidebar__layout--found-item")) > 0
                or "Найдено 0" in (d.find_element(By.TAG_NAME, "body").text or "")
            )
        )
    except Exception:
        pass

    return driver.find_elements(By.CSS_SELECTOR, "div.m-sidebar__layout--found-item")


def extract_card_title(card):
    for selector in [".m-found-item__title", ".m-found-item__title--name", "h3", "h4"]:
        try:
            value = clean_text(card.find_element(By.CSS_SELECTOR, selector).text)
            if value:
                return value
        except Exception:
            pass

    for line in lines_of(card.text):
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
        "source_tender_id": None,
        "source_lot_id": lot_id,
        "public_url": lot_fallback_url(lot_id, keyword, page),
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
            "document_names": [],
            "has_documents": False,
            "has_techspec": False,
            "parent_tender_id": None,
            "detail_checked": False,
        },
    }


def label_value_from_lines(text, labels):
    """
    Ищет значение после подписи и в соседней строке, и в той же строке.
    """
    ls = lines_of(text)
    labels_low = [clean_text(x).lower() for x in labels]

    for i, line in enumerate(ls):
        low = line.lower()
        for lab in labels_low:
            if low == lab and i + 1 < len(ls):
                return ls[i + 1]

            if low.startswith(lab + " "):
                rest = clean_text(line[len(lab):])
                if rest:
                    return rest
    return None


def date_for_labels(text, labels):
    """
    Устойчивый поиск даты после подписи.
    Поддерживает и:
        КОНЕЦ ОБСУЖДЕНИЯ
        17 сентября 2026 г., 10:00
    и:
        КОНЕЦ ОБСУЖДЕНИЯ 17 сентября 2026 г., 10:00
    """
    normalized = clean_text(text)

    month_words = "|".join(map(re.escape, RU_MONTHS.keys()))
    date_re = (
        rf"(\d{{1,2}}\s+(?:{month_words})\s+\d{{4}}\s*г?\.?,?\s*"
        rf"\d{{1,2}}:\d{{2}})"
    )

    for label in labels:
        pattern = rf"{re.escape(label)}\s*[:\-]?\s*{date_re}"
        m = re.search(pattern, normalized, re.I)
        if m:
            return parse_ru_datetime(m.group(1))

    # Резерв: построчный поиск.
    ls = lines_of(text)
    for i, line in enumerate(ls):
        low = line.lower()
        if any(low.startswith(label.lower()) for label in labels):
            parsed = parse_ru_datetime(line)
            if parsed:
                return parsed
            for j in range(i + 1, min(i + 4, len(ls))):
                parsed = parse_ru_datetime(ls[j])
                if parsed:
                    return parsed
    return None


def extract_parent_tender_id_without_click(driver):
    """
    Не уходим со страницы лота.
    Пытаемся извлечь номер закупки из атрибутов/HTML элемента
    "Перейти на закупку".
    """
    try:
        elems = driver.find_elements(
            By.XPATH,
            "//*[contains(normalize-space(.),'Перейти на закупку')]"
        )
    except Exception:
        elems = []

    for el in elems[:5]:
        try:
            parts = [
                el.get_attribute("href"),
                el.get_attribute("onclick"),
                el.get_attribute("ng-click"),
                el.get_attribute("ui-sref"),
                el.get_attribute("outerHTML"),
            ]
            blob = " ".join(clean_text(x) for x in parts if x)

            for pattern in [
                r"item/(\d{5,})/advert",
                r"advert[^0-9]{0,20}(\d{5,})",
                r"(\d{5,})[^0-9]{0,20}advert",
            ]:
                m = re.search(pattern, blob, re.I)
                if m:
                    return m.group(1)
        except Exception:
            pass

    return None


def extract_documents_metadata(driver):
    doc_count = 0
    names = []
    urls = []

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

    # Не раскрываем список, если это может изменить состояние страницы.
    # Считываем доступные ссылки/названия, если они уже присутствуют в DOM.
    try:
        for a in driver.find_elements(By.TAG_NAME, "a"):
            try:
                txt = clean_text(a.text)
                href = clean_text(a.get_attribute("href"))
                blob = f"{txt} {href}".lower()
                if not href:
                    continue
                if (
                    "документ" in blob
                    or "download" in blob
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


def find_card_by_lot_id(driver, lot_id):
    cards = driver.find_elements(By.CSS_SELECTOR, "div.m-sidebar__layout--found-item")
    needle = str(lot_id)

    for card in cards:
        try:
            if re.search(rf"№\s*{re.escape(needle)}\b", card.text or ""):
                return card
        except Exception:
            continue

    return None


def click_lot_card(driver, lot_id):
    """
    Открываем карточку именно кликом по найденному лоту.
    Это ключевое отличие от нестабильного прямого перехода.
    """
    card = find_card_by_lot_id(driver, lot_id)
    if card is None:
        raise RuntimeError(f"Lot card {lot_id} not found on current search page")

    attempts = []

    # 1. Обычный click.
    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});",
            card,
        )
        time.sleep(0.15)
        card.click()
        attempts.append("native")
    except Exception:
        pass

    def detail_is_open(d):
        try:
            url = d.current_url or ""
            body = d.find_element(By.TAG_NAME, "body").text or ""
            return (
                f"/{lot_id}/lot" in url
                and re.search(rf"№\s*{re.escape(str(lot_id))}\b", body) is not None
            )
        except Exception:
            return False

    try:
        WebDriverWait(driver, 6).until(detail_is_open)
        return
    except Exception:
        pass

    # 2. JS click по карточке.
    card = find_card_by_lot_id(driver, lot_id)
    if card is not None:
        try:
            driver.execute_script("arguments[0].click();", card)
            attempts.append("js")
            WebDriverWait(driver, 8).until(detail_is_open)
            return
        except Exception:
            pass

    # 3. Иногда клик висит на заголовке внутри карточки.
    card = find_card_by_lot_id(driver, lot_id)
    if card is not None:
        try:
            candidates = card.find_elements(
                By.CSS_SELECTOR,
                "a, button, .m-found-item__title, .m-found-item__title--name"
            )
            for child in candidates:
                try:
                    driver.execute_script("arguments[0].click();", child)
                    attempts.append("child-js")
                    WebDriverWait(driver, 5).until(detail_is_open)
                    return
                except Exception:
                    continue
        except Exception:
            pass

    raise RuntimeError(
        f"Could not open lot {lot_id} by click; attempts={attempts}; "
        f"url={driver.current_url}"
    )


def enrich_from_open_detail(driver, row):
    lot_id = row["source_lot_id"]

    body = driver.find_element(By.TAG_NAME, "body").text or ""
    current_url = driver.current_url or ""

    if f"/{lot_id}/lot" not in current_url:
        raise RuntimeError(
            f"Opened entity is not lot {lot_id}: {current_url}"
        )

    # После успешного клика это лучший URL для "Открыть оригинал".
    row["public_url"] = current_url

    customer = label_value_from_lines(body, ["ЗАКАЗЧИК"])
    delivery_place = label_value_from_lines(body, ["МЕСТО ПОСТАВКИ"])
    procurement_place = label_value_from_lines(body, ["МЕСТО ПРОВЕДЕНИЯ ЗАКУПОК"])
    ens_tru = label_value_from_lines(body, ["КОД ЕНС ТРУ"])
    characteristic = label_value_from_lines(body, ["КРАТКАЯ ХАРАКТЕРИСТИКА"])
    quantity_text = label_value_from_lines(body, ["КОЛИЧЕСТВО"])
    unit = label_value_from_lines(body, ["ЕД. ИЗМЕРЕНИЯ", "ЕДИНИЦА ИЗМЕРЕНИЯ"])

    started_at = date_for_labels(
        body,
        [
            "НАЧАЛО ОБСУЖДЕНИЯ",
            "НАЧАЛО ПРИЕМА ЗАЯВОК",
            "НАЧАЛО ПРИЁМА ЗАЯВОК",
        ],
    )
    expires_at = date_for_labels(
        body,
        [
            "КОНЕЦ ОБСУЖДЕНИЯ",
            "КОНЕЦ ПРИЕМА ЗАЯВОК",
            "КОНЕЦ ПРИЁМА ЗАЯВОК",
        ],
    )

    parent_tender_id = extract_parent_tender_id_without_click(driver)
    doc_count, doc_urls, doc_names, has_techspec = extract_documents_metadata(driver)

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

    if row.get("expires_at"):
        try:
            exp = datetime.fromisoformat(row["expires_at"])
            now = datetime.now(exp.tzinfo or timezone.utc)
            row["is_active"] = exp > now
        except Exception:
            row["is_active"] = True

    parts = [
        row.get("description"),
        characteristic,
        f"Место поставки: {delivery_place}" if delivery_place else None,
        f"Код ЕНС ТРУ: {ens_tru}" if ens_tru else None,
    ]
    row["description"] = clean_text(" | ".join(x for x in parts if x))

    row["raw"].update({
        "detail_checked": True,
        "detail_open_method": "click",
        "detail_url": current_url,
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

    return row


def return_to_search(driver, keyword, page):
    target = lot_search_url(keyword, page)

    # Сначала пытаемся вернуться назад — это быстрее.
    try:
        driver.back()
        WebDriverWait(driver, 8).until(
            lambda d: "/lot" not in (d.current_url or "")
        )
        cards = wait_for_cards(driver, timeout=8)
        if cards:
            return
    except Exception:
        pass

    # Надёжный резерв.
    driver.get(target)
    wait_for_cards(driver, timeout=WAIT_SECONDS)


def save_results(all_rows):
    # Финальная защита: только товары и только lot-level.
    goods_rows = []

    for row in all_rows:
        lot_id = clean_text(row.get("source_lot_id"))
        url = clean_text(row.get("public_url"))
        blob = f"{row.get('title','')} {row.get('description','')}"

        if not lot_id:
            continue
        if "/lot" not in url:
            continue
        if is_service(blob):
            continue
        goods_rows.append(row)

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

    return goods_rows, all_json, json_path, csv_path


def collect(driver):
    rows_by_id = {}

    for keyword in KEYWORDS:
        print("")
        print("SEARCH LOTS:", keyword)
        seen_signatures = set()

        for page in range(1, MAX_PAGES_PER_KEYWORD + 1):
            search_url = lot_search_url(keyword, page)
            print("  PAGE:", page, search_url)

            driver.get(search_url)
            WebDriverWait(driver, WAIT_SECONDS).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            cards = wait_for_cards(driver)
            print("  CARDS:", len(cards))

            if not cards:
                break

            page_ids = []
            new_on_page = 0

            # Сначала снимаем список карточек без кликов.
            parsed_page_rows = []
            for card in cards:
                row = parse_list_card(card, keyword, page)
                if not row:
                    continue

                lot_id = row["source_lot_id"]
                page_ids.append(lot_id)

                if row["raw"]["is_service"]:
                    print("    SKIP SERVICE:", lot_id, row["title"])
                    continue

                if not row["raw"]["is_relevant_goods"]:
                    print("    SKIP IRRELEVANT:", lot_id, row["title"])
                    continue

                if lot_id not in rows_by_id:
                    rows_by_id[lot_id] = row
                    parsed_page_rows.append(row)
                    new_on_page += 1
                    print("    LOT:", lot_id, "|", row["title"])
                else:
                    raw = rows_by_id[lot_id]["raw"]
                    matched = raw.setdefault("matched_keywords", [])
                    if keyword not in matched:
                        matched.append(keyword)

            # Сразу сохраняем список, чтобы не потерять его при сбое детали.
            save_results(list(rows_by_id.values()))

            signature = tuple(page_ids)
            if signature in seen_signatures:
                print("  STOP: repeated page signature")
                break
            seen_signatures.add(signature)

            if new_on_page == 0 and page > 1:
                print("  STOP: no new relevant lots")
                break

    return list(rows_by_id.values())


def enrich_rows(driver, rows):
    if DETAIL_LIMIT > 0:
        targets = rows[:DETAIL_LIMIT]
    else:
        targets = rows

    print("")
    print("DETAIL TARGETS:", len(targets), "/", len(rows))

    for idx, row in enumerate(targets, 1):
        lot_id = row["source_lot_id"]
        keyword = row["raw"].get("keyword") or "картридж"
        page = int(row["raw"].get("page") or 1)
        search_url = lot_search_url(keyword, page)

        try:
            # На всякий случай возвращаемся на нужную страницу поиска.
            if (
                f"page={page}" not in (driver.current_url or "")
                or "tabs=lot" not in (driver.current_url or "")
                or "/lot" in (driver.current_url or "")
            ):
                driver.get(search_url)
                wait_for_cards(driver)

            print(f"DETAIL {idx}/{len(targets)}: LOT {lot_id}")

            click_lot_card(driver, lot_id)
            enrich_from_open_detail(driver, row)

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
            row["raw"]["detail_open_method"] = "click"
            row["raw"]["detail_error"] = repr(e)
            print("  WARNING detail failed:", lot_id, repr(e))

        finally:
            # Сохраняем прогресс после каждой карточки.
            save_results(rows)

            try:
                if "/lot" in (driver.current_url or ""):
                    return_to_search(driver, keyword, page)
            except Exception:
                pass

    # Строки вне тестового лимита не считаем ошибкой.
    if DETAIL_LIMIT > 0:
        for row in rows[len(targets):]:
            row["raw"]["detail_checked"] = False
            row["raw"]["detail_error"] = "DETAIL_LIMIT reached (test mode)"

    return rows


def main():
    print("=" * 78)
    print("TENDER RADAR KZ - SAMRUK LOT COLLECTOR")
    print("ENTITY LEVEL: LOT")
    print("DETAIL OPEN METHOD: CLICK FOUND LOT CARD")
    print("MODE: READ ONLY, NO SUPABASE WRITE")
    print("=" * 78)

    driver = make_driver()

    try:
        rows = collect(driver)

        if not rows:
            raise RuntimeError("No relevant Samruk LOT rows were extracted")

        print("")
        print("RELEVANT UNIQUE LOTS:", len(rows))

        rows = enrich_rows(driver, rows)

        goods_rows, all_json, json_path, csv_path = save_results(rows)

        print("")
        print("ALL RELEVANT LOTS:", len(rows))
        print("GOODS SAVED:", len(goods_rows))
        print("DETAILS TARGETED:", len(rows) if DETAIL_LIMIT <= 0 else min(DETAIL_LIMIT, len(rows)))
        print("DETAILS OK:", sum(r.get("raw", {}).get("detail_checked") is True for r in rows))
        print("WITH EXPIRES_AT:", sum(bool(r.get("expires_at")) for r in rows))
        print("WITH CUSTOMER:", sum(bool(r.get("customer_name")) for r in rows))
        print("WITH PARENT TENDER:", sum(bool(r.get("source_tender_id")) for r in rows))
        print("ALL JSON:", all_json)
        print("JSON:", json_path)
        print("CSV :", csv_path)

        bad = [
            r for r in goods_rows
            if "/lot" not in clean_text(r.get("public_url"))
            or not clean_text(r.get("source_lot_id"))
        ]
        if bad:
            raise RuntimeError(
                f"Architecture check failed: {len(bad)} rows are not LOT-level"
            )

        if not goods_rows:
            raise RuntimeError("Final Samruk LOT dataset is empty")

        print("")
        print("SUCCESS: Samruk LOT-level collection completed")

    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
