"""TV Stage 2. This module deliberately has no provider import or argument."""
import urllib.request, xml.etree.ElementTree as ET
from ..events import emit
from ..images import safe_actor_filename, normalize_actor_image
from ..storage import load_json, save_json_atomic, write_bytes_atomic

def render_nfo(values):
    root=ET.Element("tvshow")
    for key,value in values.items():
        if key in ("actor","genre") or value in (None,"",[]): continue
        node=ET.SubElement(root,key); node.text=str(value)
    for genre in values.get("genre",[]): ET.SubElement(root,"genre").text=str(genre)
    for actor in values.get("actor",[]):
        node=ET.SubElement(root,"actor")
        for key in ("name","role","order"):
            if actor.get(key) is not None: ET.SubElement(node,key).text=str(actor[key])
    try: ET.indent(root,space="    ")
    except AttributeError: pass
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'+ET.tostring(root,encoding="utf-8")+b"\n"

def run(config, reporter=None, limit=None, overwrite_nfo=True, overwrite_poster=True, retry_failed=True, downloader=None, normalize=True):
    path=config.state_path("tv_show_urls_tvdb.json"); manifest=load_json(path,None)
    if not isinstance(manifest,dict) or not isinstance(manifest.get("shows"),dict): raise RuntimeError(f"No usable Stage 1 work file at {path}; run 'tv scan' first")
    def fetch(url):
        with urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"harvester/1"}),timeout=30) as response: return response.read(),response.headers.get("Content-Type","")
    fetch=downloader or fetch; actors_dir=config.tv_root/".actors"; actors_dir.mkdir(parents=True,exist_ok=True); processed=0
    for folder,record in sorted(manifest["shows"].items()):
        if record.get("status")!="matched" or not record.get("nfo"): continue
        if limit is not None and processed>=limit: break
        show=__import__('pathlib').Path(folder); state=record.setdefault("materialize",{})
        if not show.is_dir(): state["status"]="error"; state["error"]="show directory no longer exists"; continue
        nfo=show/"show.nfo"
        if not nfo.exists() or overwrite_nfo: write_bytes_atomic(nfo,render_nfo(record["nfo"])); state["nfo"]={"status":"ok"}
        else: state["nfo"]={"status":"exists"}
        assets=record.get("assets") or {}; poster=show/"poster.jpg"
        if poster.exists() and not overwrite_poster: state["poster"]={"status":"exists"}
        elif assets.get("poster_url"):
            try: data,ctype=fetch(assets["poster_url"]); write_bytes_atomic(poster,data); state["poster"]={"status":"ok"}
            except Exception as error: state["poster"]={"status":"error","error":repr(error)}
        else: state["poster"]={"status":"no_url"}
        errors=0
        for actor in assets.get("actor_urls") or []:
            output=actors_dir/safe_actor_filename(actor.get("name")); download=actor.setdefault("download",{})
            if output.exists(): download["status"]="exists"; continue
            if download.get("status")=="error" and not retry_failed: errors+=1; continue
            if not actor.get("url"): download["status"]="no_url"; continue
            try: data,_=fetch(actor["url"]); write_bytes_atomic(output,normalize_actor_image(data,normalize)); download["status"]="ok"
            except Exception as error: download.update(status="error",error=repr(error)); errors+=1
        state["status"]="partial" if errors or state["poster"]["status"]=="error" else "complete"
        processed+=1; save_json_atomic(path,manifest); emit(reporter,"progress",record.get("folder_name",folder),status=state["status"])
    save_json_atomic(path,manifest); return {"processed":processed}
