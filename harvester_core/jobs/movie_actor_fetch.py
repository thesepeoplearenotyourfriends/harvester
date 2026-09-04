"""Materialize the frozen TMDB actor URL list."""
import urllib.request

from ..events import emit
from ..images import normalize_actor_image, safe_actor_filename
from ..storage import load_json, save_json_atomic, write_bytes_atomic


def run(
    config,
    reporter=None,
    limit=None,
    retry_failed=False,
    overwrite=False,
    downloader=None,
    normalize=True,
):
    urls = load_json(config.state_path("actor_thumb_urls_tmdb.json"), {})
    status_path = config.state_path("actor_photo_download_status.json")
    statuses = load_json(status_path, {})
    target_dir = config.movie_root / ".actors"
    target_dir.mkdir(parents=True, exist_ok=True)

    def fetch(url):
        request = urllib.request.Request(url, headers={"User-Agent": "harvester/1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()

    fetch = downloader or fetch
    processed = 0
    counts = {"ok": 0, "failed": 0, "exists": 0}
    for name in sorted(urls):
        if limit is not None and processed >= limit:
            break
        output = target_dir / safe_actor_filename(name)
        if output.exists() and not overwrite:
            statuses[name] = {
                "status": "exists", "file": str(output),
                "bytes": output.stat().st_size,
            }
            counts["exists"] += 1
            continue
        if statuses.get(name, {}).get("status") == "failed" and not retry_failed:
            continue
        if not urls[name]:
            continue
        try:
            source = fetch(urls[name][0])
            if not source:
                raise RuntimeError("downloaded zero bytes")
            data = normalize_actor_image(source, normalize)
            write_bytes_atomic(output, data)
            statuses[name] = {
                "status": "ok", "file": str(output), "bytes": len(data),
                "source_bytes": len(source), "url": urls[name][0],
            }
            counts["ok"] += 1
        except Exception as error:
            statuses[name] = {"status": "failed", "error": repr(error)}
            counts["failed"] += 1
        processed += 1
        save_json_atomic(status_path, statuses)
        emit(reporter, "progress", name, status=statuses[name]["status"])
    save_json_atomic(status_path, statuses)
    return {"processed": processed, "total": len(urls), "counts": counts}
