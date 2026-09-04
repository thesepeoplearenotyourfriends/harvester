"""Scan movie NFO receipts and durably resolve actor photos using movie context."""
import re, unicodedata, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from ..events import emit
from ..storage import load_json, save_json_atomic

def norm(value):
    value=unicodedata.normalize("NFKD",str(value or "")); value="".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"\s+"," ",re.sub(r"[^a-z0-9]+"," ",value.lower().replace("&"," and "))).strip()

def _parse(path):
    root=ET.parse(path).getroot(); actors=[]
    for node in root.findall("actor"):
        name=node.findtext("name")
        if name: actors.append(name.strip())
    ids={n.attrib.get("type"): (n.text or "").strip() for n in root.findall("uniqueid")}
    year=root.findtext("year") or root.findtext("premiered") or ""
    match=re.search(r"(?:19|20)\d{2}",year)
    return {"title":root.findtext("title") or root.findtext("originaltitle"),"year":int(match.group()) if match else None,"tmdb_id":ids.get("tmdb"),"imdb_id":ids.get("imdb"),"actors":actors,"nfo":str(path)}

def run(config, provider, reporter=None, limit=None, rebuild=False, refresh=False, fallback=False):
    path=config.state_path("movie_actor_queue.json")
    queue={} if rebuild else load_json(path,{})
    for nfo in sorted(config.movie_root.rglob("*.nfo")):
        key=str(nfo); old=queue.get(key)
        if old and old.get("status")=="matched" and not refresh: continue
        try: movie=_parse(nfo)
        except Exception as e: queue[key]={"status":"error","error":repr(e)}; continue
        queue.setdefault(key,{"movie":movie,"actors":{}})["movie"]=movie
    save_json_atomic(path,queue)
    processed=0
    for key,item in queue.items():
        if limit is not None and processed>=limit: break
        if item.get("status")=="matched" and not refresh: continue
        old=dict(item)
        try:
            movie=item["movie"]; movie_id=movie.get("tmdb_id")
            if not movie_id and movie.get("imdb_id"):
                found=provider.get("/find/"+movie["imdb_id"],{"external_source":"imdb_id"}).get("movie_results") or []
                movie_id=found[0].get("id") if len(found)==1 else None
            if not movie_id: raise RuntimeError("movie could not be conservatively resolved")
            credits=provider.get(f"/movie/{movie_id}/credits").get("cast") or []
            actors={}
            for wanted in movie["actors"]:
                matches=[p for p in credits if norm(p.get("name"))==norm(wanted)]
                if len(matches)==1 and matches[0].get("profile_path"):
                    actors[wanted]=["https://image.tmdb.org/t/p/w185"+matches[0]["profile_path"]]
                elif fallback:
                    # Person search is intentionally opt-in and still requires
                    # one exact normalized name; popularity is never a match.
                    people=provider.get("/search/person",{"query":wanted}).get("results") or []
                    people=[p for p in people if norm(p.get("name"))==norm(wanted)]
                    if len(people)==1 and people[0].get("profile_path"):
                        actors[wanted]=["https://image.tmdb.org/t/p/w185"+people[0]["profile_path"]]
            item.update(status="matched",tmdb_id=movie_id,actors=actors)
            item.pop("error",None)
        except Exception as e:
            # A refresh is advisory: never replace known-good durable work with a wobble.
            if old.get("status") != "matched": item.update(status="error",error=repr(e))
        processed+=1; save_json_atomic(path,queue); emit(reporter,"progress",key,status=item.get("status"))
    urls={}
    for item in queue.values():
        for name, value in item.get("actors",{}).items():
            urls.setdefault(name,[])
            for url in value:
                if url not in urls[name]: urls[name].append(url)
    save_json_atomic(config.state_path("actor_thumb_urls_tmdb.json"),urls)
    return {"processed":processed,"queue":len(queue),"actors":len(urls)}
