#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Goszakup Collector v1.3 PDF TECHSPEC
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
  6) для активных лотов пытается скачать официальный PDF техспецификации;
  7) при успехе заменяет краткие требования полным проверенным текстом PDF;
  8) при сбое PDF сохраняет лот с исходными структурированными данными.

Для живого API нужен официальный токен goszakup.
Указать его в переменной среды:
  GOSZAKUP_TOKEN=...

Опционально:
  SUPABASE_URL=https://xxxx.supabase.co
  SUPABASE_SERVICE_ROLE_KEY=...
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from pypdf import PdfReader

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
      nameRu
      descRu
      extraDescRu
      count
      refUnitsCode
      supplyDateRu
      prepayment
      PlansKato {
        fullDeliveryPlaceNameRu
        count
      }
    }
    Files {
      id
      filePath
      originalName
      objectId
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
    techspec: Optional[Dict[str, Any]]
    collected_at: str

def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()

def request_graphql(
    token: str,
    variables: Dict[str, Any],
    timeout: int = 60,
    retries: int = 4,
) -> Dict[str, Any]:
    """GraphQL с повтором только временных сетевых/серверных ошибок."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Tender-Radar-KZ-Goszakup/1.3",
    }
    payload = {"query": QUERY, "variables": variables}
    last_error: Optional[BaseException] = None

    for attempt in range(1, retries + 1):
        try:
            r = requests.post(
                GRAPHQL_URL,
                headers=headers,
                json=payload,
                timeout=(15, timeout),
            )
            r.raise_for_status()
            data = r.json()
            if data.get("errors"):
                raise RuntimeError(
                    "GraphQL error: " + json.dumps(data["errors"], ensure_ascii=False)
                )
            return data
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            print(f"  GraphQL temporary network error {attempt}/{retries}: {exc}", file=sys.stderr)
        except requests.HTTPError as exc:
            last_error = exc
            status = getattr(exc.response, "status_code", None)
            if status not in (408, 425, 429, 500, 502, 503, 504):
                raise
            print(f"  GraphQL temporary HTTP {status} {attempt}/{retries}", file=sys.stderr)

        if attempt < retries:
            time.sleep(3 * attempt)

    raise RuntimeError(f"Goszakup GraphQL unavailable after {retries} attempts: {last_error!r}")

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

def _compact_text(v: Any) -> str:
    return " ".join(str(v or "").replace("\r", " ").replace("\n", " ").split())

def _uniq_join(values: Iterable[Any], sep: str = "; ") -> str:
    out: List[str] = []
    seen = set()
    for v in values:
        s = _compact_text(v)
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return sep.join(out)

def parse_product_fields(text: str) -> Dict[str, str]:
    """Небольшой безопасный разбор товарных характеристик из открытого текста API."""
    src = _compact_text(text)
    low = src.lower()
    out: Dict[str, str] = {}

    if "тонер" in low:
        out["ink_type"] = "тонер"
    elif "чернил" in low:
        out["ink_type"] = "чернила"

    colors = [
        (r"\bчерн(?:ый|ого|ая|ое)?\b|\bblack\b", "черный"),
        (r"\bголуб(?:ой|ого|ая|ое)?\b|\bcyan\b", "голубой"),
        (r"\bпурпурн(?:ый|ого|ая|ое)?\b|\bmagenta\b", "пурпурный"),
        (r"\bжелт(?:ый|ого|ая|ое)?\b|\byellow\b", "желтый"),
    ]
    for pat, label in colors:
        if re.search(pat, low, re.I):
            out["color"] = label
            break

    # Совместимость: бренд + модель или фраза после "для принтера/МФУ".
    m = re.search(r"(?:для\s+(?:принтера|мфу)\s+)([^.;,]{3,90})", src, re.I)
    if m:
        out["compatibility"] = _compact_text(m.group(1))
    else:
        m = re.search(r"\b(HP|Canon|Kyocera|Xerox|Pantum|Samsung|Brother|Epson)\b\s+([A-Za-z0-9][A-Za-z0-9+_.\-/ ]{1,45})", src, re.I)
        if m:
            out["compatibility"] = _compact_text(m.group(1) + " " + m.group(2)).rstrip(' .;,')

    m = re.search(r"\b(\d[\d\s.,]*)\s*(стр(?:аниц(?:ы|а)?)?|pages?|мл|ml|г|гр|kg|кг)\b", low, re.I)
    if m:
        out["yield_or_volume"] = _compact_text(m.group(1) + " " + m.group(2))

    if src:
        out["purpose"] = src[:180]
    return out

def build_techspec(lot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Строит raw.techspec из структурированных полей GraphQL v3.

    Важно: это не OCR/PDF-парсер. На этом шаге используем официальные открытые
    поля лота/пункта плана и метаданные документов. Сам PDF сохраняется в files.
    """
    plans = lot.get("Plans") if isinstance(lot.get("Plans"), list) else []
    plan = next((x for x in plans if isinstance(x, dict)), {})
    files = lot.get("Files") if isinstance(lot.get("Files"), list) else []
    files = [x for x in files if isinstance(x, dict)]

    ann = str(safe(lot, "trdBuyNumberAnno") or safe(lot.get("TrdBuy") or {}, "numberAnno"))
    lot_no = str(safe(lot, "lotNumber") or safe(lot, "id"))
    title = _compact_text(safe(lot, "nameRu"))
    lot_desc = _compact_text(safe(lot, "descriptionRu"))
    short_desc = _compact_text(plan.get("descRu") or plan.get("nameRu") or lot_desc or title)
    extra_desc = _compact_text(plan.get("extraDescRu"))

    places = []
    for k in (plan.get("PlansKato") if isinstance(plan.get("PlansKato"), list) else []):
        if isinstance(k, dict):
            places.append(k.get("fullDeliveryPlaceNameRu"))
    delivery_place = _uniq_join(places)

    quantity = plan.get("count") if plan.get("count") is not None else lot.get("count")
    unit = _compact_text(plan.get("refUnitsCode"))
    # Госзакуп код 796 = штука. Текущий UI показывает unit как текст,
    # поэтому нормализуем здесь, чтобы не было "3 796".
    if unit == "796":
        unit = "шт."
    delivery_period = _compact_text(plan.get("supplyDateRu"))
    prepayment = plan.get("prepayment")
    payment_terms = ""
    if prepayment is not None and str(prepayment).strip() != "":
        payment_terms = "Предоплата: %s%%" % prepayment

    tech_files = []
    for f in files:
        label = _compact_text(f.get("nameRu") or f.get("originalName"))
        orig = _compact_text(f.get("originalName"))
        is_tech = ("техничес" in label.lower() or "техспец" in label.lower() or
                   "techspec" in label.lower() or "tech_spec" in label.lower() or
                   "техничес" in orig.lower() or "techspec" in orig.lower())
        tech_files.append({
            "id": f.get("id"),
            "object_id": f.get("objectId"),
            "name": label or orig,
            "original_name": orig,
            "file_path": f.get("filePath"),
            "index_date": f.get("indexDate"),
            "is_techspec": bool(is_tech),
        })

    requirements = _uniq_join([lot_desc, extra_desc], sep="\n")
    parsed = parse_product_fields(_uniq_join([title, lot_desc, short_desc, extra_desc]))

    # Не создаем пустую фиктивную техспецификацию: нужен хотя бы один
    # содержательный источник (план, описание или документы).
    if not (plan or lot_desc or files):
        return None

    return {
        "source": "goszakup_graphql_v3",
        "procurement_no": ann or None,
        "lot_id": lot_no or None,
        "short_description": short_desc or None,
        "quantity": quantity,
        "unit": unit or None,
        "delivery_place": delivery_place or None,
        "delivery_terms": None,
        "delivery_period": delivery_period or None,
        "payment_terms": payment_terms or None,
        "additional_description": extra_desc or None,
        "technical_requirements": requirements or None,
        "parsed_fields": parsed,
        "files": tech_files,
        "has_techspec_file": any(x.get("is_techspec") for x in tech_files),
    }


def _normalize_lot(value: Any) -> str:
    """Нормализация визуально похожих символов в номере лота."""
    text = _compact_text(value).upper()
    text = text.translate(str.maketrans({
        "З": "3",
        "–": "-",
        "—": "-",
        "−": "-",
        "‑": "-",
        "‐": "-",
    }))
    return re.sub(r"\s+", "", text)


def _lot_prefix(value: Any) -> str:
    m = re.search(r"\d{6,}", _normalize_lot(value))
    return m.group(0) if m else ""


def _normalize_pdf_text(value: Any) -> str:
    text = str(value or "").replace("\u00ad", "").replace("\u200b", "")
    return " ".join(text.lower().split())


def _looks_like_pdf(content: bytes, content_type: str) -> bool:
    return content[:16].startswith(b"%PDF") or "pdf" in (content_type or "").lower()


def _pdf_urls(file_path: Any) -> List[str]:
    path = _compact_text(file_path)
    if not path:
        return []
    if path.startswith(("http://", "https://")):
        return [path]

    relative = path if path.startswith("/") else "/" + path
    # Первый адрес — тот же официальный хост, на котором контрольный тест уже прошёл.
    bases = (
        "https://ows.goszakup.gov.kz",
        "https://goszakup.gov.kz",
        "https://www.goszakup.gov.kz",
        "https://procurement.gov.kz",
    )
    return list(dict.fromkeys(urljoin(base, relative) for base in bases))


def _pdf_file_score(file_info: Dict[str, Any], announcement_number: str) -> int:
    name = _compact_text(file_info.get("name"))
    orig = _compact_text(file_info.get("original_name"))
    path = _compact_text(file_info.get("file_path"))
    blob = " ".join([name, orig, path]).lower()
    score = 0
    if file_info.get("is_techspec"):
        score += 100
    if "techspec" in blob or "tech_spec" in blob:
        score += 80
    if "техничес" in blob or "спецификац" in blob or "техспец" in blob:
        score += 60
    if ".pdf" in blob:
        score += 30
    ann_head = str(announcement_number or "").split("-")[0]
    if ann_head and ann_head in blob:
        score += 20
    if "договор" in blob or "contract" in blob:
        score -= 100
    return score


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    text = text.replace("\u00ad", "").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _verify_pdf_identity(text: str, announcement_number: str, lot_number: str) -> Dict[str, bool]:
    haystack = _normalize_pdf_text(text)
    ann = _normalize_pdf_text(announcement_number)
    prefix = _normalize_pdf_text(_lot_prefix(lot_number))
    return {
        "announcement": bool(ann and ann in haystack),
        "lot_prefix": bool(prefix and prefix in haystack),
    }


def _download_pdf(
    session: requests.Session,
    file_info: Dict[str, Any],
    token: str,
) -> Tuple[Optional[bytes], Optional[str], str, bool]:
    """Возвращает (bytes, final_url, error, had_network_error). Без долгих повторов."""
    last_error = ""
    had_network_error = False
    urls = _pdf_urls(file_info.get("file_path"))
    if not urls:
        return None, None, "empty file_path", False

    for url in urls:
        for authorized in (True, False):
            headers = {"User-Agent": "Tender-Radar-KZ-Goszakup/1.3"}
            if authorized:
                headers["Authorization"] = f"Bearer {token}"
            try:
                r = session.get(
                    url,
                    headers=headers,
                    timeout=(8, 22),
                    allow_redirects=True,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                had_network_error = True
                last_error = f"network: {type(exc).__name__}"
                # Не повторяем тот же URL бесконечно: следующий час даст новую попытку.
                break
            except requests.RequestException as exc:
                last_error = f"request: {type(exc).__name__}"
                break

            if r.ok and _looks_like_pdf(r.content, r.headers.get("content-type", "")):
                return r.content, r.url, "", had_network_error

            last_error = f"HTTP {r.status_code}, type={r.headers.get('content-type', '')}"
            # Для обычного 404/400 второй вариант заголовков бессмыслен.
            if r.status_code not in (401, 403):
                break

    return None, None, last_error or "PDF download failed", had_network_error


def _tender_is_active(tender: Tender) -> bool:
    status = str(tender.status or "").lower()
    if "прием" in status or "приём" in status:
        return True
    end_date = parse_dt(tender.end_date)
    return bool(end_date and end_date > datetime.now())


def enrich_pdf_techspecs(items: List[Tender], token: str) -> Dict[str, int]:
    """Добавляет полный PDF-текст активным лотам, не ломая сбор при сбое PDF.

    Важное правило: PDF — обогащение. Если Госзакуп временно не отдаёт файл,
    сам лот всё равно остаётся в tenders.json с GraphQL-техспецификацией.
    """
    enabled = env("GOSZAKUP_PDF_ENABLED", "1").lower() not in ("0", "false", "no", "off")
    limit_raw = env("GOSZAKUP_PDF_LIMIT", "0")
    breaker_raw = env("GOSZAKUP_PDF_BREAKER", "5")
    try:
        limit = max(0, int(limit_raw))
    except ValueError:
        limit = 0
    try:
        breaker = max(1, int(breaker_raw))
    except ValueError:
        breaker = 5

    stats = {
        "active": 0,
        "eligible": 0,
        "attempted": 0,
        "extracted": 0,
        "failed": 0,
        "breaker_stopped": 0,
    }
    if not enabled:
        print("[pdf] enrichment disabled by GOSZAKUP_PDF_ENABLED")
        return stats

    active_items = [x for x in items if _tender_is_active(x)]
    stats["active"] = len(active_items)
    if limit:
        active_items = active_items[:limit]

    session = requests.Session()
    consecutive_network_failures = 0

    print(f"[pdf] active lots selected: {len(active_items)}")

    for pos, tender in enumerate(active_items, 1):
        tech = tender.techspec if isinstance(tender.techspec, dict) else None
        if not tech:
            continue
        files = tech.get("files") if isinstance(tech.get("files"), list) else []
        candidates = [x for x in files if isinstance(x, dict) and x.get("file_path")]
        if not candidates:
            tech["pdf_extraction_attempted"] = False
            tech["pdf_extracted"] = False
            tech["pdf_error"] = "no document file_path in GraphQL"
            continue

        stats["eligible"] += 1
        tech["pdf_extraction_attempted"] = True
        stats["attempted"] += 1
        candidates = sorted(
            candidates,
            key=lambda x: _pdf_file_score(x, tender.announcement_number),
            reverse=True,
        )[:3]

        accepted_text = ""
        accepted_file: Optional[Dict[str, Any]] = None
        accepted_url: Optional[str] = None
        accepted_checks: Dict[str, bool] = {}
        errors: List[str] = []
        lot_network_error = False

        for file_info in candidates:
            content, final_url, error, network_error = _download_pdf(session, file_info, token)
            lot_network_error = lot_network_error or network_error
            if content is None:
                errors.append(f"{file_info.get('original_name') or file_info.get('name')}: {error}")
                continue

            try:
                text = _extract_pdf_text(content)
            except Exception as exc:
                errors.append(
                    f"{file_info.get('original_name') or file_info.get('name')}: extract {type(exc).__name__}"
                )
                continue

            if len(text) < 50:
                errors.append(
                    f"{file_info.get('original_name') or file_info.get('name')}: extracted text too short ({len(text)})"
                )
                continue

            checks = _verify_pdf_identity(
                text,
                tender.announcement_number,
                tender.lot_number,
            )
            if not (checks.get("announcement") and checks.get("lot_prefix")):
                errors.append(
                    f"{file_info.get('original_name') or file_info.get('name')}: identity {checks}"
                )
                continue

            accepted_text = text
            accepted_file = file_info
            accepted_url = final_url
            accepted_checks = checks
            break

        if accepted_text and accepted_file:
            previous_requirements = str(tech.get("technical_requirements") or "").strip()
            if previous_requirements and not tech.get("structured_requirements"):
                tech["structured_requirements"] = previous_requirements

            tech["technical_requirements"] = accepted_text
            tech["source"] = "goszakup_pdf_verified"
            tech["pdf_extracted"] = True
            tech["pdf_text_length"] = len(accepted_text)
            tech["pdf_source"] = accepted_url
            tech["pdf_original_name"] = accepted_file.get("original_name") or accepted_file.get("name")
            tech["pdf_object_id"] = accepted_file.get("object_id")
            tech["pdf_verification"] = accepted_checks
            tech["pdf_error"] = None
            tech["has_techspec_file"] = True

            # Поля, распознанные из полного PDF, дополняют, но не стирают GraphQL-разбор.
            parsed = tech.get("parsed_fields") if isinstance(tech.get("parsed_fields"), dict) else {}
            pdf_parsed = parse_product_fields(accepted_text)
            for key, value in pdf_parsed.items():
                if value and not parsed.get(key):
                    parsed[key] = value
            tech["parsed_fields"] = parsed

            stats["extracted"] += 1
            consecutive_network_failures = 0
            print(
                f"[pdf {pos}/{len(active_items)}] OK lot={tender.lot_number} "
                f"chars={len(accepted_text)} file={tech.get('pdf_original_name')}"
            )
        else:
            tech["pdf_extracted"] = False
            tech["pdf_error"] = " | ".join(errors)[:1200] or "no verified PDF text"
            stats["failed"] += 1
            if lot_network_error:
                consecutive_network_failures += 1
            else:
                consecutive_network_failures = 0
            print(
                f"[pdf {pos}/{len(active_items)}] SKIP lot={tender.lot_number}: "
                f"{tech['pdf_error'][:220]}"
            )

            # Если сам файловый сервер Госзакупа массово недоступен, не ждём таймаут
            # на каждом из сотни лотов. Через час workflow попробует снова.
            if consecutive_network_failures >= breaker:
                stats["breaker_stopped"] = len(active_items) - pos
                print(
                    f"[pdf] circuit breaker: {consecutive_network_failures} consecutive "
                    f"network failures; remaining {stats['breaker_stopped']} lots keep GraphQL techspec"
                )
                break

    return stats


def normalize(lot: Dict[str, Any], keyword: str) -> Tender:
    buy = lot.get("TrdBuy") or {}
    customer = lot.get("Customer") or {}
    organizer = buy.get("Organizer") or {}
    buy_status = buy.get("RefBuyStatus") or {}
    trade_method = buy.get("RefTradeMethods") or {}

    score, label = score_tender(lot)
    ann = str(safe(lot, "trdBuyNumberAnno") or safe(buy, "numberAnno"))
    external_id = str(safe(lot, "id"))

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
        techspec=build_techspec(lot),
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

# Supabase upload is intentionally handled only by auto_collect_and_sync.py.
# Keeping the collector read/transform-only avoids a duplicate write path and
# schema mismatch with the production `tenders` table.

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

    print("ProcureVision KZ — Goszakup Collector v1.3 PDF TECHSPEC")
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

    # Производственное PDF-обогащение выполняется только после дедупликации,
    # поэтому один и тот же лот не скачивается повторно из-за нескольких ключевых слов.
    pdf_stats = enrich_pdf_techspecs(items, token)

    json_path = save_json(items)
    csv_path = save_csv(items)

    print("Supabase upload: skipped here; auto_collect_and_sync.py owns production sync.")

    print(f"Итого уникальных лотов: {len(items)}")
    print(
        "PDF: active={active} eligible={eligible} attempted={attempted} "
        "extracted={extracted} failed={failed} breaker_stopped={breaker_stopped}".format(**pdf_stats)
    )
    print(f"JSON: {json_path}")
    print(f"CSV : {csv_path}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
