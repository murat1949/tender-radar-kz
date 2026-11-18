# -*- coding: utf-8 -*-

import os
import sys
import json
import subprocess
import time
from datetime import datetime, timezone, timedelta
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
LOGS = ROOT / "logs"
OUT.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)


def read_cfg(path):
    cfg = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    except Exception:
        pass
    return cfg


def get_config():
    cfg = {
        "SUPABASE_URL": os.getenv("SUPABASE_URL", "").strip(),
        "SUPABASE_SERVICE_ROLE_KEY": os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip(),
        "GOSZAKUP_TOKEN": os.getenv("GOSZAKUP_TOKEN", "").strip(),
    }

    if not cfg["SUPABASE_URL"] or not cfg["SUPABASE_SERVICE_ROLE_KEY"]:
        for p in (ROOT / "config.txt", ROOT.parent / "config.txt"):
            if not p.exists():
                continue
            local = read_cfg(p)
            if not cfg["SUPABASE_URL"]:
                cfg["SUPABASE_URL"] = local.get("SUPABASE_URL", "").strip()
            if not cfg["SUPABASE_SERVICE_ROLE_KEY"]:
                cfg["SUPABASE_SERVICE_ROLE_KEY"] = local.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()

    if not cfg["GOSZAKUP_TOKEN"]:
        p = ROOT / "auto_config.txt"
        if p.exists():
            local = read_cfg(p)
            cfg["GOSZAKUP_TOKEN"] = local.get("GOSZAKUP_TOKEN", "").strip()

    missing = [k for k in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "GOSZAKUP_TOKEN") if not cfg.get(k)]
    if missing:
        raise RuntimeError("Missing configuration: " + ", ".join(missing))
    return cfg


def nempty(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        return v if v else None
    return v



KZ_TZ = timezone(timedelta(hours=5))


def normalize_goszakup_datetime(v):
    """
    Goszakup GraphQL returns Kazakhstan wall-clock timestamps without an offset.
    Treat naive values as Kazakhstan time (UTC+5) before sending them to Supabase,
    otherwise PostgreSQL may interpret them as UTC and the portal shifts them again.
    """
    s = nempty(v)
    if not s:
        return None
    raw = str(s).strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KZ_TZ)
        return dt.isoformat()
    except Exception:
        return raw


def goszakup_is_active(status, expires_at):
    st = status.lower().strip()
    # «Опубликован» — активный статус новой платформы zakup.gov.kz (status_id 6),
    # приём ценовых предложений по нему идёт до offer_end_date.
    status_active = ("прием" in st or "приём" in st or st == "опубликован")
    if not status_active:
        return False
    if not expires_at:
        return True
    try:
        exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=KZ_TZ)
        return exp > datetime.now(timezone.utc)
    except Exception:
        return status_active

def load_json(path):
    if not path.exists():
        raise RuntimeError("Output file not found: %s" % path)
    return json.loads(path.read_text(encoding="utf-8"))


def run_script(label, script_name, env_extra=None):
    script = ROOT / script_name
    if not script.exists():
        raise RuntimeError("Script not found: %s" % script_name)

    print()
    print("=" * 70)
    print("COLLECT:", label.upper())
    print("SCRIPT :", script_name)
    print("=" * 70)

    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)

    p = subprocess.Popen(
        [sys.executable, "-u", str(script)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
        bufsize=1,
    )

    lines = []
    for line in p.stdout:
        print(line.rstrip())
        lines.append(line)
    code = p.wait()

    (LOGS / (label + "_collector.log")).write_text(
        "".join(lines), encoding="utf-8", errors="replace"
    )

    if code != 0:
        raise RuntimeError("%s collector failed with code %s" % (label, code))


def make_samruk():
    rows = load_json(OUT / "samruk_tenders.json")
    out = []

    for r in rows:
        if not isinstance(r, dict):
            continue

        lot_id = str(r.get("source_lot_id") or "").strip()
        if not lot_id:
            continue

        raw = r.get("raw") if isinstance(r.get("raw"), dict) else {}
        remaining = str(raw.get("remaining_text") or "").strip()
        expires_at = nempty(r.get("expires_at"))

        # В Samruk актуальная карточка в поиске обычно содержит "Осталось: ...".
        # Если этого поля нет, используем точную дату окончания, когда она известна.
        is_active = bool(remaining)

        if not is_active and expires_at:
            try:
                exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                is_active = exp > datetime.now(exp.tzinfo)
            except Exception:
                is_active = False

        out.append({
            "source_code": "samruk",
            "source_tender_id": nempty(r.get("source_tender_id")),
            "source_lot_id": lot_id,
            "public_url": nempty(r.get("public_url")),
            "title": nempty(r.get("title")),
            "description": nempty(r.get("description")),
            "customer_name": nempty(r.get("customer_name")),
            "customer_bin": nempty(r.get("customer_bin")),
            "region": nempty(r.get("region")),
            "procurement_method": nempty(r.get("procurement_method")),
            "status_code": nempty(r.get("status_code")),
            "status_name": nempty(r.get("status_name")) or "Опубликовано",
            "amount": r.get("amount"),
            "currency": nempty(r.get("currency")) or "KZT",
            "quantity": r.get("quantity"),
            "unit": nempty(r.get("unit")),
            "category": nempty(r.get("category")),
            "published_at": nempty(r.get("published_at")),
            "started_at": nempty(r.get("started_at")),
            "expires_at": expires_at,
            "is_active": is_active,
            "raw": raw if raw else r,
        })

    return out



def validate_samruk_lots(rows):
    """
    Защита от возврата к старой ошибочной логике "закупка вместо лота".
    В основной sync допускаются только строки Samruk уровня LOT.
    """
    bad = []
    for r in rows:
        lot_id = str(r.get("source_lot_id") or "").strip()
        url = str(r.get("public_url") or "").strip()
        if not lot_id or "/lot" not in url:
            bad.append((lot_id, url))

    if bad:
        preview = bad[:5]
        raise RuntimeError(
            "SAMRUK ARCHITECTURE CHECK FAILED: "
            f"{len(bad)} rows are not LOT-level. Sample: {preview}"
        )

    print("SAMRUK ARCHITECTURE CHECK: OK - LOT level only")
    return True


def make_goszakup():
    rows = load_json(OUT / "tenders.json")
    out = []

    for r in rows:
        if not isinstance(r, dict):
            continue

        ext = str(r.get("external_id") or "").strip()
        lotnum = str(r.get("lot_number") or "").strip()
        ann = str(r.get("announcement_number") or "").strip()
        sid = ext or lotnum or ann
        if not sid:
            continue

        status = str(r.get("status") or "")
        published_at = normalize_goszakup_datetime(r.get("publish_date"))
        started_at = normalize_goszakup_datetime(r.get("start_date"))
        expires_at = normalize_goszakup_datetime(r.get("end_date"))
        is_active = goszakup_is_active(status, expires_at)

        out.append({
            "source_code": "goszakup",
            "source_tender_id": ann or None,
            "source_lot_id": sid,
            "public_url": nempty(r.get("public_url")),
            "title": nempty(r.get("title")),
            "description": nempty(r.get("description")),
            "customer_name": nempty(r.get("customer_name")),
            "customer_bin": nempty(r.get("customer_bin")),
            "region": nempty(r.get("region")),
            "procurement_method": nempty(r.get("trade_method")),
            "status_code": nempty(r.get("status_code")),
            "status_name": nempty(status),
            "amount": r.get("amount_kzt"),
            "currency": "KZT",
            "quantity": r.get("quantity"),
            "unit": nempty(r.get("unit")),
            "category": nempty(r.get("category")),
            "published_at": published_at,
            "started_at": started_at,
            "expires_at": expires_at,
            "is_active": is_active,
            "raw": r,
        })

    return out



def deactivate_existing_source(cfg, source):
    """
    Историю не удаляем. Перед успешной загрузкой свежего набора
    помечаем прежние активные строки источника как неактивные.
    Это убирает старые Samruk-записи из рабочего экрана.
    """
    endpoint = (
        cfg["SUPABASE_URL"].rstrip("/")
        + "/rest/v1/tenders?source_code=eq."
        + source
        + "&is_active=eq.true"
    )
    key = cfg["SUPABASE_SERVICE_ROLE_KEY"]

    req = urllib.request.Request(
        endpoint,
        data=json.dumps({"is_active": False}).encode("utf-8"),
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        method="PATCH",
    )

    with urllib.request.urlopen(req, timeout=120) as resp:
        resp.read()

    print("STALE", source, ": previous active rows -> inactive")



def upload_rows(cfg, source, rows):
    if not rows:
        print("WARNING:", source, "returned 0 rows.")
        print("Existing rows in Supabase are NOT deleted.")
        return 0

    endpoint = (
        cfg["SUPABASE_URL"].rstrip("/")
        + "/rest/v1/tenders?on_conflict=source_code,source_lot_id"
    )
    key = cfg["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }

    sent = 0
    for start in range(0, len(rows), 100):
        batch = rows[start:start + 100]
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(batch, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                resp.read()
            sent += len(batch)
            print("SYNC", source, ":", sent, "/", len(rows))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print("SUPABASE HTTP ERROR", source, ":", e.code)
            print(body)
            raise

    return sent


def update_source(cfg, source, ok=True, err=None):
    endpoint = cfg["SUPABASE_URL"].rstrip("/") + "/rest/v1/sources?code=eq." + source
    key = cfg["SUPABASE_SERVICE_ROLE_KEY"]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {"last_run_at": now}

    if ok:
        body["last_success_at"] = now
        body["last_error"] = None
        body["status"] = "ready"
    else:
        body["last_error"] = str(err)
        body["status"] = "error"

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        method="PATCH",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except Exception as e:
        print("WARNING: source status update failed:", source, repr(e))


SAMRUK_KEYWORDS = [
    "картридж",
    "тонер",
    "расходные материалы для оргтехники",
]

GOSZAKUP_KEYWORDS = [
    "картридж",
    "тонер",
    "заправка картриджей",
    "расходные материалы для оргтехники",
]


def collect_goszakup_via_zakup():
    """Открытые лоты Госзакупа с zakup.gov.kz -> OUT/tenders.json
    (тот же формат, что у collector_goszakup.py, его читает make_goszakup)."""
    import collector_zakup

    raw = os.getenv("GOSZAKUP_KEYWORDS", "")
    keywords = [x.strip() for x in raw.split(",") if x.strip()] or GOSZAKUP_KEYWORDS
    items = []
    failed = 0
    for keyword in keywords:
        try:
            found = collector_zakup.collect_keyword(keyword)
            print("[zakup]", keyword, "- open lots:", len(found))
            items.extend(found)
        except Exception as e:
            failed += 1
            print("[zakup]", keyword, "- ERROR:", repr(e))
    if failed == len(keywords):
        raise RuntimeError("zakup.gov.kz did not answer for any keyword")
    try:
        recent = collector_zakup.crawl_recent(keywords, hours=8)
        print("[zakup] recent crawl matched:", len(recent))
        items.extend(recent)
    except Exception as e:
        print("[zakup] recent crawl ERROR:", repr(e))
    items = collector_zakup.deduplicate(items)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "tenders.json").write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[zakup] unique open lots:", len(items))


def main():
    print("=" * 70)
    print("TENDER RADAR KZ - CLOUD AUTO COLLECT - LOT ARCHITECTURE + STALE CLEANUP")
    print("SOURCES: SAMRUK + GOSZAKUP")
    print("MITWORK: DISABLED")
    print("=" * 70)

    try:
        cfg = get_config()
    except Exception as e:
        print("CONFIG ERROR:", repr(e))
        return 2

    totals = {}

    try:
        old = OUT / "samruk_tenders.json"
        if old.exists():
            old.unlink()

        # С 17.11.2026 сайт zakup.sk.kz не пускает зарубежные серверы. Лоты Самрука
        # берём с открытого API zakup.gov.kz (система SKK, номера лотов те же).
        # Браузерный сборщик по сайту Самрука остаётся запасным.
        try:
            import collector_zakup
            sk_rows = collector_zakup.collect_samruk(SAMRUK_KEYWORDS)
            OUT.mkdir(parents=True, exist_ok=True)
            (OUT / "samruk_tenders.json").write_text(
                json.dumps(sk_rows, ensure_ascii=False, indent=2), encoding="utf-8")
            print("[zakup] samruk open lots:", len(sk_rows))
        except Exception as zakup_error:
            print("WARNING samruk via zakup.gov.kz failed, fallback to browser:", repr(zakup_error))
            run_script(
                "samruk",
                "samruk_collector.py",
                {
                    "SAMRUK_DETAIL_LIMIT": os.getenv("SAMRUK_DETAIL_LIMIT", "0"),
                },
            )
        rows = make_samruk()
        validate_samruk_lots(rows)

        active_rows = sum(1 for r in rows if r.get("is_active") is True)
        print("Prepared for Supabase:", len(rows))
        print("Samruk current active lots:", active_rows)

        if not rows:
            raise RuntimeError("Samruk returned 0 rows - old data left untouched")
        if active_rows == 0:
            raise RuntimeError(
                "Samruk returned 0 current active lots - old data left untouched"
            )

        # Без удаления истории: старые активные строки гасим,
        # затем свежие лоты upsert-им обратно с актуальным is_active.
        deactivate_existing_source(cfg, "samruk")
        totals["samruk"] = upload_rows(cfg, "samruk", rows)
        update_source(cfg, "samruk", True)

    except Exception as e:
        print("ERROR samruk:", repr(e))
        update_source(cfg, "samruk", False, e)
        totals["samruk"] = "ERROR"

    try:
        old = OUT / "tenders.json"
        if old.exists():
            old.unlink()

        # С осени 2026 старый вход ows.goszakup.gov.kz отваливается (403, обрывы).
        # Основной источник — открытый API Единой платформы zakup.gov.kz (без токена).
        # Старый сборщик остаётся запасным, если новая платформа не ответила.
        try:
            collect_goszakup_via_zakup()
        except Exception as zakup_error:
            print("WARNING zakup.gov.kz failed, fallback to ows:", repr(zakup_error))
            token = cfg["GOSZAKUP_TOKEN"]
            run_script(
                "goszakup",
                "collector_goszakup.py",
                {
                    "GOSZAKUP_TOKEN": token,
                    "TOKEN": token,
                    "API_TOKEN": token,
                },
            )

        rows = make_goszakup()
        active_rows = sum(1 for r in rows if r.get("is_active") is True)
        print("Prepared for Supabase:", len(rows))
        print("Goszakup current active lots:", active_rows)

        if not rows:
            raise RuntimeError("Goszakup returned 0 rows - old data left untouched")

        # Keep history, but prevent old rows that disappeared from the latest source
        # from staying active forever.
        deactivate_existing_source(cfg, "goszakup")
        totals["goszakup"] = upload_rows(cfg, "goszakup", rows)
        update_source(cfg, "goszakup", True)

    except Exception as e:
        print("ERROR goszakup:", repr(e))
        update_source(cfg, "goszakup", False, e)
        totals["goszakup"] = "ERROR"

    print()
    print("=" * 70)
    print("TENDER RADAR KZ - AUTO RESULT")
    print("=" * 70)
    for source, total in totals.items():
        print(source, ":", total)
    print()

    if all(v != "ERROR" for v in totals.values()):
        print("SUCCESS: Samruk and Goszakup update completed.")
        return 0

    print("DONE WITH ERRORS. See collector logs.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
