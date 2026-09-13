#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Goszakup Collector v1.2 TECHSPEC RESTORE
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

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

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
    import re
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

_PDF_TECHSPEC_CACHE: Dict[str, Dict[str, Any]] = {}


def _tech_file_score(f: Dict[str, Any]) -> int:
    blob = " ".join(_compact_text(f.get(k)).lower() for k in ("nameRu", "nameKz", "originalName", "filePath"))
    score = 0
    if "техничес" in blob:
        score += 10
    if "спецификац" in blob:
        score += 10
    if "techspec" in blob or "tech_spec" in blob:
        score += 10
    if ".pdf" in blob:
        score += 2
    if "договор" in blob:
        score -= 5
    return score


def _choose_tech_file(files: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    candidates = [f for f in files if isinstance(f, dict)]
    if not candidates:
        return None
    candidates.sort(key=_tech_file_score, reverse=True)
    best = candidates[0]
    return best if _tech_file_score(best) > 0 else None


def _pdf_url_candidates(file_path: str) -> List[str]:
    p = _compact_text(file_path)
    if not p:
        return []
    if p.startswith("http://") or p.startswith("https://"):
        return [p]
    pp = p if p.startswith("/") else "/" + p
    bases = (
        "https://ows.goszakup.gov.kz",
        "https://goszakup.gov.kz",
        "https://www.goszakup.gov.kz",
        "https://procurement.gov.kz",
        "https://old.goszakup.gov.kz",
        "https://zakup.gov.kz",
    )
    out: List[str] = []
    seen = set()
    for base in bases:
        u = urljoin(base, pp)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _looks_like_pdf(content: bytes, content_type: str) -> bool:
    return content[:16].startswith(b"%PDF") or "pdf" in (content_type or "").lower()


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    if PdfReader is None:
        raise RuntimeError("pypdf is not installed")
    reader = PdfReader(io.BytesIO(pdf_bytes))
    parts: List[str] = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    value = "\n".join(parts).replace("\u00ad", "").replace("\u200b", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _russian_pdf_part(text: str) -> str:
    # Госзакуп часто отдаёт двуязычный PDF: сначала казахская, затем русская часть.
    m = re.search(r"(?i)Техническая\s+спецификация", text)
    return text[m.start():] if m else text


def _extract_requirements_from_pdf(text: str) -> str:
    ru = _russian_pdf_part(text)
    flat = _compact_text(ru)
    patterns = [
        r"Описание и требуемые функциональные, технические, качественные и эксплуатационные характеристики закупаемых товаров:\s*(.+)$",
        r"характеристики закупаемых товаров:\s*(.+)$",
        r"технические требования:\s*(.+)$",
    ]
    for pat in patterns:
        m = re.search(pat, flat, re.I | re.S)
        if m:
            value = _compact_text(m.group(1))
            if value:
                return value[:12000]
    # Fallback: сохраняем русскую часть, но ограничиваем объём для карточки/БД.
    return flat[:12000]


def _download_and_parse_techspec(tech_file: Dict[str, Any]) -> Dict[str, Any]:
    """Безопасно скачивает и разбирает PDF. Любая ошибка возвращается в meta и не роняет сбор."""
    file_path = _compact_text(tech_file.get("filePath"))
    cache_key = file_path or str(tech_file.get("id") or tech_file.get("objectId") or "")
    if cache_key and cache_key in _PDF_TECHSPEC_CACHE:
        return _PDF_TECHSPEC_CACHE[cache_key]

    result: Dict[str, Any] = {
        "ok": False,
        "source_url": None,
        "text": None,
        "requirements": None,
        "error": None,
    }
    if PdfReader is None:
        result["error"] = "pypdf_not_installed"
        if cache_key:
            _PDF_TECHSPEC_CACHE[cache_key] = result
        return result

    token = os.getenv("GOSZAKUP_TOKEN", "").strip()
    session = requests.Session()
    last_error = None
    for url in _pdf_url_candidates(file_path):
        for headers in (
            {"Authorization": f"Bearer {token}", "User-Agent": "Tender-Radar-KZ/0.4.3"} if token else {"User-Agent": "Tender-Radar-KZ/0.4.3"},
            {"User-Agent": "Mozilla/5.0 Tender-Radar-KZ/0.4.3"},
        ):
            try:
                r = session.get(url, headers=headers, timeout=45, allow_redirects=True)
                if r.ok and _looks_like_pdf(r.content, r.headers.get("content-type", "")):
                    pdf_text = _extract_pdf_text(r.content)
                    req = _extract_requirements_from_pdf(pdf_text)
                    result.update({
                        "ok": bool(pdf_text),
                        "source_url": r.url,
                        "text": pdf_text[:30000] if pdf_text else None,
                        "requirements": req or None,
                        "error": None,
                    })
                    if cache_key:
                        _PDF_TECHSPEC_CACHE[cache_key] = result
                    return result
                last_error = f"HTTP {r.status_code}, content-type={r.headers.get('content-type','')}"
            except Exception as e:
                last_error = repr(e)

    result["error"] = last_error or "pdf_download_failed"
    if cache_key:
        _PDF_TECHSPEC_CACHE[cache_key] = result
    return result


def build_techspec(lot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Строит raw.techspec из GraphQL v3 и, если доступен, текста официального PDF."""
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
            "name": label or orig,
            "original_name": orig,
            "file_path": f.get("filePath"),
            "index_date": f.get("indexDate"),
            "is_techspec": bool(is_tech),
        })

    pdf_info: Dict[str, Any] = {}
    selected_tech_file = _choose_tech_file(files)
    if selected_tech_file:
        pdf_info = _download_and_parse_techspec(selected_tech_file)

    pdf_requirements = _compact_text(pdf_info.get("requirements")) if pdf_info else ""
    requirements = pdf_requirements or _uniq_join([lot_desc, extra_desc], sep="\n")
    parse_source = _uniq_join([title, lot_desc, short_desc, extra_desc, pdf_requirements])
    parsed = parse_product_fields(parse_source)

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
        "pdf_downloaded": bool(pdf_info.get("ok")) if pdf_info else False,
        "pdf_source_url": pdf_info.get("source_url") if pdf_info else None,
        "pdf_text": pdf_info.get("text") if pdf_info and pdf_info.get("ok") else None,
        "pdf_error": pdf_info.get("error") if pdf_info and not pdf_info.get("ok") else None,
    }

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

    print("ProcureVision KZ — Goszakup Collector v1.2 TECHSPEC RESTORE")
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
