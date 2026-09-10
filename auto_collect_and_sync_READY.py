# -*- coding: utf-8 -*-

import os
import sys
import json
import subprocess
import time
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
            "expires_at": nempty(r.get("expires_at")),
            "is_active": bool(r.get("is_active", True)),
            "raw": r.get("raw") if isinstance(r.get("raw"), dict) else r,
        })

    return out


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
            "published_at": nempty(r.get("publish_date")),
            "started_at": nempty(r.get("start_date")),
            "expires_at": nempty(r.get("end_date")),
            "is_active": ("прием" in status.lower() or "приём" in status.lower()),
            "raw": r,
        })

    return out


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


def main():
    print("=" * 70)
    print("TENDER RADAR KZ - CLOUD AUTO COLLECT")
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

        run_script("samruk", "samruk_collector_v3.py")
        rows = make_samruk()
        print("Prepared for Supabase:", len(rows))
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
        print("Prepared for Supabase:", len(rows))
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
