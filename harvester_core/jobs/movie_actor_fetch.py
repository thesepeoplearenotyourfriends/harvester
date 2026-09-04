import urllib.request
from datetime import datetime, timezone
from ..events import emit
from ..images import safe_actor_filename, normalize_actor_image
from ..storage import load_json, save_json_atomic, write_bytes_atomic

def run(config, reporter=None, limit=None, retry_failed=False, overwrite=False, downloader=None, normalize=True):
    urls=load_json(config.state_path("actor_thumb_urls_tmdb.json"),{})
    status_path=config.state_path("actor_photo_download_status.json")
    status=load_json(status_path,{})
    target=config.movie_root/".actors"; target.mkdir(parents=True,exist_ok=True)
    def fetch(url):
        with urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"harvester/1"}),timeout=30) as response:
            return response.read()
    fetch=downloader or fetch; processed=0
    for name in sorted(urls):
        if limit is not None and processed>=limit: break
        output=target/safe_actor_filename(name)
        if output.exists() and not overwrite:
            status[name]={"status":"exists","file":str(output),"bytes":output.stat().st_size}; continue
        if status.get(name,{}).get("status")=="failed" and not retry_failed: continue
        if not urls[name]: continue
        try:
            data=fetch(urls[name][0]); data=normalize_actor_image(data,normalize)
            if not data: raise RuntimeError("downloaded zero bytes")
            write_bytes_atomic(output,data); status[name]={"status":"ok","file":str(output),"bytes":len(data)}
        except Exception as error: status[name]={"status":"failed","error":repr(error)}
        processed+=1; save_json_atomic(status_path,status); emit(reporter,"progress",name,status=status[name]["status"])
    save_json_atomic(status_path,status)
    return {"processed":processed,"statuses":status}
