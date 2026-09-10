# -*- coding: utf-8 -*-

import csv
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


URL = "https://zakup.sk.kz/"
KEYWORD = "картридж"

OUT = Path("output")
OUT.mkdir(exist_ok=True)


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def money_to_float(value):
    s = str(value or "")
    s = s.replace("\xa0", "").replace(" ", "").replace("₸", "")
    s = s.replace(",", ".")
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def remaining_to_expires_at(text):
    """
    Samruk показывает относительный срок, например:
      5 дней
      9 часов 57 минут
    Превращаем его в приблизительный ISO datetime.
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


def make_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=ru-RU")
    return webdriver.Chrome(options=options)


def find_search_box(driver):
    inputs = driver.find_elements(By.TAG_NAME, "input")

    for item in inputs:
        placeholder = item.get_attribute("placeholder") or ""
        if (
            "Слово для поиска" in placeholder
            or "номер закупки" in placeholder
        ):
            return item

    return None


def parse_card(card, search_url):
    text = card.text or ""
    lines = [clean_text(x) for x in text.splitlines() if clean_text(x)]

    lot_match = re.search(r"№\s*(\d{5,})", text)
    lot_id = lot_match.group(1) if lot_match else None

    try:
        title = clean_text(
            card.find_element(By.CSS_SELECTOR, ".m-found-item__title").text
        )
    except Exception:
        title = ""

    method = ""
    remaining = ""
    amount_text = ""

    for line in lines:
        low = line.lower()

        if (
            not method
            and (
                "запрос ценовых" in low
                or "тендер" in low
                or "электронный магазин" in low
                or "из одного источника" in low
            )
        ):
            method = line

        if line.startswith("Осталось:"):
            remaining = clean_text(line.split(":", 1)[1])

        if line.startswith("Стоимость:"):
            amount_text = clean_text(line.split(":", 1)[1])

    # На карточках Samruk обычного href нет.
    # Это проверенный стабильный route карточки конкретного лота.
    public_url = (
        f"https://zakup.sk.kz/#/ext?popup=item/{lot_id}/lot"
        if lot_id else search_url
    )

    low_title = title.lower()
    is_service = (
        low_title.startswith("услуг")
        or "услуги по заправке" in low_title
        or "обслуживан" in low_title
    )

    return {
        "source_code": "samruk",
        "source_tender_id": None,
        "source_lot_id": lot_id,
        "public_url": public_url,
        "title": title or "Закупка Samruk",
        "description": clean_text(text),
        "customer_name": None,
        "customer_bin": None,
        "region": None,
        "procurement_method": method or None,
        "status_code": None,
        "status_name": "Опубликовано",
        "amount": money_to_float(amount_text),
        "currency": "KZT",
        "quantity": None,
        "unit": None,
        "category": KEYWORD,
        "published_at": None,
        "expires_at": remaining_to_expires_at(remaining),
        "is_active": True,
        "raw": {
            "remaining_text": remaining,
            "amount_text": amount_text,
            "search_url": search_url,
            "is_service": is_service,
        },
    }


def save_results(rows):
    json_path = OUT / "samruk_tenders.json"
    csv_path = OUT / "samruk_tenders.csv"

    json_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    fields = [
        "source_lot_id",
        "title",
        "procurement_method",
        "amount",
        "currency",
        "expires_at",
        "public_url",
    ]

    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()

        for row in rows:
            writer.writerow({k: row.get(k) for k in fields})

    return json_path, csv_path


def main():
    print("=" * 70)
    print("TENDER RADAR KZ - SAMRUK COLLECTOR V3")
    print("MODE: READ ONLY, NO SUPABASE WRITE")
    print("=" * 70)

    driver = make_driver()

    try:
        print("OPEN:", URL)
        driver.get(URL)

        wait = WebDriverWait(driver, 40)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(4)

        print("TITLE:", driver.title)

        search_box = find_search_box(driver)

        if search_box is None:
            raise RuntimeError("Real Samruk tender search input not found")

        print("SEARCH INPUT FOUND")
        print("KEYWORD:", KEYWORD)

        search_box.clear()
        search_box.send_keys(KEYWORD)

        try:
            button = wait.until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//button[contains(normalize-space(.),'Найти')]")
                )
            )
            button.click()
        except Exception:
            search_box.send_keys(Keys.ENTER)

        print("SEARCH STARTED")

        wait.until(
            lambda d: len(
                d.find_elements(
                    By.CSS_SELECTOR,
                    "div.m-sidebar__layout--found-item"
                )
            ) > 0
        )

        time.sleep(3)

        search_url = driver.current_url
        cards = driver.find_elements(
            By.CSS_SELECTOR,
            "div.m-sidebar__layout--found-item"
        )

        print("RESULT URL:", search_url)
        print("FOUND CARDS:", len(cards))

        all_rows = []
        goods_rows = []
        seen = set()

        for card in cards:
            row = parse_card(card, search_url)
            lot_id = row.get("source_lot_id")

            if not lot_id or lot_id in seen:
                continue

            seen.add(lot_id)
            all_rows.append(row)

            if not row["raw"]["is_service"]:
                goods_rows.append(row)

            print(
                "LOT:",
                lot_id,
                "|",
                row.get("title"),
                "|",
                row.get("amount"),
                "|",
                row["raw"].get("remaining_text"),
                "| SERVICE:",
                row["raw"].get("is_service"),
            )

        (OUT / "samruk_all_results.json").write_text(
            json.dumps(all_rows, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        json_path, csv_path = save_results(goods_rows)

        driver.save_screenshot(str(OUT / "samruk_results.png"))

        print("")
        print("ALL UNIQUE RESULTS:", len(all_rows))
        print("GOODS KEPT:", len(goods_rows))
        print("SERVICES SKIPPED:", len(all_rows) - len(goods_rows))
        print("JSON:", json_path)
        print("CSV :", csv_path)

        if not goods_rows:
            raise RuntimeError("No goods rows were extracted from Samruk")

        print("")
        print("SUCCESS: Samruk real tender cards extracted")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
