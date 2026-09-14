#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tender Radar KZ — Goszakup PDF Techspec TEST
=============================================

Изолированный контрольный тест. НЕ пишет в Supabase и НЕ меняет рабочие данные.

Цель:
1) через официальный GraphQL v3 найти контрольный лот 87884071-3ЦП1;
2) получить метаданные Files (включая filePath);
3) найти файл технической спецификации;
4) скачать PDF;
5) извлечь из PDF текст через pypdf;
6) проверить контрольные признаки: 35A / CB435A / 1500 / P1005 / P1006.

Результаты сохраняются в output/:
- goszakup_pdf_test_result.json
- goszakup_pdf_test_text.txt
- goszakup_control_87884071.pdf (если PDF удалось скачать)
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from pypdf import PdfReader

GRAPHQL_URL = "https://ows.goszakup.gov.kz/v3/graphql"
CONTROL_LOT = os.getenv("CONTROL_LOT", "87884071-3ЦП1").strip()
CONTROL_LOT_PREFIX = "87884071"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

QUERY = r"""
query FindLot($limit: Int, $after: Int, $filter: LotsFiltersInput) {
  Lots(limit: $limit, after: $after, filter: $filter) {
    id
    lotNumber
    nameRu
    descriptionRu
    trdBuyNumberAnno
    trdBuyId
    count
    amount
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
      nameKz
      indexDate
      systemId
    }
    TrdBuy {
      id
      numberAnno
      nameRu
      startDate
      endDate
      publishDate
    }
  }
}
"""


def compact(v: Any) -> str:
    return " ".join(str(v or "").replace("\r", " ").replace("\n", " ").split())


def gql(token: str, variables: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    GraphQL with automatic retry for temporary Goszakup network stalls.

    The endpoint has already shown both ConnectTimeout and ReadTimeout in
    GitHub Actions, while the same control query succeeds at other times.
    We therefore retry only transport-level failures; HTTP/GraphQL errors
    are still surfaced immediately.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Tender-Radar-KZ-Goszakup-PDF-Test/1.1-retry",
    }

    max_attempts = 4
    waits = [5, 15, 30]
    last_error: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        try:
            print(f"[GraphQL] request attempt {attempt}/{max_attempts}", flush=True)
            r = requests.post(
                GRAPHQL_URL,
                headers=headers,
                json={"query": QUERY, "variables": variables},
                timeout=(20, 90),  # connect timeout, read timeout
            )
            r.raise_for_status()
            data = r.json()
            if data.get("errors"):
                raise RuntimeError("GraphQL error: " + json.dumps(data["errors"], ensure_ascii=False))
            print(f"[GraphQL] request attempt {attempt}: OK", flush=True)
            return data.get("data", {}).get("Lots") or []

        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError) as e:
            last_error = e
            print(f"[GraphQL] temporary network error on attempt {attempt}: {repr(e)}",
                  file=sys.stderr, flush=True)
            if attempt < max_attempts:
                pause = waits[attempt - 1]
                print(f"[GraphQL] retry in {pause}s", flush=True)
                time.sleep(pause)
                continue
            break

    raise RuntimeError(
        f"Goszakup GraphQL unavailable after {max_attempts} attempts: {repr(last_error)}"
    )


def find_control_lot(token: str) -> Dict[str, Any]:
    # 1. Точный поиск по полному номеру лота.
    tries = [
        {"lotNumber": CONTROL_LOT},
        {"lotNumber": CONTROL_LOT_PREFIX},
        {"trdBuyNumberAnno": "17596109-1"},
        {"nameDescriptionRu": "картридж"},
    ]

    for filt in tries:
        print("[GraphQL] filter =", json.dumps(filt, ensure_ascii=False), flush=True)
        lots = gql(token, {"limit": 200, "after": None, "filter": filt})
        print("[GraphQL] returned:", len(lots), flush=True)

        # Сначала строгая проверка номера лота.
        for lot in lots:
            number = compact(lot.get("lotNumber"))
            if number == CONTROL_LOT or number.startswith(CONTROL_LOT_PREFIX):
                return lot

    raise RuntimeError(f"Контрольный лот {CONTROL_LOT} не найден через GraphQL")


def choose_tech_file(files: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not files:
        return None

    def score(f: Dict[str, Any]) -> int:
        blob = " ".join(
            compact(f.get(k)).lower()
            for k in ("nameRu", "nameKz", "originalName", "filePath")
        )
        s = 0
        if "техничес" in blob:
            s += 10
        if "спецификац" in blob:
            s += 10
        if "techspec" in blob or "tech_spec" in blob:
            s += 10
        if blob.endswith(".pdf") or ".pdf" in blob:
            s += 2
        if "договор" in blob:
            s -= 5
        return s

    ranked = sorted([f for f in files if isinstance(f, dict)], key=score, reverse=True)
    return ranked[0] if ranked else None


def url_candidates(file_path: str) -> List[str]:
    p = compact(file_path)
    if not p:
        return []

    out: List[str] = []
    if p.startswith("http://") or p.startswith("https://"):
        out.append(p)
    else:
        pp = p if p.startswith("/") else "/" + p
        for base in (
            "https://ows.goszakup.gov.kz",
            "https://goszakup.gov.kz",
            "https://www.goszakup.gov.kz",
            "https://old.goszakup.gov.kz",
            "https://procurement.gov.kz",
            "https://zakup.gov.kz",
        ):
            out.append(urljoin(base, pp))

    # Убираем дубликаты, сохраняя порядок.
    seen = set()
    uniq = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def looks_like_pdf(content: bytes, content_type: str) -> bool:
    head = content[:16]
    return head.startswith(b"%PDF") or "pdf" in (content_type or "").lower()


def try_download_url(session: requests.Session, url: str, token: str) -> Tuple[Optional[bytes], Dict[str, Any]]:
    attempts = [
        {"Authorization": f"Bearer {token}", "User-Agent": "Tender-Radar-KZ/1.0"},
        {"User-Agent": "Mozilla/5.0 Tender-Radar-KZ/1.0"},
    ]
    last_info: Dict[str, Any] = {"url": url}

    for headers in attempts:
        try:
            r = session.get(url, headers=headers, timeout=60, allow_redirects=True)
            info = {
                "url": url,
                "final_url": r.url,
                "status": r.status_code,
                "content_type": r.headers.get("content-type", ""),
                "bytes": len(r.content),
                "authorized": "Authorization" in headers,
            }
            last_info = info
            print("[download]", json.dumps(info, ensure_ascii=False), flush=True)
            if r.ok and looks_like_pdf(r.content, info["content_type"]):
                return r.content, info
        except Exception as e:
            last_info = {"url": url, "error": repr(e), "authorized": "Authorization" in headers}
            print("[download] ERROR", json.dumps(last_info, ensure_ascii=False), flush=True)

    return None, last_info


def scrape_pdf_links(session: requests.Session, lot: Dict[str, Any], token: str) -> List[str]:
    """Резервный путь: смотрим публичные HTML-страницы и ищем ссылки на PDF/techspec."""
    ann = compact(lot.get("trdBuyNumberAnno") or (lot.get("TrdBuy") or {}).get("numberAnno"))
    ann_num = ann.split("-")[0] if ann else ""
    lot_id = compact(lot.get("id"))

    pages = []
    if ann_num:
        pages.extend([
            f"https://old.goszakup.gov.kz/ru/announce/index/{ann_num}?tab=documents",
            f"https://old.goszakup.gov.kz/ru/announce/index/{ann_num}?tab=lots",
            f"https://procurement.gov.kz/ru/announce/index/{ann_num}?tab=documents",
            f"https://procurement.gov.kz/ru/announce/index/{ann_num}?tab=lots",
        ])
    if ann_num and lot_id:
        pages.append(f"https://old.goszakup.gov.kz/ru/subpriceoffer/index/{ann_num}/{lot_id}")

    found: List[str] = []
    for page_url in pages:
        try:
            r = session.get(page_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=45, allow_redirects=True)
            print("[html]", r.status_code, r.url, "bytes=", len(r.content), flush=True)
            if not r.ok:
                continue
            html = r.text
            # href/src/action значения, где есть pdf или techspec.
            for m in re.finditer(r'''(?i)(?:href|src|action)=["']([^"']+)["']''', html):
                href = m.group(1)
                low = href.lower()
                if "techspec" in low or ".pdf" in low or "техспец" in low:
                    found.append(urljoin(r.url, href))
            # Иногда URL может быть внутри JS/JSON без href.
            for m in re.finditer(r'''(?i)(https?://[^\s"']+|/[A-Za-z0-9_./?=&%-]+)''', html):
                href = m.group(1)
                low = href.lower()
                if "techspec" in low and ("pdf" in low or "download" in low):
                    found.append(urljoin(r.url, href))
        except Exception as e:
            print("[html] ERROR", page_url, repr(e), flush=True)

    seen = set()
    uniq = []
    for u in found:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def extract_pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    parts: List[str] = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    text = "\n".join(parts)
    text = text.replace("\u00ad", "").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def marker_report(text: str) -> Dict[str, bool]:
    low = text.lower()
    return {
        "35A": "35a" in low,
        "CB435A": "cb435a" in low,
        "1500": "1500" in low or "1 500" in low,
        "P1005": "p1005" in low,
        "P1006": "p1006" in low,
        "новый": "нов" in low,
        "не восстановленный": "не восстанов" in low or "невосстанов" in low,
        "не перезаправленный": "не перезаправ" in low or "неперезаправ" in low,
    }


def main() -> int:
    token = os.getenv("GOSZAKUP_TOKEN", "").strip()
    if not token:
        print("ERROR: GOSZAKUP_TOKEN is missing", file=sys.stderr)
        return 2

    result: Dict[str, Any] = {
        "ok": False,
        "control_lot": CONTROL_LOT,
        "stage": "start",
        "download_attempts": [],
    }

    try:
        lot = find_control_lot(token)
        result["stage"] = "lot_found"
        result["lot"] = {
            "id": lot.get("id"),
            "lotNumber": lot.get("lotNumber"),
            "nameRu": lot.get("nameRu"),
            "descriptionRu": lot.get("descriptionRu"),
            "trdBuyNumberAnno": lot.get("trdBuyNumberAnno"),
            "trdBuyId": lot.get("trdBuyId"),
        }

        files = [x for x in (lot.get("Files") or []) if isinstance(x, dict)]
        result["files"] = files
        print("\n=== FILES FROM GRAPHQL ===", flush=True)
        print(json.dumps(files, ensure_ascii=False, indent=2), flush=True)

        tech_file = choose_tech_file(files)
        result["tech_file"] = tech_file
        if not tech_file:
            raise RuntimeError("GraphQL не вернул ни одного Files для контрольного лота")

        print("\n=== SELECTED TECH FILE ===", flush=True)
        print(json.dumps(tech_file, ensure_ascii=False, indent=2), flush=True)

        session = requests.Session()
        pdf_bytes: Optional[bytes] = None
        success_info: Optional[Dict[str, Any]] = None

        for url in url_candidates(str(tech_file.get("filePath") or "")):
            content, info = try_download_url(session, url, token)
            result["download_attempts"].append(info)
            if content:
                pdf_bytes = content
                success_info = info
                break

        # Если filePath не дал PDF — пробуем найти реальную ссылку в публичном HTML.
        if pdf_bytes is None:
            scraped = scrape_pdf_links(session, lot, token)
            result["scraped_links"] = scraped
            print("\n=== SCRAPED CANDIDATES ===", flush=True)
            for u in scraped:
                print(u, flush=True)
            for url in scraped:
                content, info = try_download_url(session, url, token)
                result["download_attempts"].append(info)
                if content:
                    pdf_bytes = content
                    success_info = info
                    break

        if pdf_bytes is None:
            result["stage"] = "pdf_not_downloaded"
            raise RuntimeError("Не удалось скачать PDF. Смотрите filePath и download_attempts в JSON.")

        pdf_path = OUTPUT_DIR / "goszakup_control_87884071.pdf"
        pdf_path.write_bytes(pdf_bytes)
        result["pdf_path"] = str(pdf_path)
        result["pdf_source"] = success_info
        result["stage"] = "pdf_downloaded"

        text = extract_pdf_text(pdf_bytes)
        text_path = OUTPUT_DIR / "goszakup_pdf_test_text.txt"
        text_path.write_text(text, encoding="utf-8")
        result["text_length"] = len(text)
        result["markers"] = marker_report(text)
        result["text_excerpt"] = text[:4000]
        result["stage"] = "text_extracted"

        print("\n=== MARKERS ===", flush=True)
        print(json.dumps(result["markers"], ensure_ascii=False, indent=2), flush=True)
        print("\n=== TEXT EXCERPT ===", flush=True)
        print(text[:4000], flush=True)

        core = ["35A", "CB435A", "1500", "P1005", "P1006"]
        result["ok"] = all(result["markers"].get(k) for k in core)
        result["stage"] = "passed" if result["ok"] else "text_extracted_but_markers_incomplete"

    except Exception as e:
        result["error"] = repr(e)
        print("\nTEST ERROR:", repr(e), file=sys.stderr, flush=True)

    out = OUTPUT_DIR / "goszakup_pdf_test_result.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nRESULT JSON:", out, flush=True)
    print("FINAL STATUS:", result.get("stage"), "OK=", result.get("ok"), flush=True)

    # Control workflow must be green only when the PDF text itself passed
    # the required marker checks. On network/PDF failure we return non-zero,
    # so the Supabase PATCH step will not run and cannot hide the real cause.
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
