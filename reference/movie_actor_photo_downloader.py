#!/usr/bin/env python3

import os
import re
import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone


URL_DB = "actor_thumb_urls_tmdb.json"
STATUS_DB = "actor_photo_download_status.json"

OUT_DIR = "/mnt/2tb/Movie/.actors"

TIMEOUT = 30
SLEEP_BETWEEN = 0.0
SAVE_EVERY = 25

# TMDB w185 images are already fairly small, but this keeps the pile sane.
USE_PIL_RESIZE = True
MAX_SIZE = (185, 278)
JPEG_QUALITY = 75


def fmt_kb(n):
    return f"{n / 1024:,.1f} KB"


def fmt_mb(n):
    return f"{n / 1024 / 1024:,.2f} MB"


def fmt_time(seconds):
    seconds = int(seconds)

    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60

    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def json_save_atomic(path, data):
    tmp = str(path) + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)

    os.replace(tmp, path)


def safe_filename(name):
    """
    Keep it simple and human-readable.

    Aaron Pearl -> Aaron_Pearl.jpg
    """
    name = name.strip()
    name = re.sub(r"[\/\\:]+", "_", name)
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"[^A-Za-z0-9._'()&+-]+", "_", name)
    name = name.strip("._ ")

    if not name:
        name = "unknown_actor"

    return name + ".jpg"


def download_bytes(url):
    headers = {
        "User-Agent": "local-tmdb-actor-photo-gulper/1.0",
        "Accept": "image/jpeg,image/*,*/*",
    }

    req = urllib.request.Request(url, headers=headers)

    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        content_type = resp.headers.get("Content-Type", "")
        data = resp.read()

    return data, content_type


def maybe_resize_image(data):
    if not USE_PIL_RESIZE:
        return data

    try:
        from PIL import Image
        from io import BytesIO
    except Exception:
        return data

    try:
        src = BytesIO(data)

        with Image.open(src) as img:
            img = img.convert("RGB")
            img.thumbnail(MAX_SIZE, Image.LANCZOS)

            out = BytesIO()
            img.save(
                out,
                format="JPEG",
                quality=JPEG_QUALITY,
                optimize=True,
                progressive=False,
            )

            return out.getvalue()

    except Exception:
        # If PIL hates the file, just keep the original downloaded bytes.
        return data


def write_file_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = str(path) + ".tmp"

    with open(tmp, "wb") as f:
        f.write(data)

    os.replace(tmp, path)


def status_counts(status):
    counts = {}

    for item in status.values():
        s = item.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1

    return dict(sorted(counts.items()))


def gulp_actor_photos(
    url_db=URL_DB,
    status_db=STATUS_DB,
    out_dir=OUT_DIR,
    retry_failed=False,
    overwrite_existing=False,
    limit=None,
):
    actor_urls = json_load(url_db, {})
    status = json_load(status_db, {})

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    actors = sorted(actor_urls.keys())

    processed = 0
    ok_count = 0
    fail_count = 0
    exists_count = 0
    changed = 0

    downloaded_bytes = 0
    source_bytes = 0

    start_time = time.time()

    print("Actors in URL DB:", len(actors))
    print("Status before:", status_counts(status))
    print("Output dir:", out_dir)
    print()

    for actor in actors:
        if limit is not None and processed >= limit:
            break

        urls = actor_urls.get(actor) or []

        if not urls:
            continue

        filename = safe_filename(actor)
        out_path = out_dir / filename

        old = status.get(actor, {})
        old_status = old.get("status")

        if out_path.exists() and not overwrite_existing:
            exists_count += 1

            status[actor] = {
                "status": "exists",
                "updated": now_iso(),
                "file": str(out_path),
                "bytes": out_path.stat().st_size,
            }

            changed += 1
            continue

            changed += 1

            # Usually don't print every existing file on resume.
            continue

        if old_status == "failed" and not retry_failed:
            continue

        url = urls[0]
        processed += 1

        try:
            data, content_type = download_bytes(url)

            if not data:
                raise RuntimeError("downloaded zero bytes")

            final_data = maybe_resize_image(data)

            write_file_atomic(out_path, final_data)

            src_size = len(data)
            final_size = len(final_data)

            source_bytes += src_size
            downloaded_bytes += final_size
            ok_count += 1

            status[actor] = {
                "status": "ok",
                "updated": now_iso(),
                "url": url,
                "file": str(out_path),
                "source_bytes": src_size,
                "bytes": final_size,
                "content_type": content_type,
            }

            elapsed = time.time() - start_time
            rate = ok_count / elapsed if elapsed > 0 else 0

            print(
                f"[{processed:05d}] PASS  "
                f"{actor}  "
                f"{fmt_kb(final_size)}  "
                f"run={fmt_mb(downloaded_bytes)}  "
                f"ok={ok_count} fail={fail_count}  "
                f"{rate:.2f}/sec"
            )

        except urllib.error.HTTPError as e:
            fail_count += 1

            status[actor] = {
                "status": "failed",
                "updated": now_iso(),
                "url": url,
                "error": f"HTTPError {e.code}",
            }

            print(
                f"[{processed:05d}] FAIL  "
                f"{actor}  "
                f"HTTP {e.code}  "
                f"run={fmt_mb(downloaded_bytes)}  "
                f"ok={ok_count} fail={fail_count}"
            )

        except Exception as e:
            fail_count += 1

            status[actor] = {
                "status": "failed",
                "updated": now_iso(),
                "url": url,
                "error": repr(e),
            }

            print(
                f"[{processed:05d}] FAIL  "
                f"{actor}  "
                f"{repr(e)}  "
                f"run={fmt_mb(downloaded_bytes)}  "
                f"ok={ok_count} fail={fail_count}"
            )

        changed += 1

        if changed >= SAVE_EVERY:
            json_save_atomic(status_db, status)

            elapsed = time.time() - start_time
            total_done = ok_count + fail_count
            rate = total_done / elapsed if elapsed > 0 else 0

            print(
                f"--- checkpoint: "
                f"ok={ok_count} fail={fail_count} exists={exists_count} "
                f"written={fmt_mb(downloaded_bytes)} "
                f"raw={fmt_mb(source_bytes)} "
                f"elapsed={fmt_time(elapsed)} "
                f"rate={rate:.2f}/sec ---"
            )

            changed = 0

        if SLEEP_BETWEEN:
            time.sleep(SLEEP_BETWEEN)

    json_save_atomic(status_db, status)

    elapsed = time.time() - start_time

    print()
    print("MUGSHOT GULP COMPLETE")
    print("---------------------")
    print("Status after:", status_counts(status))
    print("Processed this run:", processed)
    print("Succeeded this run:", ok_count)
    print("Failed this run:", fail_count)
    print("Already existed:", exists_count)
    print("Written this run:", fmt_mb(downloaded_bytes))
    print("Raw downloaded:", fmt_mb(source_bytes))
    print("Elapsed:", fmt_time(elapsed))

    if elapsed > 0:
        print("Average:", f"{processed / elapsed:.2f} actors/sec")


if __name__ == "__main__":
    gulp_actor_photos(
        retry_failed=True,

        # test nibble:
        # limit=100,
    )
