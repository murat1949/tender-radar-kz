#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tender Radar KZ — isolated ACTIVE Goszakup PDF control.

All control values come from environment variables. No lot number, product
model, brand, or expected phrase is hard-coded into the program.

The script performs only:
  Goszakup GraphQL -> select the requested lot -> select/download a PDF
  -> extract text -> verify the PDF belongs to the requested announcement/lot
  -> save diagnostics.

EXPECTED_TEXT is optional and diagnostic only. Its absence from the PDF never
invalidates an otherwise correctly identified technical-specification PDF.
The GitHub Actions workflow performs the one-row Supabase patch only after
this script exits successfully.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from pypdf import PdfReader

GRAPHQL_URL = "https://ows.goszakup.gov.kz/v3/graphql"

CONTROL_LOT = os.getenv("CONTROL_LOT", "").strip()
CONTROL_ANN = os.getenv("CONTROL_ANN", "").strip()
EXPECTED_TEXT = os.getenv("EXPECTED_TEXT", "").strip()
CONTROL_FILE_OBJECT_ID = os.getenv("CONTROL_FILE_OBJECT_ID", "").strip()

OUTPUT_DIR = Path(__file__).resolve().parent / "output_active_control"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
RESULT_PATH = OUTPUT_DIR / "active_control_result.json"
TEXT_PATH = OUTPUT_DIR / "active_control_text.txt"
PDF_PATH = OUTPUT_DIR / "active_control.pdf"

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


def compact(value: Any) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def normalize_lot(value: Any) -> str:
    """Normalize visual look-alikes such as Cyrillic З vs digit 3."""
    s = compact(value).upper()
    s = s.translate(str.maketrans({
        "З": "3",  # Cyrillic ZE, visually close to 3 in the portal/PDF
        "–": "-",
        "—": "-",
        "−": "-",
        "‑": "-",
        "‐": "-",
    }))
    return re.sub(r"\s+", "", s)


def lot_prefix(value: Any) -> str:
    """Return the long numeric lot prefix, independent of suffix spelling."""
    m = re.search(r"\d{6,}", normalize_lot(value))
    return m.group(0) if m else ""


def normalize_marker(value: Any) -> str:
    s = str(value or "").replace("\u00ad", "").replace("\u200b", "")
    return " ".join(s.lower().split())


def lot_announcement(lot: Dict[str, Any]) -> str:
    return compact(
        lot.get("trdBuyNumberAnno")
        or ((lot.get("TrdBuy") or {}).get("numberAnno") if isinstance(lot.get("TrdBuy"), dict) else "")
    )


def gql(token: str, variables: Dict[str, Any], retries: int = 4) -> List[Dict[str, Any]]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Tender-Radar-KZ-Active-Control/2.0",
    }
    last_error: Optional[BaseException] = None

    for attempt in range(1, retries + 1):
        try:
            print(f"[GraphQL] attempt {attempt}/{retries}", flush=True)
            response = requests.post(
                GRAPHQL_URL,
                headers=headers,
                json={"query": QUERY, "variables": variables},
                timeout=(20, 90),
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("errors"):
                raise RuntimeError(
                    "GraphQL error: " + json.dumps(payload["errors"], ensure_ascii=False)
                )
            lots = payload.get("data", {}).get("Lots") or []
            return [x for x in lots if isinstance(x, dict)]
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            print("[GraphQL] transient error:", repr(exc), flush=True)
            if attempt < retries:
                time.sleep(4 * attempt)
        except requests.HTTPError as exc:
            last_error = exc
            status = getattr(exc.response, "status_code", None)
            body = ""
            try:
                body = (exc.response.text or "")[:1000]
            except Exception:
                pass
            print(f"[GraphQL] HTTP error status={status}: {body}", flush=True)
            # Retry server/rate-limit errors, but fail fast on other client errors.
            if status not in (408, 425, 429, 500, 502, 503, 504) or attempt >= retries:
                raise
            time.sleep(4 * attempt)

    raise RuntimeError(f"GraphQL unavailable after {retries} attempts: {last_error!r}")


def choose_control_lot(lots: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    candidates = [x for x in lots if isinstance(x, dict)]
    target_norm = normalize_lot(CONTROL_LOT)
    target_prefix = lot_prefix(CONTROL_LOT)

    def rank(lot: Dict[str, Any]) -> int:
        num = compact(lot.get("lotNumber"))
        ann = lot_announcement(lot)
        score = 0
        if ann == CONTROL_ANN:
            score += 1000
        if target_prefix and lot_prefix(num) == target_prefix:
            score += 500
        if normalize_lot(num) == target_norm:
            score += 200
        return score

    ranked = sorted(candidates, key=rank, reverse=True)
    if not ranked:
        return None

    best = ranked[0]
    # The essential identity check is the numeric prefix. The suffix may be
    # rendered by Goszakup as "3ЦП1" or "ЗЦП1" (Cyrillic З).
    if target_prefix and lot_prefix(best.get("lotNumber")) != target_prefix:
        return None
    return best


def find_control_lot(token: str) -> Dict[str, Any]:
    # Announcement-first is deliberate: selection by numeric lot prefix remains
    # stable even when the portal/API render suffix characters differently.
    filters = [
        {"trdBuyNumberAnno": CONTROL_ANN},
        {"lotNumber": CONTROL_LOT},
    ]

    for filt in filters:
        print("[GraphQL] filter =", json.dumps(filt, ensure_ascii=False), flush=True)
        lots = gql(token, {"limit": 200, "after": None, "filter": filt})
        print("[GraphQL] returned:", len(lots), flush=True)
        lot = choose_control_lot(lots)
        if lot is not None:
            print(
                "SELECTED LOT:",
                json.dumps(
                    {
                        "id": lot.get("id"),
                        "lotNumber": lot.get("lotNumber"),
                        "trdBuyNumberAnno": lot_announcement(lot),
                        "amount": lot.get("amount"),
                        "count": lot.get("count"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return lot

    raise RuntimeError(
        f"Контрольный активный лот с числовым префиксом {lot_prefix(CONTROL_LOT)} "
        f"в объявлении {CONTROL_ANN} не найден"
    )


def tech_file_score(file_info: Dict[str, Any]) -> int:
    blob = " ".join(
        compact(file_info.get(key)).lower()
        for key in ("nameRu", "nameKz", "originalName", "filePath")
    )
    score = 0

    object_id = str(file_info.get("objectId") or "").strip()
    original_name = compact(file_info.get("originalName")).lower()

    if CONTROL_FILE_OBJECT_ID and object_id == CONTROL_FILE_OBJECT_ID:
        score += 1000
    if CONTROL_FILE_OBJECT_ID and CONTROL_FILE_OBJECT_ID in original_name:
        score += 500
    if CONTROL_ANN.split("-")[0] in original_name:
        score += 100
    if "techspec" in blob or "tech_spec" in blob:
        score += 80
    if "техничес" in blob or "техникалық" in blob:
        score += 40
    if "спецификац" in blob or "ерекшел" in blob:
        score += 40
    if ".pdf" in blob:
        score += 20
    if "договор" in blob or "contract" in blob:
        score -= 100
    return score


def ranked_tech_files(files: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = [x for x in files if isinstance(x, dict) and compact(x.get("filePath"))]
    return sorted(rows, key=tech_file_score, reverse=True)


def url_candidates(file_path: str) -> List[str]:
    path = compact(file_path)
    if not path:
        return []

    result: List[str] = []
    if path.startswith(("http://", "https://")):
        result.append(path)
    else:
        relative = path if path.startswith("/") else "/" + path
        for base in (
            "https://ows.goszakup.gov.kz",
            "https://goszakup.gov.kz",
            "https://www.goszakup.gov.kz",
            "https://old.goszakup.gov.kz",
            "https://procurement.gov.kz",
        ):
            result.append(urljoin(base, relative))

    # Preserve order while removing duplicates.
    return list(dict.fromkeys(result))


def looks_like_pdf(content: bytes, content_type: str) -> bool:
    return content[:16].startswith(b"%PDF") or "pdf" in (content_type or "").lower()


def try_download(
    session: requests.Session,
    url: str,
    token: str,
    retries: int = 3,
) -> Tuple[Optional[bytes], Dict[str, Any]]:
    last_info: Dict[str, Any] = {"url": url}

    header_variants = (
        {
            "Authorization": f"Bearer {token}",
            "User-Agent": "Tender-Radar-KZ-Active-Control/2.0",
        },
        {
            "User-Agent": "Mozilla/5.0 Tender-Radar-KZ-Active-Control/2.0",
        },
    )

    for headers in header_variants:
        for attempt in range(1, retries + 1):
            try:
                response = session.get(
                    url,
                    headers=headers,
                    timeout=(20, 90),
                    allow_redirects=True,
                )
                info = {
                    "url": url,
                    "final_url": response.url,
                    "status": response.status_code,
                    "content_type": response.headers.get("content-type", ""),
                    "bytes": len(response.content),
                    "authorized": "Authorization" in headers,
                    "attempt": attempt,
                }
                print("[download]", json.dumps(info, ensure_ascii=False), flush=True)
                last_info = info

                if response.ok and looks_like_pdf(response.content, info["content_type"]):
                    return response.content, info

                # Non-transient client response: no need to repeat the same
                # header variant three times.
                if response.status_code not in (408, 425, 429, 500, 502, 503, 504):
                    break
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_info = {
                    "url": url,
                    "error": repr(exc),
                    "attempt": attempt,
                    "authorized": "Authorization" in headers,
                }
                print(
                    "[download] transient error",
                    json.dumps(last_info, ensure_ascii=False),
                    flush=True,
                )
                if attempt < retries:
                    time.sleep(4 * attempt)

    return None, last_info


def extract_pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    text = text.replace("\u00ad", "").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def verify_pdf_text(text: str) -> Tuple[bool, Dict[str, bool]]:
    """Verify document identity. Product phrases are optional diagnostics only."""
    haystack = normalize_marker(text)
    checks: Dict[str, bool] = {
        "announcement": normalize_marker(CONTROL_ANN) in haystack,
        "lot_prefix": normalize_marker(lot_prefix(CONTROL_LOT)) in haystack,
    }
    if EXPECTED_TEXT:
        checks["expected_text"] = normalize_marker(EXPECTED_TEXT) in haystack

    identity_ok = checks["announcement"] and checks["lot_prefix"]
    return identity_ok, checks


def save_result(result: Dict[str, Any]) -> None:
    RESULT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    token = os.getenv("GOSZAKUP_TOKEN", "").strip()
    if not token:
        print("ERROR: GOSZAKUP_TOKEN is missing", flush=True)
        return 2
    if not CONTROL_ANN or not lot_prefix(CONTROL_LOT):
        print("ERROR: CONTROL_ANN and CONTROL_LOT are required", flush=True)
        return 2

    result: Dict[str, Any] = {
        "ok": False,
        "control_lot": CONTROL_LOT,
        "control_lot_prefix": lot_prefix(CONTROL_LOT),
        "control_announcement": CONTROL_ANN,
        "control_file_object_id": CONTROL_FILE_OBJECT_ID,
        "expected_text": EXPECTED_TEXT,
        "stage": "start",
        "download_attempts": [],
    }

    try:
        lot = find_control_lot(token)
        selected_ann = lot_announcement(lot)
        selected_prefix = lot_prefix(lot.get("lotNumber"))

        if selected_ann and selected_ann != CONTROL_ANN:
            raise RuntimeError(
                f"Выбран неверный номер объявления: {selected_ann!r}, ожидался {CONTROL_ANN!r}"
            )
        if selected_prefix != lot_prefix(CONTROL_LOT):
            raise RuntimeError(
                f"Выбран неверный префикс лота: {selected_prefix!r}"
            )

        result["lot"] = {
            key: lot.get(key)
            for key in (
                "id",
                "lotNumber",
                "nameRu",
                "descriptionRu",
                "trdBuyNumberAnno",
                "trdBuyId",
                "count",
                "amount",
            )
        }
        result["selected_lot_number_normalized"] = normalize_lot(lot.get("lotNumber"))

        files = [x for x in (lot.get("Files") or []) if isinstance(x, dict)]
        ranked_files = ranked_tech_files(files)
        result["files"] = files
        result["file_candidates"] = [
            {
                "id": f.get("id"),
                "objectId": f.get("objectId"),
                "originalName": f.get("originalName"),
                "nameRu": f.get("nameRu"),
                "score": tech_file_score(f),
            }
            for f in ranked_files
        ]

        if not ranked_files:
            raise RuntimeError("GraphQL не вернул файлов для контрольного лота")

        session = requests.Session()
        accepted_pdf: Optional[bytes] = None
        accepted_text = ""
        accepted_file: Optional[Dict[str, Any]] = None
        accepted_download: Optional[Dict[str, Any]] = None
        accepted_checks: Dict[str, bool] = {}

        # Do not trust only the filename. Verify document identity from PDF text:
        # announcement number + numeric lot prefix.
        for file_info in ranked_files:
            print(
                "TRY FILE:",
                json.dumps(
                    {
                        "id": file_info.get("id"),
                        "objectId": file_info.get("objectId"),
                        "originalName": file_info.get("originalName"),
                        "score": tech_file_score(file_info),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

            for url in url_candidates(str(file_info.get("filePath") or "")):
                content, info = try_download(session, url, token)
                attempt_record = dict(info)
                attempt_record.update(
                    {
                        "file_id": file_info.get("id"),
                        "objectId": file_info.get("objectId"),
                        "originalName": file_info.get("originalName"),
                    }
                )
                result["download_attempts"].append(attempt_record)

                if content is None:
                    continue

                try:
                    text = extract_pdf_text(content)
                except Exception as exc:
                    print("[pdf] extract error:", repr(exc), flush=True)
                    continue

                ok, checks = verify_pdf_text(text)
                print(
                    "[pdf] verify:",
                    json.dumps(
                        {"chars": len(text), "checks": checks, "ok": ok},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

                if ok:
                    accepted_pdf = content
                    accepted_text = text
                    accepted_file = file_info
                    accepted_download = info
                    accepted_checks = checks
                    break

            if accepted_pdf is not None:
                break

        if accepted_pdf is None or accepted_file is None:
            raise RuntimeError(
                "Не найден PDF, содержащий одновременно номер объявления "
                f"{CONTROL_ANN} и префикс лота {lot_prefix(CONTROL_LOT)}"
            )

        PDF_PATH.write_bytes(accepted_pdf)
        TEXT_PATH.write_text(accepted_text, encoding="utf-8")

        result.update(
            {
                "tech_file": accepted_file,
                "pdf_path": str(PDF_PATH),
                "pdf_source": accepted_download,
                "text_length": len(accepted_text),
                "verification": accepted_checks,
                "expected_text_found": (
                    accepted_checks.get("expected_text") if EXPECTED_TEXT else None
                ),
                "text_excerpt": accepted_text[:4000],
                "stage": "passed",
                "ok": True,
            }
        )

        print("ACTIVE CONTROL PASS: API -> PDF -> TEXT", flush=True)
        print("LOT API:", lot.get("lotNumber"), flush=True)
        print("LOT PREFIX:", lot_prefix(lot.get("lotNumber")), flush=True)
        print("ANNOUNCEMENT:", CONTROL_ANN, flush=True)
        print(
            "PDF:",
            accepted_file.get("originalName"),
            "objectId=",
            accepted_file.get("objectId"),
            flush=True,
        )
        if EXPECTED_TEXT:
            print(
                "OPTIONAL EXPECTED:",
                EXPECTED_TEXT,
                "FOUND=",
                accepted_checks.get("expected_text", False),
                flush=True,
            )
        print("PDF CHARS:", len(accepted_text), flush=True)

    except Exception as exc:
        result["stage"] = "error"
        result["error"] = repr(exc)
        print("ACTIVE CONTROL ERROR:", repr(exc), flush=True)

    save_result(result)
    print("RESULT JSON:", RESULT_PATH, flush=True)
    print("FINAL STATUS:", result.get("stage"), "OK=", result.get("ok"), flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
