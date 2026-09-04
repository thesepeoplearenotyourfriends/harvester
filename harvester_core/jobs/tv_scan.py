"""TV Stage 1: cautious resolution into a frozen, URL-bearing manifest."""
import re, unicodedata
from datetime import datetime, timezone
from ..events import emit
from ..storage import load_json, save_json_atomic

def now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def norm(value):
    value=unicodedata.normalize("NFKD",str(value or "")); value="".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"\s+"," ",re.sub(r"[^a-z0-9]+"," ",value.lower().replace("&"," and "))).strip()
def parse_folder(name):
    match=re.match(r"^(.*?)\s*(?:-|–|—)\s*season\s*\d+(?:\s*[\[(]((?:18|19|20)\d{2})[\])])?\s*$",name,re.I)
    raw=(match.group(1) if match else name).strip(); trailing=match.group(2) if match else None
    year=re.match(r"^(.*?)\s*[\[(]((?:18|19|20)\d{2})[\])]\s*$",raw)
    return ((year.group(1).strip() if year else raw), int(trailing or year.group(2)) if trailing or year else None)
def resolve(provider,title,year):
    params={"query":title,"type":"series","limit":10}
    if year: params["year"]=year
    rows,_=provider.get("/search",params); rows=[x for x in (rows or []) if str(x.get("type") or "series").lower()=="series"]
    scored=[]
    for row in rows:
        names=[row.get("name"),row.get("title"),*(row.get("aliases") or [])]
        exact=norm(title) in [norm(x) for x in names if x]
        candidate_year=str(row.get("year") or row.get("first_air_time") or "")[:4]
        candidate_year=int(candidate_year) if candidate_year.isdigit() else None
        score=(80 if exact else 0)+(35 if year and candidate_year==year else 0)
        scored.append((score,exact,candidate_year,row))
    scored.sort(key=lambda x:x[0],reverse=True)
    exact_count=sum(x[1] for x in scored)
    accepted=bool(scored and ((year and scored[0][1] and scored[0][2]==year) or (not year and scored[0][1] and exact_count==1)))
    if not accepted: return None,"not_found" if not scored else "ambiguous",[{"name":x[3].get("name"),"year":x[2],"score":x[0]} for x in scored[:8]]
    return scored[0][3],"matched",[]
def _nfo(details):
    return {"title":details.get("name"),"originaltitle":details.get("name"),"year":str(details.get("year") or "")[:4],"plot":details.get("overview"),"tvdbId":details.get("id"),"genre":[x.get("name") for x in details.get("genres",[]) if x.get("name")],"actor":[{"name":x.get("personName"),"role":x.get("name")} for x in details.get("characters",[]) if x.get("personName")]}
def run(config, provider, reporter=None, limit=None, rebuild=False, refresh=False, retry_errors=True, retry_ambiguous=False, retry_not_found=False):
    path=config.state_path("tv_show_urls_tvdb.json"); manifest={"_meta":{"library_root":str(config.tv_root)},"shows":{}} if rebuild else load_json(path,None)
    if not isinstance(manifest,dict): manifest={"_meta":{"library_root":str(config.tv_root)},"shows":{}}
    shows=manifest.setdefault("shows",{})
    for folder in sorted((x for x in config.tv_root.iterdir() if x.is_dir() and not x.name.startswith(".")),key=lambda x:x.name.casefold()):
        shows.setdefault(str(folder),{"folder_name":folder.name,"status":"pending"})
    processed=0
    for folder,record in shows.items():
        status=record.get("status")
        allowed=status in (None,"pending") or (status=="error" and retry_errors) or (status=="ambiguous" and retry_ambiguous) or (status=="not_found" and retry_not_found) or (status=="matched" and refresh)
        if not allowed: continue
        if limit is not None and processed>=limit: break
        old=dict(record)
        try:
            title,year=parse_folder(record["folder_name"]); selected,status,candidates=resolve(provider,title,year)
            if status!="matched": record.update(status=status,candidates=candidates,updated=now())
            else:
                sid=selected.get("tvdb_id") or selected.get("id"); details,_=provider.get(f"/series/{sid}/extended")
                characters=details.get("characters") or []
                record.update(status="matched",tvdb_id=sid,nfo=_nfo(details),assets={"poster_url":details.get("image"),"actor_urls":[{"name":x.get("personName"),"url":x.get("image")} for x in characters if x.get("personName")]},updated=now())
        except Exception as error:
            if old.get("status")=="matched": record.clear(); record.update(old)
            else: record.update(status="error",error=repr(error),updated=now())
        processed+=1; manifest["_meta"]["updated"]=now(); save_json_atomic(path,manifest); emit(reporter,"progress",record["folder_name"],status=record["status"])
    save_json_atomic(path,manifest); return {"processed":processed,"shows":len(shows)}
