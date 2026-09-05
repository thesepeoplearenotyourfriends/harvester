"""Materialize the frozen TMDB actor URL list."""
from datetime import datetime, timezone
import copy
import time
import urllib.request

from ..events import emit
from ..images import normalize_actor_image, safe_actor_filename
from ..artifacts import planned, use_committer
from ..storage import load_json, save_json_atomic


def run(
    config,
    reporter=None,
    limit=None,
    retry_failed=False,
    overwrite=False,
    downloader=None,
    normalize=True,
    transport=None,
    request_timeout=30,
    sleep_between=0.0,
    save_every=25,
    sleep=None,
    targets=None,
    committer=None,
):
    committer = use_committer(committer)
    urls = load_json(config.state_path("actor_thumb_urls_tmdb.json"), {})
    status_path = config.state_path("actor_photo_download_status.json")
    statuses = copy.deepcopy(load_json(status_path, {}))
    target_dir = config.movie_root / ".actors"
    committer.mkdir(target_dir)

    def fetch(url):
        request = urllib.request.Request(url, headers={
            "User-Agent": "local-tmdb-actor-photo-gulper/1.0",
            "Accept": "image/jpeg,image/*,*/*",
        })
        opener = transport.open if transport else urllib.request.urlopen
        with opener(request, timeout=request_timeout) as response:
            return response.read(), response.headers.get("Content-Type", "")

    fetch = downloader or fetch
    processed = 0
    changed = 0
    sleep = sleep or time.sleep
    counts = {"ok": 0, "failed": 0, "exists": 0}
    selected = {value.casefold() for value in (targets or [])}
    for name in sorted(urls):
        if selected and name.casefold() not in selected:
            continue
        if limit is not None and processed >= limit:
            break
        output = target_dir / safe_actor_filename(name)
        if committer.exists(output) and not overwrite:
            statuses[name] = {
                "status": "exists", "file": str(output),
                "bytes": committer.stat(output).st_size, "updated": now_iso(),
            }
            counts["exists"] += 1
            changed += 1
            continue
        if statuses.get(name, {}).get("status") == "failed" and not retry_failed:
            continue
        if not urls[name]:
            continue
        try:
            downloaded = fetch(urls[name][0])
            source, content_type = (
                downloaded if isinstance(downloaded, tuple) else (downloaded, "")
            )
            if not source:
                raise RuntimeError("downloaded zero bytes")
            data = normalize_actor_image(source, normalize)
            committer.write(output, data)
            statuses[name] = {
                "status": "ok" if committer.committing else "planned",
                "file": str(output), "bytes": len(data),
                "source_bytes": len(source), "url": urls[name][0],
                "content_type": content_type, "updated": now_iso(),
            }
            outcome = "ok" if committer.committing else "planned"
            counts[outcome] = counts.get(outcome, 0) + 1
        except Exception as error:
            statuses[name] = {
                "status": "failed", "error": repr(error),
                "url": urls[name][0], "updated": now_iso(),
            }
            counts["failed"] += 1
        processed += 1
        changed += 1
        if changed >= save_every:
            if committer.committing:
                save_json_atomic(status_path, statuses)
            changed = 0
        emit(reporter, "progress" if committer.committing else "prepared", name,
             status=statuses[name]["status"])
        if sleep_between:
            sleep(sleep_between)
    if committer.committing:
        save_json_atomic(status_path, statuses)
    if not committer.committing:
        return {"processed": processed, "total": len(urls),
                "planned_counts": counts, "planned": planned(committer)}
    return {"processed": processed, "total": len(urls), "counts": counts}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
