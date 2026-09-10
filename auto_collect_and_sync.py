# -*- coding: utf-8 -*-
import os, sys, json, subprocess, time, getpass, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT/"output"
LOGS = ROOT/"logs"
LOGS.mkdir(exist_ok=True)

def read_cfg(path):
    cfg={}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line=line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k,v=line.split("=",1)
            cfg[k.strip()]=v.strip()
    except Exception:
        pass
    return cfg

def find_supabase_config():
    env_cfg = {
        "SUPABASE_URL": os.getenv("SUPABASE_URL", "").strip(),
        "SUPABASE_SERVICE_ROLE_KEY": os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip(),
    }
    if env_cfg["SUPABASE_URL"] and env_cfg["SUPABASE_SERVICE_ROLE_KEY"]:
        return "environment", env_cfg

    candidates=[ROOT/"config.txt"]
    try:
        candidates += list(ROOT.parent.glob("*/config.txt"))
    except Exception:
        pass
    for p in candidates:
        if p.exists():
            c=read_cfg(p)
            if c.get("SUPABASE_URL") and c.get("SUPABASE_SERVICE_ROLE_KEY"):
                return p,c
    return None,{}

def ensure_token():
    token=os.getenv("GOSZAKUP_TOKEN","").strip()
    if token:
        return token

    if os.getenv("GITHUB_ACTIONS","").lower()=="true":
        raise RuntimeError("GitHub Secret GOSZAKUP_TOKEN is not configured")

    p=ROOT/"auto_config.txt"
    cfg=read_cfg(p) if p.exists() else {}
    token=cfg.get("GOSZAKUP_TOKEN","").strip()
    if token:
        return token

    print()
    print("ONE-TIME LOCAL SETUP FOR GOSZAKUP")
    token=getpass.getpass("GOSZAKUP_TOKEN: ").strip()
    if not token:
        raise RuntimeError("Goszakup token is empty")
    p.write_text("# ProcureVision AUTO local settings\nGOSZAKUP_TOKEN="+token+"\n",encoding="utf-8")
    print("Token saved locally in auto_config.txt.")
    return token

def run_collector(name, script, env=None):
    log=LOGS/(name+"_collector.log")
    print()
    print("="*64)
    print("COLLECT:",name.upper())
    print("="*64)

    e=os.environ.copy()
    # Child collectors only collect to JSON. Parent alone writes to Supabase.
    e.pop("SUPABASE_URL", None)
    e.pop("SUPABASE_SERVICE_ROLE_KEY", None)
    if env:
        e.update(env)

    p=subprocess.Popen([sys.executable, str(ROOT/script)], cwd=str(ROOT), env=e,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, bufsize=1)
    lines=[]
    for line in p.stdout:
        print(line.rstrip())
        lines.append(line)
    code=p.wait()
    log.write_text("".join(lines),encoding="utf-8",errors="replace")
    if code!=0:
        raise RuntimeError("%s collector failed with code %s" % (name,code))

def nempty(v):
    if v is None: return None
    if isinstance(v,str):
        v=v.strip()
        return v if v else None
    return v

def iso_or_none(v):
    return nempty(v)


def dt(v):
    if not v:
        return None
    import re as _re
    s=str(v)
    m=_re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", s)
    if m:
        return m.group(0).replace(" ","T")
    m=_re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", s)
    if m:
        return m.group(0)
    return None

def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))

def make_mitwork():
    ann=load_json(OUT/"mitwork_announcements.json")
    out=[]
    for a in ann:
        if not isinstance(a,dict):
            continue
        tender_id=str(a.get("external_id") or a.get("announcement_number") or "").strip()
        lots=a.get("lots") if isinstance(a.get("lots"),list) else []
        if not lots:
            lots=[{}]
        for i,lot in enumerate(lots,1):
            lot_number=str(lot.get("lot_number") or i).strip()
            source_lot_id=(tender_id+":"+lot_number) if tender_id else lot_number
            subject=str(lot.get("subject") or a.get("title") or "").strip()
            desc=str(lot.get("description") or "").strip()
            out.append({
                "source_code":"samruk",
                "source_tender_id":tender_id or None,
                "source_lot_id":source_lot_id or None,
                "public_url":a.get("public_url"),
                "title":subject or str(a.get("title") or "").strip(),
                "description":desc,
                "customer_name":a.get("organizer"),
                "customer_bin":None,
                "region":None,
                "procurement_method":a.get("procurement_method"),
                "status_code":None,
                "status_name":a.get("status"),
                "amount":lot.get("total_price_kzt") if lot.get("total_price_kzt") is not None else a.get("amount_kzt"),
                "currency":"KZT",
                "quantity":lot.get("quantity"),
                "unit":None,
                "category":a.get("procurement_type"),
                "published_at":None,
                "started_at":dt(a.get("start_date")),
                "expires_at":dt(a.get("end_date")),
                "is_active":str(a.get("status") or "").lower() in ("опубликовано","прием заявок","приём заявок"),
                "raw":{"announcement":a,"lot":lot}
            })
    return out

def make_gos():
    rows=load_json(OUT/"tenders.json")
    out=[]
    for r in rows:
        ext=str(r.get("external_id") or "").strip()
        lotnum=str(r.get("lot_number") or "").strip()
        ann=str(r.get("announcement_number") or "").strip()
        sid=ext or lotnum or ann
        status=str(r.get("status") or "")
        out.append({
          "source_code":"goszakup","source_tender_id":ann or None,"source_lot_id":sid or None,
          "public_url":nempty(r.get("public_url")),"title":nempty(r.get("title")),
          "description":nempty(r.get("description")),"customer_name":nempty(r.get("customer_name")),
          "customer_bin":nempty(r.get("customer_bin")),"region":None,
          "procurement_method":nempty(r.get("trade_method")),"status_code":None,
          "status_name":nempty(status),"amount":r.get("amount_kzt"),"currency":"KZT",
          "quantity":r.get("quantity"),"unit":None,"category":None,
          "published_at":iso_or_none(r.get("publish_date")),"started_at":iso_or_none(r.get("start_date")),
          "expires_at":iso_or_none(r.get("end_date")),
          "is_active":("прием" in status.lower() or "приём" in status.lower()),"raw":r
        })
    return out

def upload_rows(cfg, source, rows):
    endpoint=cfg["SUPABASE_URL"].rstrip("/")+"/rest/v1/tenders?on_conflict=source_code,source_lot_id"
    key=cfg["SUPABASE_SERVICE_ROLE_KEY"]
    headers={"apikey":key,"Authorization":"Bearer "+key,"Content-Type":"application/json",
             "Prefer":"resolution=merge-duplicates,return=minimal"}
    batch_size=100
    sent=0
    for start in range(0,len(rows),batch_size):
        batch=rows[start:start+batch_size]
        req=urllib.request.Request(endpoint,
            data=json.dumps(batch,ensure_ascii=False).encode("utf-8"),
            headers=headers,method="POST")
        try:
            with urllib.request.urlopen(req,timeout=120) as resp:
                resp.read()
            sent += len(batch)
            print("SYNC",source,":",sent,"/",len(rows))
        except urllib.error.HTTPError as e:
            body=e.read().decode("utf-8",errors="replace")
            print("SUPABASE HTTP ERROR",source,":",e.code)
            print(body)
            raise
    return sent

def update_source(cfg, source, ok=True, err=None):
    endpoint=cfg["SUPABASE_URL"].rstrip("/")+"/rest/v1/sources?code=eq."+source
    key=cfg["SUPABASE_SERVICE_ROLE_KEY"]
    body={"last_run_at":time.strftime("%Y-%m-%dT%H:%M:%SZ")}
    if ok:
        body["last_success_at"]=body["last_run_at"]; body["last_error"]=None; body["status"]="ready"
    else:
        body["last_error"]=str(err); body["status"]="error"
    req=urllib.request.Request(endpoint,data=json.dumps(body).encode("utf-8"),
        headers={"apikey":key,"Authorization":"Bearer "+key,"Content-Type":"application/json","Prefer":"return=minimal"},
        method="PATCH")
    try:
        with urllib.request.urlopen(req,timeout=30) as resp: resp.read()
    except Exception:
        pass

def main():
    cfg_path,cfg=find_supabase_config()
    if not cfg:
        print("ERROR: configured Supabase config.txt not found.")
        print("Put this AUTO folder beside your previous ProcureVision folders.")
        return 2
    print("Supabase config source:",cfg_path)
    token=ensure_token()

    jobs=[
     # ("mitwork","mitwork_collector_v2.py",{},make_mitwork),
      ("goszakup","collector_goszakup.py",{"GOSZAKUP_TOKEN":token,"TOKEN":token,"API_TOKEN":token},make_gos),
    ]

    totals={}
    for name,script,env,maker in jobs:
        public_source = "samruk" if name=="mitwork" else name
        try:
            run_collector(name,script,env)
            rows=maker()
            print("Prepared for Supabase:",len(rows))
            if name=="goszakup" and len(rows)==0:
                print("WARNING: Goszakup returned 0 current keyword matches.")
                print("Existing Goszakup rows in Supabase are NOT deleted.")
                totals[public_source]=0
                update_source(cfg,public_source,True)
            else:
                totals[public_source]=upload_rows(cfg,public_source,rows)
                update_source(cfg,public_source,True)
        except Exception as e:
            print("ERROR",public_source,":",repr(e))
            update_source(cfg,public_source,False,e)
            totals[public_source]="ERROR"

    print()
    print("="*64)
    print("PROCUREVISION AUTO RESULT")
    print("="*64)
    for k,v in totals.items():
        print(k,":",v)
    print()
    if all(v!="ERROR" for v in totals.values()):
        print("SUCCESS: Samruk and Goszakup update completed.")
        return 0
    print("DONE WITH ERRORS. See logs folder.")
    return 1

if __name__=="__main__":
    sys.exit(main())
