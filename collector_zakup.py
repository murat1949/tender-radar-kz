# -*- coding: utf-8 -*-
"""
Tender Radar KZ (радар Айдара) — сборщик Госзакупа через Единую платформу закупок.

С осени 2026 старый вход ows.goszakup.gov.kz отвечает с перебоями (403, обрывы
соединения). Новая платформа zakup.gov.kz отдаёт те же лоты через открытый API
без токена: https://zakup.gov.kz/api/core/api/public/lots/

Поиск: параметр q ищет по названию лота, самые свежие первыми.
Берём только активные статусы:
  6 — «Опубликован», 7 — «Опубликован (прием заявок)»,
  9 — «Опубликован (прием ценовых предложений)».
Номера лотов совпадают со старым порталом (external_id), поэтому записи
ложатся в базу без дублей с теми, что собирал старый сборщик.

Выход: список словарей в формате tenders.json, который читает
auto_collect_and_sync_commercial.make_goszakup().
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

import requests

API_LOTS = "https://zakup.gov.kz/api/core/api/public/lots/"
ACTIVE_STATUS_IDS = (6, 7, 9)
PAGE_SIZE = 100
# Ссылки старого портала перенаправляются на новую платформу (структура сохранена).
PUBLIC_ANNOUNCE_URL = "https://www.goszakup.gov.kz/ru/announce/index/{announcement_id}"
HEADERS = {"User-Agent": "Mozilla/5.0 (TenderRadarKZ)", "Accept": "application/json"}
PAUSE_SECONDS = 0.15


class ZakupUnavailable(RuntimeError):
    pass


def _get(params: Dict[str, Any], attempts: int = 4) -> Dict[str, Any]:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            r = requests.get(API_LOTS, params=params, headers=HEADERS, timeout=(20, 60))
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                last = RuntimeError(f"HTTP {r.status_code}")
            else:
                raise ZakupUnavailable(f"HTTP {r.status_code}: {r.text[:200]}")
        except (requests.RequestException, ValueError) as exc:
            last = exc
        time.sleep(2 * attempt)
    raise ZakupUnavailable(f"zakup.gov.kz unavailable after {attempts} attempts: {last!r}")


def _future(value: Any, now: datetime) -> bool:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.tzinfo is not None and dt > now
    except (TypeError, ValueError):
        return False


def _join(*parts: Any) -> str:
    seen, out = set(), []
    for p in parts:
        s = str(p or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return ". ".join(out)


def to_record(lot: Dict[str, Any], keyword: str) -> Dict[str, Any]:
    customers = lot.get("customers") or []
    customer = customers[0] if customers else {}
    enstrus = lot.get("enstrus") or []
    addresses = lot.get("delivery_addresses") or []
    status = lot.get("status") or {}
    ext = lot.get("external_id")
    ann_ext = lot.get("announcement_external_id")
    return {
        "source": "zakup.gov.kz",
        "external_id": str(ext) if ext else f"zk{lot.get('id')}",
        "lot_number": str(lot.get("lot_number") or ""),
        "announcement_number": str(lot.get("announcement_number") or ""),
        "title": str(lot.get("name_ru") or ""),
        # В описание кладём и название объявления, и описание по ЕНС ТРУ —
        # так совпадение по словам профиля ищется шире, чем по одному названию.
        "description": _join(
            lot.get("description_ru"),
            lot.get("announcement_name_ru"),
            *[_join(e.get("name_ru"), e.get("short_description_ru")) for e in enstrus],
        ),
        "customer_name": str(customer.get("name_ru") or lot.get("organization_name") or ""),
        "customer_bin": str(customer.get("bin") or ""),
        "organizer_name": str(lot.get("organization_name") or ""),
        "organizer_bin": "",
        "amount_kzt": lot.get("total_price"),
        "quantity": lot.get("quantity"),
        "publish_date": str(lot.get("announcement_publish_date") or ""),
        "start_date": str(lot.get("offer_start_date") or ""),
        "end_date": str(lot.get("offer_end_date") or ""),
        "status": str(lot.get("status_name") or status.get("name") or ""),
        "status_code": str(status.get("id") or ""),
        "trade_method": str(lot.get("purchase_method_name") or ""),
        "region": str(addresses[0].get("name_ru") or "") if addresses else "",
        "category": str(enstrus[0].get("name_ru") or "") if enstrus else "",
        "customer_phone": "",
        "customer_email": "",
        "organizer_phone": "",
        "organizer_email": "",
        "public_url": PUBLIC_ANNOUNCE_URL.format(announcement_id=ann_ext) if ann_ext else "https://zakup.gov.kz/",
        "keyword": keyword,
        "priority_score": 0,
        "priority_label": "",
        "techspec": None,
        "zakup_lot_id": lot.get("id"),
        "zakup_status_id": status.get("id"),
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }


def collect_keyword(keyword: str, limit: int = 600, system_id: int = 1) -> List[Dict[str, Any]]:
    """Открытые лоты по слову: активные статусы, срок подачи ещё не прошёл.
    system_id: 1 — Госзакуп (ЭГЗ), 3 — Самрук-Казына (SKK)."""
    now = datetime.now(timezone.utc)
    found: Dict[str, Dict[str, Any]] = {}
    for status_id in ACTIVE_STATUS_IDS:
        offset = 0
        while offset < limit:
            page = _get({"q": keyword, "status_id": status_id, "system_id": system_id,
                         "limit": PAGE_SIZE, "offset": offset})
            results = page.get("results") or []
            for lot in results:
                if _future(lot.get("offer_end_date"), now):
                    rec = to_record(lot, keyword)
                    found.setdefault(rec["external_id"], rec)
            time.sleep(PAUSE_SECONDS)
            offset += PAGE_SIZE
            # Свежие идут первыми: если на странице не осталось ни одного
            # открытого лота, дальше только старые — останавливаемся.
            if len(results) < PAGE_SIZE or not any(_future(l.get("offer_end_date"), now) for l in results):
                break
    return list(found.values())


def deduplicate(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    best: Dict[str, Dict[str, Any]] = {}
    for item in items:
        best.setdefault(item["external_id"], item)
    return list(best.values())


def stem(word: str) -> str:
    """Простое отсечение окончания: «освещение» → «освещени» (найдёт «освещения»)."""
    w = str(word or "").strip().lower().replace("ё", "е")
    return w[:-2] if len(w) >= 6 else w


def text_of(rec: Dict[str, Any]) -> str:
    return " ".join([rec.get("title", ""), rec.get("description", ""),
                     rec.get("category", "")]).lower().replace("ё", "е")


def crawl_recent(keywords: List[str], hours: float = 8, max_pages: int = 150,
                 budget_seconds: float = 600, system_id: int = 1) -> List[Dict[str, Any]]:
    """Пролистать все свежие лоты (новые первыми) и оставить открытые,
    где встречается любое слово профиля — в названии лота, объявления или описании."""
    now = datetime.now(timezone.utc)
    stems = [(k, stem(k)) for k in keywords if stem(k)]
    border = now.timestamp() - hours * 3600
    started = time.monotonic()
    found: Dict[str, Dict[str, Any]] = {}
    for page_no in range(max_pages):
        if time.monotonic() - started > budget_seconds:
            break
        page = _get({"limit": PAGE_SIZE, "offset": page_no * PAGE_SIZE, "system_id": system_id})
        results = page.get("results") or []
        oldest = None
        for lot in results:
            try:
                oldest = datetime.fromisoformat(str(lot.get("announcement_publish_date"))).timestamp()
            except (TypeError, ValueError):
                pass
            st = (lot.get("status") or {}).get("id")
            if st not in ACTIVE_STATUS_IDS or not _future(lot.get("offer_end_date"), now):
                continue
            rec = to_record(lot, "")
            text = text_of(rec)
            hit = [k for k, s in stems if s in text]
            if hit:
                rec["keyword"] = hit[0]
                found.setdefault(rec["external_id"], rec)
        time.sleep(PAUSE_SECONDS)
        if len(results) < PAGE_SIZE or (oldest is not None and oldest < border):
            break
    return list(found.values())


# ---------------- Самрук-Казына (system_id = 3) ----------------
SAMRUK_SYSTEM_ID = 3
SAMRUK_LOT_URL = "https://zakup.sk.kz/#/ext(popup:item/{lot_id}/lot)?tabs=lot"


def samruk_row(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Запись zakup.gov.kz (system 3) -> строка commercial.tenders с source_code=samruk.
    external_id совпадает с номером лота на zakup.sk.kz, поэтому дублей со старым сборщиком нет."""
    lot_id = str(rec["external_id"])
    return {
        "source_code": "samruk",
        "source_tender_id": rec.get("announcement_number") or None,
        "source_lot_id": lot_id,
        "public_url": SAMRUK_LOT_URL.format(lot_id=lot_id),
        "title": rec.get("title") or "",
        "description": rec.get("description") or "",
        "customer_name": rec.get("customer_name") or "",
        "customer_bin": rec.get("customer_bin") or None,
        "region": rec.get("region") or None,
        "procurement_method": rec.get("trade_method") or None,
        "status_name": "Опубликовано",
        "amount": rec.get("amount_kzt"),
        "currency": "KZT",
        "quantity": rec.get("quantity"),
        "unit": None,
        "category": None,
        "published_at": rec.get("publish_date") or None,
        "started_at": rec.get("start_date") or None,
        "expires_at": rec.get("end_date") or None,
        "is_active": True,
        "raw": {"via": "zakup.gov.kz", "system_id": SAMRUK_SYSTEM_ID,
                "zakup_lot_id": rec.get("zakup_lot_id"), "status": rec.get("status"),
                "detail_checked": True, "acceptance_deadline_checked": bool(rec.get("end_date")),
                "keyword": rec.get("keyword")},
    }


def collect_samruk(keywords: List[str], hours: float = 8) -> List[Dict[str, Any]]:
    """Открытые лоты Самрука по словам профилей: поиск по названию + свежие за последние часы."""
    found: Dict[str, Dict[str, Any]] = {}
    failed = 0
    for kw in keywords:
        try:
            for rec in collect_keyword(kw, system_id=SAMRUK_SYSTEM_ID):
                found.setdefault(rec["external_id"], rec)
        except Exception:
            failed += 1
    if keywords and failed == len(keywords):
        raise ZakupUnavailable("zakup.gov.kz (Samruk) did not answer for any keyword")
    try:
        for rec in crawl_recent(keywords, hours=hours, system_id=SAMRUK_SYSTEM_ID):
            found.setdefault(rec["external_id"], rec)
    except Exception:
        pass
    return [samruk_row(r) for r in found.values()]
