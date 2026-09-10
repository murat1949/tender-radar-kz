#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Goszakup Collector v1
---------------------
Первая рабочая версия сборщика для портала мониторинга закупок.

Источник:
  Официальный GraphQL API goszakup.gov.kz v3

Что делает:
  1) ищет лоты по ключевым словам;
  2) подтягивает объявление, заказчика, БИН, сроки, сумму и контакты;
  3) нормализует данные;
  4) считает простой приоритет;
  5) сохраняет JSON/CSV;
  6) при наличии параметров Supabase — отправляет данные в таблицу tenders.

Для живого API нужен официальный токен goszakup.
Указать его в переменной среды:
  GOSZAKUP_TOKEN=...

Опционально:
  SUPABASE_URL=https://xxxx.supabase.co
  SUPABASE_SERVICE_ROLE_KEY=...
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

GRAPHQL_URL = "https://ows.goszakup.gov.kz/v3/graphql"
PUBLIC_ANNOUNCE_URL = "https://www.goszakup.gov.kz/ru/announce/index/{number_anno}"

DEFAULT_KEYWORDS = [
    "картридж",
    "тонер",
    "заправка картриджей",
    "расходные материалы для оргтехники",
]

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

QUERY = """
query SearchLots($limit: Int, $after: Int, $filter: LotsFiltersInput) {
  Lots(limit: $limit, after: $after, filter: $filter) {
    id
    lotNumber
    refLotStatusId
    count
    amount
    nameRu
    descriptionRu
    customerId
    customerBin
    customerNameRu
    trdBuyNumberAnno
    trdBuyId
    refTradeMethodsId
    refBuyTradeMethodsId
    lastUpdateDate
    indexDate
    isConstructionWork
    isLightIndustry
    disablePersonId
    Plans {
      id
      count
      refUnitsCode
      descRu
      extraDescRu
      supplyDateRu
      prepayment
    }
    Files {
      id
      filePath
      originalName
      nameRu
      indexDate
    }
    Customer {
      pid
      bin
      nameRu
      fullNameRu
      email
      phone
      website
      katoList
    }
    TrdBuy {
      id
      numberAnno
      nameRu
      totalSum
      countLots
      refTradeMethodsId
      customerBin
      customerNameRu
      orgBin
      orgNameRu
      refBuyStatusId
      startDate
      endDate
      publishDate
      isConstructionWork
      isLightIndustry
      kato
      Organizer {
        pid
        bin
        nameRu
        fullNameRu
        email
        phone
        website
        katoList
      }
      RefBuyStatus {
        id
        nameRu
        code
      }
      RefTradeMethods {
        id
        nameRu
        code
      }
    }
  }
}
"""

@dataclass
class Tender:
    source: str
    external_id: str
    lot_number: str
    announcement_number: str
    title: str
    description: str
    customer_name: str
    customer_bin: str
    organizer_name: str
    organizer_bin: str
    amount_kzt: Optional[float]
    quantity: Optional[float]
    publish_date: str
    start_date: str
    end_date: str
    status: str
    trade_method: str
    customer_phone: str
    customer_email: str
    organizer_phone: str
    organizer_email: str
    public_url: str
    keyword: str
    priority_score: int
    priority_label: str
    techspec: Dict[str, Any]
    collected_at: str

def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()

def request_graphql(token: str, variables: Dict[str, Any], timeout: int = 45) -> Dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "ProcureVision-KZ/0.1",
    }
    payload = {"query": QUERY, "variables": variables}
    r = requests.post(GRAPHQL_URL, headers=headers, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError("GraphQL error: " + json.dumps(data["errors"], ensure_ascii=False))
    return data

def safe(d: Optional[Dict[str, Any]], key: str, default: Any = "") -> Any:
    if not d:
        return default
    value = d.get(key, default)
    return default if value is None else value

def parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    candidates = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]
    cleaned = str(s).strip().replace("Z", "")
    for fmt in candidates:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None

def score_tender(lot: Dict[str, Any]) -> tuple[int, str]:
    score = 0
    buy = lot.get("TrdBuy") or {}
    customer = lot.get("Customer") or {}
    organizer = buy.get("Organizer") or {}

    amount = lot.get("amount") or buy.get("totalSum") or 0
    if amount and amount >= 1_000_000:
        score += 2
    elif amount and amount >= 500_000:
        score += 1

    end_date = parse_dt(safe(buy, "endDate"))
    now = datetime.now()
    if end_date:
        hours = (end_date - now).total_seconds() / 3600
        if 0 <= hours <= 48:
            score += 2
        elif 48 < hours <= 96:
            score += 1

    text = (safe(lot, "nameRu") + " " + safe(lot, "descriptionRu")).lower()
    brands = ["hp", "canon", "kyocera", "xerox", "pantum", "samsung", "brother", "epson"]
    if any(b in text for b in brands):
        score += 1

    if safe(customer, "phone") or safe(customer, "email") or safe(organizer, "phone") or safe(organizer, "email"):
        score += 1

    # Простая эвристика "общие основания". Надежная классификация будет отдельным модулем.
    if not lot.get("disablePersonId"):
        score += 1

    if score >= 5:
        label = "🔥 Срочно"
    elif score >= 3:
        label = "🟡 Смотреть"
    else:
        label = "⚪ В базе"
    return score, label


def build_techspec(lot: Dict[str, Any]) -> Dict[str, Any]:
    """Формирует raw.techspec для Tender Radar KZ из официальных полей Goszakup GraphQL."""
    plans = lot.get("Plans") if isinstance(lot.get("Plans"), list) else []
    files = lot.get("Files") if isinstance(lot.get("Files"), list) else []
    plan = next((x for x in plans if isinstance(x, dict)), {}) if plans else {}

    buy = lot.get("TrdBuy") or {}
    ann = str(safe(lot, "trdBuyNumberAnno") or safe(buy, "numberAnno") or "")
    lot_id = str(safe(lot, "lotNumber") or safe(lot, "id") or "")

    def first_nonempty(*values):
        for v in values:
            if v is None:
                continue
            if isinstance(v, str):
                v = v.strip()
                if v:
                    return v
            elif v != "":
                return v
        return None

    # Находим документ, который больше всего похож на техническую спецификацию.
    tech_file = None
    for f in files:
        if not isinstance(f, dict):
            continue
        blob = " ".join(str(f.get(k) or "") for k in ("nameRu", "originalName")).lower()
        if "техническ" in blob or "спецификац" in blob or "techspec" in blob:
            tech_file = f
            break
    if tech_file is None and files:
        tech_file = next((f for f in files if isinstance(f, dict)), None)

    qty = first_nonempty(plan.get("count"), lot.get("count"))
    unit = first_nonempty(plan.get("refUnitsCode"))
    short_desc = first_nonempty(plan.get("descRu"), lot.get("descriptionRu"), lot.get("nameRu"))
    extra_desc = first_nonempty(plan.get("extraDescRu"))
    delivery_period = first_nonempty(plan.get("supplyDateRu"))

    prepayment = plan.get("prepayment")
    payment_terms = None
    if prepayment is not None and str(prepayment).strip() != "":
        payment_terms = "Предоплата: {}%".format(prepayment)

    # В Lots есть список кодов мест поставки. Это не человекочитаемый адрес,
    # но сохраняем его как доступный официальный признак, не выдумывая адрес.
    kato = lot.get("plnPointKatoList")
    delivery_place = None
    if isinstance(kato, list) and kato:
        delivery_place = ", ".join(str(x) for x in kato if x is not None)

    spec = {
        "procurement_no": ann or None,
        "lot_id": lot_id or None,
        "short_description": short_desc,
        "quantity": qty,
        "unit": unit,
        "delivery_place": delivery_place,
        "delivery_terms": None,
        "delivery_period": delivery_period,
        "payment_terms": payment_terms,
        "additional_description": extra_desc,
        "technical_requirements": first_nonempty(lot.get("descriptionRu")),
        "parsed_fields": {},
    }

    if tech_file:
        spec["file"] = {
            "id": tech_file.get("id"),
            "name": first_nonempty(tech_file.get("nameRu"), tech_file.get("originalName")),
            "original_name": tech_file.get("originalName"),
            "file_path": tech_file.get("filePath"),
            "index_date": tech_file.get("indexDate"),
        }

    # Не создаём пустой блок techspec: нужен хотя бы один полезный признак.
    useful = [
        spec.get("short_description"), spec.get("quantity"), spec.get("delivery_period"),
        spec.get("additional_description"), spec.get("technical_requirements"),
        spec.get("file")
    ]
    return spec if any(v not in (None, "", {}, []) for v in useful) else {}


def normalize(lot: Dict[str, Any], keyword: str) -> Tender:
    buy = lot.get("TrdBuy") or {}
    customer = lot.get("Customer") or {}
    organizer = buy.get("Organizer") or {}
    buy_status = buy.get("RefBuyStatus") or {}
    trade_method = buy.get("RefTradeMethods") or {}

    score, label = score_tender(lot)
    ann = str(safe(lot, "trdBuyNumberAnno") or safe(buy, "numberAnno"))
    external_id = str(safe(lot, "id"))
    techspec = build_techspec(lot)

    return Tender(
        source="goszakup.gov.kz",
        external_id=external_id,
        lot_number=str(safe(lot, "lotNumber")),
        announcement_number=ann,
        title=str(safe(lot, "nameRu")),
        description=str(safe(lot, "descriptionRu")),
        customer_name=str(safe(lot, "customerNameRu") or safe(customer, "fullNameRu") or safe(customer, "nameRu") or safe(buy, "customerNameRu")),
        customer_bin=str(safe(lot, "customerBin") or safe(customer, "bin") or safe(buy, "customerBin")),
        organizer_name=str(safe(buy, "orgNameRu") or safe(organizer, "fullNameRu") or safe(organizer, "nameRu")),
        organizer_bin=str(safe(buy, "orgBin") or safe(organizer, "bin")),
        amount_kzt=lot.get("amount") or buy.get("totalSum"),
        quantity=lot.get("count"),
        publish_date=str(safe(buy, "publishDate")),
        start_date=str(safe(buy, "startDate")),
        end_date=str(safe(buy, "endDate")),
        status=str(safe(buy_status, "nameRu") or safe(buy, "refBuyStatusId")),
        trade_method=str(safe(trade_method, "nameRu") or safe(buy, "refTradeMethodsId")),
        customer_phone=str(safe(customer, "phone")),
        customer_email=str(safe(customer, "email")),
        organizer_phone=str(safe(organizer, "phone")),
        organizer_email=str(safe(organizer, "email")),
        public_url=PUBLIC_ANNOUNCE_URL.format(number_anno=ann) if ann else "",
        keyword=keyword,
        priority_score=score,
        priority_label=label,
        techspec=techspec,
        collected_at=datetime.now(timezone.utc).isoformat(),
    )

def collect_keyword(token: str, keyword: str, limit: int = 100) -> List[Tender]:
    """
    Ищем по названию + описанию.
    По документации goszakup GraphQL v3 поле nameDescriptionRu поддерживает морфологический поиск.
    """
    variables = {
        "limit": min(limit, 200),
        "after": None,
        "filter": {
            "nameDescriptionRu": keyword
        }
    }
    data = request_graphql(token, variables)
    lots = data.get("data", {}).get("Lots") or []
    return [normalize(lot, keyword) for lot in lots]

def deduplicate(items: Iterable[Tender]) -> List[Tender]:
    best: Dict[str, Tender] = {}
    for t in items:
        key = t.external_id or f"{t.announcement_number}:{t.lot_number}"
        existing = best.get(key)
        if not existing or t.priority_score > existing.priority_score:
            best[key] = t
    return sorted(
        best.values(),
        key=lambda x: (x.priority_score, x.end_date or ""),
        reverse=True
    )

def save_json(items: List[Tender]) -> Path:
    path = OUTPUT_DIR / "tenders.json"
    path.write_text(
        json.dumps([asdict(x) for x in items], ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    return path

def save_csv(items: List[Tender]) -> Path:
    path = OUTPUT_DIR / "tenders.csv"
    fields = list(Tender.__dataclass_fields__.keys())
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for item in items:
            w.writerow(asdict(item))
    return path

def upload_supabase(items: List[Tender]) -> None:
    url = env("SUPABASE_URL")
    key = env("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key or not items:
        return

    endpoint = url.rstrip("/") + "/rest/v1/tenders"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }

    payload = []
    for t in items:
        d = asdict(t)
        payload.append({
            "source": d["source"],
            "external_id": d["external_id"],
            "lot_number": d["lot_number"],
            "announcement_number": d["announcement_number"],
            "title": d["title"],
            "description": d["description"],
            "customer_name": d["customer_name"],
            "customer_bin": d["customer_bin"],
            "organizer_name": d["organizer_name"],
            "organizer_bin": d["organizer_bin"],
            "amount_kzt": d["amount_kzt"],
            "quantity": d["quantity"],
            "publish_date": d["publish_date"] or None,
            "start_date": d["start_date"] or None,
            "end_date": d["end_date"] or None,
            "status": d["status"],
            "trade_method": d["trade_method"],
            "customer_phone": d["customer_phone"],
            "customer_email": d["customer_email"],
            "organizer_phone": d["organizer_phone"],
            "organizer_email": d["organizer_email"],
            "public_url": d["public_url"],
            "keyword": d["keyword"],
            "priority_score": d["priority_score"],
            "priority_label": d["priority_label"],
            "collected_at": d["collected_at"],
        })

    r = requests.post(endpoint, headers=headers, json=payload, timeout=45)
    r.raise_for_status()

def load_keywords() -> List[str]:
    raw = env("GOSZAKUP_KEYWORDS")
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]
    return DEFAULT_KEYWORDS

def main() -> int:
    token = env("GOSZAKUP_TOKEN")
    if not token:
        print("ERROR: не указан GOSZAKUP_TOKEN.")
        print("Получите официальный токен goszakup и задайте переменную среды GOSZAKUP_TOKEN.")
        return 2

    keywords = load_keywords()
    all_items: List[Tender] = []

    print("ProcureVision KZ — Goszakup Collector v1")
    print("Ключевые слова:", ", ".join(keywords))

    for keyword in keywords:
        print(f"[search] {keyword}")
        try:
            found = collect_keyword(token, keyword)
            print(f"  найдено: {len(found)}")
            all_items.extend(found)
        except Exception as e:
            print(f"  ошибка: {e}", file=sys.stderr)
        time.sleep(0.3)

    items = deduplicate(all_items)
    json_path = save_json(items)
    csv_path = save_csv(items)

    try:
        upload_supabase(items)
        if env("SUPABASE_URL") and env("SUPABASE_SERVICE_ROLE_KEY"):
            print("Supabase: данные отправлены.")
    except Exception as e:
        print(f"Supabase error: {e}", file=sys.stderr)

    print(f"Итого уникальных лотов: {len(items)}")
    print(f"JSON: {json_path}")
    print(f"CSV : {csv_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
