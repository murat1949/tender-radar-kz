#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import io, json, os, re, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from pypdf import PdfReader

GRAPHQL_URL = "https://ows.goszakup.gov.kz/v3/graphql"
CONTROL_LOT = os.getenv("CONTROL_LOT", "87888776-3ЦП1").strip()
CONTROL_ANN = os.getenv("CONTROL_ANN", "17602864-1").strip()
EXPECTED_TEXT = os.getenv("EXPECTED_TEXT", "LaserJet P1102").strip()
OUTPUT_DIR = Path(__file__).resolve().parent / "output_active_control"
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
      id nameRu descRu extraDescRu count refUnitsCode supplyDateRu prepayment
      PlansKato { fullDeliveryPlaceNameRu count }
    }
    Files {
      id filePath originalName objectId nameRu nameKz indexDate systemId
    }
    TrdBuy { id numberAnno nameRu startDate endDate publishDate }
  }
}
"""

def compact(v: Any) -> str:
    return " ".join(str(v or "").replace("\r", " ").replace("\n", " ").split())

def gql(token: str, variables: Dict[str, Any], retries: int = 4) -> List[Dict[str, Any]]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Tender-Radar-KZ-Active-Control/1.0",
    }
    last = None
    for attempt in range(1, retries + 1):
        try:
            print(f"[GraphQL] attempt {attempt}/{retries}", flush=True)
            r = requests.post(
                GRAPHQL_URL,
                headers=headers,
                json={"query": QUERY, "variables": variables},
                timeout=60,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("errors"):
                raise RuntimeError("GraphQL error: " + json.dumps(data["errors"], ensure_ascii=False))
            return data.get("data", {}).get("Lots") or []
        except (requests.Timeout, requests.ConnectionError) as e:
            last = e
            print("[GraphQL] transient error:", repr(e), flush=True)
            if attempt < retries:
                time.sleep(5 * attempt)
    raise RuntimeError(f"GraphQL unavailable after {retries} attempts: {last!r}")

def find_control_lot(token: str) -> Dict[str, Any]:
    for filt in [
        {"lotNumber": CONTROL_LOT},
        {"trdBuyNumberAnno": CONTROL_ANN},
        {"nameDescriptionRu": "картридж"},
    ]:
        print("[GraphQL] filter =", json.dumps(filt, ensure_ascii=False), flush=True)
        lots = gql(token, {"limit": 200, "after": None, "filter": filt})
        print("[GraphQL] returned:", len(lots), flush=True)
        for lot in lots:
            if compact(lot.get("lotNumber")) == CONTROL_LOT:
                return lot
    raise RuntimeError(f"Контрольный активный лот {CONTROL_LOT} не найден")

def choose_tech_file(files: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    def score(f: Dict[str, Any]) -> int:
        blob = " ".join(compact(f.get(k)).lower() for k in ("nameRu","nameKz","originalName","filePath"))
        s = 0
        if "техничес" in blob: s += 10
        if "спецификац" in blob: s += 10
        if "techspec" in blob or "tech_spec" in blob: s += 10
        if ".pdf" in blob: s += 2
        if "договор" in blob: s -= 5
        return s
    ranked = sorted([f for f in files if isinstance(f, dict)], key=score, reverse=True)
    return ranked[0] if ranked else None

def url_candidates(file_path: str) -> List[str]:
    p = compact(file_path)
    if not p:
        return []
    out = []
    if p.startswith(("http://","https://")):
        out.append(p)
    else:
        pp = p if p.startswith("/") else "/" + p
        for base in (
            "https://ows.goszakup.gov.kz",
            "https://goszakup.gov.kz",
            "https://www.goszakup.gov.kz",
            "https://old.goszakup.gov.kz",
            "https://procurement.gov.kz",
        ):
            out.append(urljoin(base, pp))
    return list(dict.fromkeys(out))

def looks_like_pdf(content: bytes, content_type: str) -> bool:
    return content[:16].startswith(b"%PDF") or "pdf" in (content_type or "").lower()

def try_download(session: requests.Session, url: str, token: str) -> Tuple[Optional[bytes], Dict[str, Any]]:
    last_info = {"url": url}
    for headers in (
        {"Authorization": f"Bearer {token}", "User-Agent": "Tender-Radar-KZ/1.0"},
        {"User-Agent": "Mozilla/5.0 Tender-Radar-KZ/1.0"},
    ):
        for attempt in range(1, 4):
            try:
                r = session.get(url, headers=headers, timeout=60, allow_redirects=True)
                info = {
                    "url": url,
                    "final_url": r.url,
                    "status": r.status_code,
                    "content_type": r.headers.get("content-type", ""),
                    "bytes": len(r.content),
                    "authorized": "Authorization" in headers,
                    "attempt": attempt,
                }
                print("[download]", json.dumps(info, ensure_ascii=False), flush=True)
                last_info = info
                if r.ok and looks_like_pdf(r.content, info["content_type"]):
                    return r.content, info
            except (requests.Timeout, requests.ConnectionError) as e:
                last_info = {
                    "url": url,
                    "error": repr(e),
                    "attempt": attempt,
                    "authorized": "Authorization" in headers,
                }
                print("[download] transient error", json.dumps(last_info, ensure_ascii=False), flush=True)
                if attempt < 3:
                    time.sleep(4 * attempt)
    return None, last_info

def extract_pdf_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    text = text.replace("\u00ad", "").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def main() -> int:
    token = os.getenv("GOSZAKUP_TOKEN", "").strip()
    if not token:
        print("ERROR: GOSZAKUP_TOKEN is missing", flush=True)
        return 2

    result = {"ok": False, "control_lot": CONTROL_LOT, "stage": "start", "download_attempts": []}
    try:
        lot = find_control_lot(token)
        result["lot"] = {k: lot.get(k) for k in ("id","lotNumber","nameRu","descriptionRu","trdBuyNumberAnno","trdBuyId","count","amount")}
        files = [x for x in (lot.get("Files") or []) if isinstance(x, dict)]
        result["files"] = files
        tech_file = choose_tech_file(files)
        result["tech_file"] = tech_file
        if not tech_file:
            raise RuntimeError("GraphQL не вернул файл техспецификации")

        print("SELECTED FILE:", json.dumps(tech_file, ensure_ascii=False), flush=True)

        session = requests.Session()
        pdf_bytes = None
        success_info = None
        for url in url_candidates(str(tech_file.get("filePath") or "")):
            content, info = try_download(session, url, token)
            result["download_attempts"].append(info)
            if content:
                pdf_bytes, success_info = content, info
                break
        if pdf_bytes is None:
            raise RuntimeError("Не удалось скачать PDF техспецификации")

        pdf_path = OUTPUT_DIR / "active_control_87888776.pdf"
        pdf_path.write_bytes(pdf_bytes)
        text = extract_pdf_text(pdf_bytes)
        (OUTPUT_DIR / "active_control_text.txt").write_text(text, encoding="utf-8")

        expected_ok = EXPECTED_TEXT.lower() in text.lower()
        result.update({
            "pdf_path": str(pdf_path),
            "pdf_source": success_info,
            "text_length": len(text),
            "expected_text": EXPECTED_TEXT,
            "expected_text_found": expected_ok,
            "text_excerpt": text[:4000],
            "stage": "passed" if expected_ok else "expected_text_missing",
            "ok": expected_ok,
        })
        if not expected_ok:
            raise RuntimeError(f"В PDF не найден контрольный текст: {EXPECTED_TEXT}")

        print("ACTIVE CONTROL PASS: API -> PDF -> TEXT", flush=True)
        print("LOT:", CONTROL_LOT, flush=True)
        print("EXPECTED:", EXPECTED_TEXT, "FOUND=", expected_ok, flush=True)
        print("PDF CHARS:", len(text), flush=True)

    except Exception as e:
        result["error"] = repr(e)
        print("ACTIVE CONTROL ERROR:", repr(e), flush=True)

    out = OUTPUT_DIR / "active_control_result.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("RESULT JSON:", out, flush=True)
    print("FINAL STATUS:", result.get("stage"), "OK=", result.get("ok"), flush=True)
    return 0 if result.get("ok") else 1

if __name__ == "__main__":
    raise SystemExit(main())
