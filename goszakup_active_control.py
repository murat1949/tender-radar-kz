#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Tender Radar KZ — universal isolated Goszakup control.

The target lot is configured only through environment variables:
  CONTROL_LOT      required
  CONTROL_ANN      required
  EXPECTED_TEXT    optional, but recommended for a control test
  CONTROL_FILE_OBJECT_ID optional hint only
  GOSZAKUP_TOKEN   required

This script never writes to Supabase. It only performs:
  Goszakup GraphQL -> select target lot -> find/download candidate PDFs
  -> extract text -> verify identity -> save diagnostics.

No lot number, announcement number, product model, or previous test marker is
hard-coded in the program.
"""

from __future__ import annotations

import io
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from pypdf import PdfReader

GRAPHQL_URL = "https://ows.goszakup.gov.kz/v3/graphql"
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


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def compact(value: Any) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def normalize_lot(value: Any) -> str:
    s = compact(value).upper()
    s = s.translate(str.maketrans({
        "З": "3",  # Cyrillic ZE can be visually confused with digit 3
        "–": "-",
        "—": "-",
        "−": "-",
        "‑": "-",
        "‐": "-",
    }))
    return re.sub(r"\s+", "", s)


def lot_prefix(value: Any) -> str:
    m = re.search(r"\d{6,}", normalize_lot(value))
    return m.group(0) if m else ""


def normalize_text(value: Any) -> str:
    s = str(value or "").replace("\u00ad", "").replace("\u200b", "")
    return " ".join(s.lower().split())


def alnum_text(value: Any) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", normalize_text(value), flags=re.I)


def contains_marker(text: str, marker: str) -> bool:
    if not marker:
        return True
    normal_marker = normalize_text(marker)
    if normal_marker and normal_marker in normalize_text(text):
        return True
    compact_marker = alnum_text(marker)
    return bool(compact_marker and compact_marker in alnum_text(text))


def lot_announcement(lot: Dict[str, Any]) -> str:
    trd = lot.get("TrdBuy") if isinstance(lot.get("TrdBuy"), dict) else {}
    return compact(lot.get("trdBuyNumberAnno") or trd.get("numberAnno"))


def gql(token: str, variables: Dict[str, Any], retries: int = 6) -> List[Dict[str, Any]]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Tender-Radar-KZ-Active-Control/3.0",
    }
    last_error: Optional[BaseException] = None

    for attempt in range(1, retries + 1):
        try:
            print(f"[GraphQL] attempt {attempt}/{retries}", flush=True)
            response = requests.post(
                GRAPHQL_URL,
                headers=headers,
                json={"query": QUERY, "variables": variables},
                timeout=(15, 90),
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("errors"):
                raise RuntimeError("GraphQL error: " + json.dumps(payload["errors"], ensure_ascii=False))
            lots = payload.get("data", {}).get("Lots") or []
            return [x for x in lots if isinstance(x, dict)]
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            print("[GraphQL] transient error:", repr(exc), flush=True)
            if attempt < retries:
                time.sleep(min(5 * attempt, 25))
        except requests.HTTPError as exc:
            last_error = exc
            status = getattr(exc.response, "status_code", None)
            body = ""
            try:
                body = (exc.response.text or "")[:1000]
            except Exception:
                pass
            print(f"[GraphQL] HTTP error status={status}: {body}", flush=True)
            if status not in (408, 425, 429, 500, 502, 503, 504) or attempt >= retries:
                raise
            time.sleep(min(5 * attempt, 25))

    raise RuntimeError(f"Goszakup GraphQL unavailable after {retries} attempts: {last_error!r}")


def choose_control_lot(
    lots: Iterable[Dict[str, Any]], control_lot: str, control_ann: str
) -> Optional[Dict[str, Any]]:
    candidates = [x for x in lots if isinstance(x, dict)]
    target_norm = normalize_lot(control_lot)
    target_prefix = lot_prefix(control_lot)

    def rank(lot: Dict[str, Any]) -> int:
        number = compact(lot.get("lotNumber"))
        ann = lot_announcement(lot)
        score = 0
        if ann == control_ann:
            score += 1000
        if target_prefix and lot_prefix(number) == target_prefix:
            score += 500
        if normalize_lot(number) == target_norm:
            score += 200
        return score

    ranked = sorted(candidates, key=rank, reverse=True)
    if not ranked:
        return None

    best = ranked[0]
    if target_prefix and lot_prefix(best.get("lotNumber")) != target_prefix:
        return None
    if lot_announcement(best) and lot_announcement(best) != control_ann:
        return None
    return best


def find_control_lot(token: str, control_lot: str, control_ann: str) -> Dict[str, Any]:
    filters = [
        {"trdBuyNumberAnno": control_ann},
        {"lotNumber": control_lot},
    ]

    for filt in filters:
        print("[GraphQL] filter =", json.dumps(filt, ensure_ascii=False), flush=True)
        lots = gql(token, {"limit": 200, "after": None, "filter": filt})
        print("[GraphQL] returned:", len(lots), flush=True)
        lot = choose_control_lot(lots, control_lot, control_ann)
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
        f"Target lot prefix {lot_prefix(control_lot)} in announcement {control_ann} was not found"
    )


def tech_file_score(file_info: Dict[str, Any], control_ann: str, object_hint: str) -> int:
    blob = " ".join(
        compact(file_info.get(key)).lower()
        for key in ("nameRu", "nameKz", "originalName", "filePath")
    )
    score = 0
    object_id = str(file_info.get("objectId") or "").strip()
    original_name = compact(file_info.get("originalName")).lower()

    if object_hint and object_id == object_hint:
        score += 1000
    if object_hint and object_hint in original_name:
        score += 500
    if control_ann.split("-")[0] in original_name:
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


def ranked_tech_files(
    files: Iterable[Dict[str, Any]], control_ann: str, object_hint: str
) -> List[Dict[str, Any]]:
    rows = [x for x in files if isinstance(x, dict) and compact(x.get("filePath"))]
    return sorted(rows, key=lambda f: tech_file_score(f, control_ann, object_hint), reverse=True)


def url_candidates(file_path: str) -> List[str]:
    path = compact(file_path)
    if not path:
        return []
    if path.startswith(("http://", "https://")):
        return [path]

    relative = path if path.startswith("/") else "/" + path
    bases = (
        "https://ows.goszakup.gov.kz",
        "https://goszakup.gov.kz",
        "https://www.goszakup.gov.kz",
        "https://old.goszakup.gov.kz",
        "https://procurement.gov.kz",
    )
    return list(dict.fromkeys(urljoin(base, relative) for base in bases))


def looks_like_pdf(content: bytes, content_type: str) -> bool:
    return content[:16].startswith(b"%PDF") or "pdf" in (content_type or "").lower()


def try_download(
    session: requests.Session, url: str, token: str, retries: int = 4
) -> Tuple[Optional[bytes], Dict[str, Any]]:
    last_info: Dict[str, Any] = {"url": url}
    header_variants = (
        {"Authorization": f"Bearer {token}", "User-Agent": "Tender-Radar-KZ-Active-Control/3.0"},
        {"User-Agent": "Mozilla/5.0 Tender-Radar-KZ-Active-Control/3.0"},
    )

    for headers in header_variants:
        for attempt in range(1, retries + 1):
            try:
                response = session.get(
                    url,
                    headers=headers,
                    timeout=(15, 90),
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
                if response.status_code not in (408, 425, 429, 500, 502, 503, 504):
                    break
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_info = {
                    "url": url,
                    "error": repr(exc),
                    "attempt": attempt,
                    "authorized": "Authorization" in headers,
                }
                print("[download] transient error", json.dumps(last_info, ensure_ascii=False), flush=True)
                if attempt < retries:
                    time.sleep(min(5 * attempt, 20))

    return None, last_info


def extract_pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    text = text.replace("\u00ad", "").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def verify_pdf_text(text: str, control_lot: str, control_ann: str, expected_text: str) -> Tuple[bool, Dict[str, bool]]:
    checks = {
        "announcement": contains_marker(text, control_ann),
        "lot_prefix": contains_marker(text, lot_prefix(control_lot)),
        "expected_text": contains_marker(text, expected_text) if expected_text else True,
    }
    return all(checks.values()), checks


def save_result(result: Dict[str, Any]) -> None:
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    result: Dict[str, Any] = {"ok": False, "stage": "start", "download_attempts": []}

    try:
        token = env_required("GOSZAKUP_TOKEN")
        control_lot = env_required("CONTROL_LOT")
        control_ann = env_required("CONTROL_ANN")
        expected_text = os.getenv("EXPECTED_TEXT", "").strip()
        object_hint = os.getenv("CONTROL_FILE_OBJECT_ID", "").strip()

        result.update({
            "control_lot": control_lot,
            "control_lot_prefix": lot_prefix(control_lot),
            "control_announcement": control_ann,
            "control_file_object_id": object_hint,
            "expected_text": expected_text,
        })

        lot = find_control_lot(token, control_lot, control_ann)
        selected_ann = lot_announcement(lot)
        selected_prefix = lot_prefix(lot.get("lotNumber"))

        if selected_ann and selected_ann != control_ann:
            raise RuntimeError(f"Wrong announcement selected: {selected_ann!r}, expected {control_ann!r}")
        if selected_prefix != lot_prefix(control_lot):
            raise RuntimeError(f"Wrong lot prefix selected: {selected_prefix!r}")

        result["lot"] = {
            key: lot.get(key)
            for key in (
                "id", "lotNumber", "nameRu", "descriptionRu", "trdBuyNumberAnno",
                "trdBuyId", "count", "amount"
            )
        }

        files = [x for x in (lot.get("Files") or []) if isinstance(x, dict)]
        ranked_files = ranked_tech_files(files, control_ann, object_hint)
        result["files"] = files
        result["file_candidates"] = [
            {
                "id": f.get("id"),
                "objectId": f.get("objectId"),
                "originalName": f.get("originalName"),
                "nameRu": f.get("nameRu"),
                "score": tech_file_score(f, control_ann, object_hint),
            }
            for f in ranked_files
        ]

        if not ranked_files:
            raise RuntimeError("GraphQL returned no downloadable files for the target lot")

        session = requests.Session()
        accepted_pdf: Optional[bytes] = None
        accepted_text = ""
        accepted_file: Optional[Dict[str, Any]] = None
        accepted_download: Optional[Dict[str, Any]] = None
        accepted_checks: Dict[str, bool] = {}

        for file_info in ranked_files:
            print(
                "TRY FILE:",
                json.dumps(
                    {
                        "id": file_info.get("id"),
                        "objectId": file_info.get("objectId"),
                        "originalName": file_info.get("originalName"),
                        "score": tech_file_score(file_info, control_ann, object_hint),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

            for url in url_candidates(str(file_info.get("filePath") or "")):
                content, info = try_download(session, url, token)
                record = dict(info)
                record.update({
                    "file_id": file_info.get("id"),
                    "objectId": file_info.get("objectId"),
                    "originalName": file_info.get("originalName"),
                })
                result["download_attempts"].append(record)

                if content is None:
                    continue

                try:
                    text = extract_pdf_text(content)
                except Exception as exc:
                    print("[pdf] extract error:", repr(exc), flush=True)
                    continue

                ok, checks = verify_pdf_text(text, control_lot, control_ann, expected_text)
                print(
                    "[pdf] verify:",
                    json.dumps({"chars": len(text), "checks": checks, "ok": ok}, ensure_ascii=False),
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
            expected_note = f", expected marker {expected_text!r}" if expected_text else ""
            raise RuntimeError(
                f"No PDF matched announcement {control_ann!r}, lot prefix {lot_prefix(control_lot)!r}{expected_note}"
            )

        PDF_PATH.write_bytes(accepted_pdf)
        TEXT_PATH.write_text(accepted_text, encoding="utf-8")

        result.update({
            "tech_file": accepted_file,
            "pdf_path": str(PDF_PATH),
            "pdf_source": accepted_download,
            "text_length": len(accepted_text),
            "verification": accepted_checks,
            "expected_text_found": accepted_checks.get("expected_text", False),
            "text_excerpt": accepted_text[:4000],
            "stage": "passed",
            "ok": True,
        })

        print("ACTIVE CONTROL PASS: API -> PDF -> TEXT", flush=True)
        print("LOT API:", lot.get("lotNumber"), flush=True)
        print("LOT PREFIX:", lot_prefix(lot.get("lotNumber")), flush=True)
        print("ANNOUNCEMENT:", control_ann, flush=True)
        print("PDF:", accepted_file.get("originalName"), "objectId=", accepted_file.get("objectId"), flush=True)
        if expected_text:
            print("EXPECTED:", expected_text, "FOUND=True", flush=True)
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
