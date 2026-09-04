#!/usr/bin/env python3
"""
Stage 2: consume the durable work file made by scan_tv_tvdb_urls.py and
materialize a TV library without ever calling the TVDB API.

For every matched title record it may write:

  TV Shows/<Show>/show.nfo
  TV Shows/<Show>/poster.jpg or poster.png
  TV Shows/.actors/<Actor_Name>.jpg

The work file is updated atomically with what was written, already existed,
was unavailable, or failed.  The filesystem remains the primary receipt:
existing NFO/poster/mugshot files are skipped on later runs.

The NFO renderer deliberately writes rich show metadata, but never writes:
  - web URLs
  - remote thumbnail fields
  - local paths or filenames
  - actor image links

This script only downloads image URLs already frozen in the Stage 1 work file.
It does not resolve titles, search TVDB, or ask any metadata API anything.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

# Keep this identical to scan_tv_tvdb_urls.py.
TV_SHOWS_DIR = "/mnt/2tb/TV"

# The Stage 1 work file lives beside this script, not inside the library.
WORK_FILE = "tv_show_urls_tvdb.json"

# Normal restart behavior.
OVERWRITE_NFO = True
OVERWRITE_POSTER = True
RETRY_FAILED = True

# Image handling.  TVDB w185 headshots are already small, but converting them
# to a known JPEG keeps .actors pleasantly predictable when Pillow is around.
USE_PIL_FOR_ACTORS = True
ACTOR_MAX_SIZE = (185, 278)
ACTOR_JPEG_QUALITY = 75

# Network behavior for the direct image requests made by this script.
TIMEOUT = 30
REQUEST_ATTEMPTS = 4
SLEEP_BETWEEN_REQUESTS = 0.5
SAVE_EVERY_CHANGES = 25

# Set to e.g. 5 for a first live nibble, then put it back to None.
LIMIT = None
VERBOSE = True

USER_AGENT = "local-tv-tvdb-materializer/1.0"


# ---------------------------------------------------------------------
# Small durable-storage helpers
# ---------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def script_dir():
    try:
        return Path(__file__).resolve().parent
    except NameError:
        return Path.cwd()


def json_load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: could not read {path}: {e!r}", file=sys.stderr)
        return default


def json_save_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")

    os.replace(tmp, path)


def write_bytes_atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")

    with open(tmp, "wb") as f:
        f.write(data)

    os.replace(tmp, path)


def write_text_atomic(path, text):
    write_bytes_atomic(path, text.encode("utf-8"))


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


def safe_filename(name):
    """Pleasant actor cache names: Adam Scott -> Adam_Scott.jpg."""
    name = (name or "").strip()
    name = re.sub(r"[\/\\:]+", "_", name)
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"[^A-Za-z0-9._'()&+-]+", "_", name)
    name = name.strip("._ ")

    if not name:
        name = "unknown_actor"

    return name + ".jpg"


def record_status_counts(manifest):
    counts = Counter()

    for record in manifest.get("shows", {}).values():
        counts[record.get("status", "unknown")] += 1

    return dict(sorted(counts.items()))


# ---------------------------------------------------------------------
# Link-free rich TV NFO writer
# ---------------------------------------------------------------------

def text(value):
    """Return a clean XML text value or None for absent values."""
    if value is None:
        return None

    if isinstance(value, bool):
        return "true" if value else "false"

    value = str(value).strip()
    return value or None


def append_text(parent, tag, value):
    value = text(value)

    if value is None:
        return None

    node = ET.SubElement(parent, tag)
    node.text = value
    return node


def append_many(parent, tag, values):
    for value in values or []:
        append_text(parent, tag, value)


def render_show_nfo(nfo):
    """
    Render the Stage 1 NFO payload to the established movie-ish XML shape.

    The payload deliberately contains no asset URLs or local paths.  This
    function does not even receive the sibling assets object, which makes it
    hard to accidentally leak an image URL into show.nfo later.
    """
    if not isinstance(nfo, dict):
        raise ValueError("record has no usable NFO payload")

    root = ET.Element("tvshow")

    # Core old-movie-shaped scalar vocabulary.
    for tag in (
        "title",
        "originaltitle",
        "sorttitle",
        "rating",
        "year",
        "votes",
        "outline",
        "plot",
        "tagline",
        "runtime",
        "status",
        "type",
        "inproduction",
        "mpaa",
        "certification",
        "id",
        "tmdbId",
        "tvdbId",
        "premiered",
        "lastaired",
        "numberofseasons",
        "numberofepisodes",
        "abbreviation",
        "originalcountry",
        "originallanguage",
        "nextaired",
        "airstime",
        "defaultseasontype",
        "isorderrandomized",
    ):
        append_text(root, tag, nfo.get(tag))

    # Redundant legacy-style IDs are intentional: the movie library already
    # uses id + ids + tmdbId, and a shared UI can consume whichever it knows.
    ids = nfo.get("ids") or {}
    if isinstance(ids, dict) and ids:
        ids_node = ET.SubElement(root, "ids")

        for key, value in ids.items():
            value = text(value)
            if not value:
                continue

            entry = ET.SubElement(ids_node, "entry")
            append_text(entry, "key", key)
            append_text(entry, "value", value)

    # Repeated simple metadata.  Nothing in these fields is an asset pointer.
    append_many(root, "alternativetitle", nfo.get("alternativetitles"))
    append_many(root, "country", nfo.get("country"))
    append_many(root, "genre", nfo.get("genre"))
    append_many(root, "keyword", nfo.get("keyword"))
    append_many(root, "studio", nfo.get("studio"))
    append_many(root, "network", nfo.get("network"))
    append_many(root, "creator", nfo.get("creator"))
    append_many(root, "credits", nfo.get("credits"))
    append_many(root, "language", nfo.get("language"))
    append_many(root, "airsday", nfo.get("airsday"))

    languages = [text(value) for value in (nfo.get("language") or [])]
    languages = [value for value in languages if value]
    if languages:
        append_text(root, "languages", ", ".join(languages))

    # Keep the useful show-level season inventory even though no episode NFOs
    # are created.  A shared UI can ignore it now and use it later.
    seasons = nfo.get("seasons") or []
    if seasons:
        seasons_node = ET.SubElement(root, "seasons")

        for season in seasons:
            if not isinstance(season, dict):
                continue

            season_node = ET.SubElement(seasons_node, "season")
            append_text(season_node, "seasonnumber", season.get("season_number"))
            append_text(season_node, "name", season.get("name"))
            append_text(season_node, "premiered", season.get("air_date"))
            append_text(season_node, "episodecount", season.get("episode_count"))
            append_text(season_node, "seasontype", season.get("season_type"))
            append_text(season_node, "year", season.get("year"))

    # Actor entries deliberately contain only identity/role/order.  No remote
    # thumb and no ../.actors path: local cache resolution is entirely external.
    for actor in nfo.get("actor") or []:
        if not isinstance(actor, dict):
            continue

        name = text(actor.get("name"))
        if not name:
            continue

        actor_node = ET.SubElement(root, "actor")
        append_text(actor_node, "name", name)
        append_text(actor_node, "role", actor.get("role"))
        append_text(actor_node, "order", actor.get("order"))

    # Python 3.9+ gives us readable indentation without a home-made pretty
    # printer.  The tool targets a modern local Python; fall back gracefully.
    try:
        ET.indent(root, space="    ")
    except AttributeError:
        pass

    xml_body = ET.tostring(root, encoding="utf-8", xml_declaration=False)
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + xml_body + b"\n"


# ---------------------------------------------------------------------
# Image downloading and small actor-cache treatment
# ---------------------------------------------------------------------

def download_bytes(url):
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError(f"not an HTTP image URL: {url!r}")

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "image/jpeg,image/png,image/webp,image/*,*/*",
    }
    request = urllib.request.Request(url, headers=headers)

    last_error = None

    for attempt in range(REQUEST_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                content_type = response.headers.get("Content-Type", "")
                data = response.read()

            if not data:
                raise RuntimeError("downloaded zero bytes")

            return data, content_type

        except urllib.error.HTTPError as e:
            # A missing remote image will remain missing; retrying a 404 is
            # just noise.  Rate limits and server hiccups earn a few tries.
            if e.code == 404:
                raise RuntimeError("HTTP 404") from e

            retryable = e.code == 429 or 500 <= e.code <= 599
            last_error = e

            if not retryable or attempt == REQUEST_ATTEMPTS - 1:
                break

            retry_after = e.headers.get("Retry-After")
            delay = (
                float(retry_after)
                if retry_after and retry_after.replace(".", "", 1).isdigit()
                else min(2 ** attempt, 20)
            )

            if VERBOSE:
                print(f"             image HTTP {e.code}; retrying in {delay:.1f}s")

            time.sleep(delay)

        except (urllib.error.URLError, TimeoutError) as e:
            last_error = e

            if attempt == REQUEST_ATTEMPTS - 1:
                break

            delay = min(2 ** attempt, 20)

            if VERBOSE:
                print(f"             image network hiccup; retrying in {delay}s")

            time.sleep(delay)

    raise RuntimeError(f"image request failed after retries: {last_error!r}")


def image_extension(data, content_type):
    """Keep posters as actual JPG or PNG files whenever possible."""
    content_type = (content_type or "").lower().split(";", 1)[0].strip()

    if data.startswith(b"\x89PNG\r\n\x1a\n") or content_type == "image/png":
        return ".png"

    # TVDB's ordinary poster/profile rendition endpoints are JPEG.  Defaulting
    # to .jpg is the useful pragmatic choice when a server omits its header.
    return ".jpg"


def maybe_make_actor_jpeg(data):
    """
    Best effort only.  If Pillow is unavailable or dislikes an image, preserve
    the downloaded bytes; .actors is a low-stakes face cache, not a lab.
    """
    if not USE_PIL_FOR_ACTORS:
        return data

    try:
        from PIL import Image
    except Exception:
        return data

    try:
        with Image.open(BytesIO(data)) as image:
            image = image.convert("RGB")
            image.thumbnail(ACTOR_MAX_SIZE, Image.LANCZOS)

            out = BytesIO()
            image.save(
                out,
                format="JPEG",
                quality=ACTOR_JPEG_QUALITY,
                optimize=True,
                progressive=False,
            )
            return out.getvalue()

    except Exception:
        return data


def poster_paths(show_dir):
    return show_dir / "poster.jpg", show_dir / "poster.png"


def actor_cache_path(actors_dir, name):
    return actors_dir / safe_filename(name)


# ---------------------------------------------------------------------
# Work-file status bookkeeping
# ---------------------------------------------------------------------

def artifact_state(record):
    state = record.setdefault("materialize", {})
    state.setdefault("nfo", {})
    state.setdefault("poster", {})
    state.setdefault("actors", {})
    return state


def mark_item(item, status, **extra):
    item.clear()
    item.update({
        "status": status,
        "updated": now_iso(),
        **extra,
    })


def record_overall_materialize_status(record):
    """Summarize Stage 2 without pretending missing headshots are fatal."""
    state = artifact_state(record)
    nfo_status = state.get("nfo", {}).get("status")
    poster_status = state.get("poster", {}).get("status")

    actors = record.get("assets", {}).get("actor_urls") or []
    actor_counts = Counter(
        (item.get("download") or {}).get("status", "pending")
        for item in actors
    )

    if nfo_status in ("ok", "exists") and poster_status in ("ok", "exists", "no_url"):
        # A show can be done even when some cast members simply have no TVDB
        # profile photo.  Actual request errors leave it partial/retryable.
        actor_errors = actor_counts.get("error", 0)
        state["status"] = "partial" if actor_errors else "complete"
    elif nfo_status == "error" or poster_status == "error":
        state["status"] = "error"
    else:
        state["status"] = "partial"

    state["updated"] = now_iso()
    state["actor_counts"] = dict(sorted(actor_counts.items()))


def may_retry(item):
    status = item.get("status")
    return status not in ("ok", "exists", "no_url") and (status != "error" or RETRY_FAILED)


# ---------------------------------------------------------------------
# One show materializer
# ---------------------------------------------------------------------

def materialize_show(show_path, record, actors_dir, counters):
    show_dir = Path(show_path)
    folder_name = record.get("folder_name") or show_dir.name
    state = artifact_state(record)
    nfo_payload = record.get("nfo")
    assets = record.get("assets") or {}

    if not show_dir.is_dir():
        mark_item(state["nfo"], "error", error="show directory no longer exists")
        mark_item(state["poster"], "error", error="show directory no longer exists")
        record_overall_materialize_status(record)
        print(f"             GONE   {folder_name}")
        counters["show_missing"] += 1
        return 2

    changes = 0
    print(f"[{counters['shows_seen']:04d}] SHOW  {folder_name}")

    # NFO ---------------------------------------------------------------
    nfo_path = show_dir / "show.nfo"
    nfo_state = state["nfo"]

    try:
        if nfo_path.exists() and not OVERWRITE_NFO:
            mark_item(nfo_state, "exists", bytes=nfo_path.stat().st_size)
            counters["nfo_exists"] += 1
            print(f"             NFO EXISTS  {fmt_kb(nfo_path.stat().st_size)}")
        elif not nfo_payload:
            mark_item(nfo_state, "error", error="matched record has no NFO payload")
            counters["nfo_error"] += 1
            print("             NFO ERROR   no staged NFO payload")
        else:
            xml = render_show_nfo(nfo_payload)
            write_bytes_atomic(nfo_path, xml)
            mark_item(nfo_state, "ok", bytes=len(xml))
            counters["nfo_ok"] += 1
            print(f"             NFO PASS    {fmt_kb(len(xml))}")
        changes += 1

    except Exception as e:
        mark_item(nfo_state, "error", error=repr(e))
        counters["nfo_error"] += 1
        changes += 1
        print(f"             NFO ERROR   {e!r}")

    # Poster ------------------------------------------------------------
    poster_jpg, poster_png = poster_paths(show_dir)
    poster_state = state["poster"]
    poster_url = assets.get("poster_url")

    try:
        existing_poster = next(
            (path for path in (poster_jpg, poster_png) if path.exists()),
            None,
        )

        if existing_poster and not OVERWRITE_POSTER:
            mark_item(
                poster_state,
                "exists",
                bytes=existing_poster.stat().st_size,
                file=existing_poster.name,
            )
            counters["poster_exists"] += 1
            print(f"             POSTER EXISTS  {existing_poster.name}  {fmt_kb(existing_poster.stat().st_size)}")

        elif not poster_url:
            mark_item(poster_state, "no_url", error="TVDB record has no poster URL")
            counters["poster_no_url"] += 1
            print("             POSTER NONE    TVDB has no poster")

        elif may_retry(poster_state):
            data, content_type = download_bytes(poster_url)
            suffix = image_extension(data, content_type)
            target = show_dir / ("poster" + suffix)

            # Do not leave a stale alternate image around after an explicit
            # overwrite changed its format.
            other = poster_png if target == poster_jpg else poster_jpg
            write_bytes_atomic(target, data)
            if other.exists() and other != target:
                try:
                    other.unlink()
                except OSError:
                    pass

            mark_item(
                poster_state,
                "ok",
                bytes=len(data),
                file=target.name,
                content_type=content_type,
            )
            counters["poster_ok"] += 1
            counters["bytes"] += len(data)
            print(f"             POSTER PASS    {target.name}  {fmt_kb(len(data))}")
            changes += 1

        else:
            counters["poster_skipped"] += 1
            print(f"             POSTER SKIP    prior status={poster_state.get('status')}")

    except Exception as e:
        mark_item(poster_state, "error", error=repr(e), url=poster_url)
        counters["poster_error"] += 1
        changes += 1
        print(f"             POSTER ERROR   {e!r}")

    # Actor mugshots ---------------------------------------------------
    actors = assets.get("actor_urls") or []

    if actors:
        print(f"             FACES          {len(actors)} listed")

    for actor in actors:
        if not isinstance(actor, dict):
            continue

        name = (actor.get("name") or "").strip()
        url = actor.get("url")

        if not name:
            continue

        actor_state = actor.setdefault("download", {})
        out_path = actor_cache_path(actors_dir, name)

        try:
            if out_path.exists():
                mark_item(actor_state, "exists", bytes=out_path.stat().st_size)
                counters["actor_exists"] += 1
                continue

            if not url:
                mark_item(actor_state, "no_url", error="TVDB cast record had no profile URL")
                counters["actor_no_url"] += 1
                continue

            if not may_retry(actor_state):
                counters["actor_skipped"] += 1
                continue

            data, content_type = download_bytes(url)
            final_data = maybe_make_actor_jpeg(data)
            write_bytes_atomic(out_path, final_data)

            mark_item(
                actor_state,
                "ok",
                bytes=len(final_data),
                source_bytes=len(data),
                content_type=content_type,
            )
            counters["actor_ok"] += 1
            counters["bytes"] += len(final_data)
            changes += 1

            print(f"             FACE PASS      {name}  {fmt_kb(len(final_data))}")

            if SLEEP_BETWEEN_REQUESTS:
                time.sleep(SLEEP_BETWEEN_REQUESTS)

        except KeyboardInterrupt:
            raise

        except Exception as e:
            mark_item(actor_state, "error", error=repr(e), url=url)
            counters["actor_error"] += 1
            changes += 1
            print(f"             FACE ERROR     {name}  {e!r}")

    record_overall_materialize_status(record)
    return changes


# ---------------------------------------------------------------------
# Main work loop
# ---------------------------------------------------------------------

def materialize_tv_library():
    here = script_dir()
    work_path = here / WORK_FILE
    library_root = Path(TV_SHOWS_DIR).expanduser().resolve()
    actors_dir = library_root / ".actors"

    manifest = json_load(work_path, None)

    if not isinstance(manifest, dict) or not isinstance(manifest.get("shows"), dict):
        raise RuntimeError(
            f"No usable Stage 1 work file at {work_path}. "
            "Run scan_tv_tvdb_urls.py first."
        )

    expected_root = (manifest.get("_meta") or {}).get("library_root")
    if expected_root and Path(expected_root).expanduser().resolve() != library_root:
        print("WARNING: manifest library_root differs from this script's TV_SHOWS_DIR")
        print("         manifest:", expected_root)
        print("         config:  ", library_root)
        print("         Continuing because title-folder paths in the manifest are explicit.")
        print()

    if not library_root.is_dir():
        raise FileNotFoundError(f"TV_SHOWS_DIR is not a readable directory: {library_root}")

    actors_dir.mkdir(parents=True, exist_ok=True)

    shows = manifest["shows"]
    matched_items = [
        (path, record)
        for path, record in shows.items()
        if record.get("status") == "matched" and record.get("nfo")
    ]
    matched_items.sort(key=lambda item: (item[1].get("folder_name") or item[0]).casefold())

    counters = Counter()
    counters["bytes"] = 0

    print("TV TVDB MATERIALIZER")
    print("--------------------")
    print("Library root:", library_root)
    print("Actor cache:", actors_dir)
    print("Work file:", work_path)
    print("Stage 1 status:", record_status_counts(manifest))
    print("Matched titles available:", len(matched_items))
    print("NFO overwrite:", OVERWRITE_NFO)
    print("Poster overwrite:", OVERWRITE_POSTER)
    print("Retry prior image errors:", RETRY_FAILED)
    print()

    processed = 0
    changed_since_save = 0
    start = time.time()

    try:
        for show_path, record in matched_items:
            if LIMIT is not None and processed >= LIMIT:
                break

            processed += 1
            counters["shows_seen"] = processed

            changes = materialize_show(show_path, record, actors_dir, counters)
            changed_since_save += changes

            manifest.setdefault("_meta", {})["updated"] = now_iso()

            if changed_since_save >= SAVE_EVERY_CHANGES:
                json_save_atomic(work_path, manifest)
                changed_since_save = 0
                elapsed = time.time() - start
                print(
                    "             --- checkpoint "
                    f"shows={processed} "
                    f"nfo={counters['nfo_ok']}+{counters['nfo_exists']} "
                    f"poster={counters['poster_ok']}+{counters['poster_exists']} "
                    f"faces={counters['actor_ok']}+{counters['actor_exists']} "
                    f"written={fmt_mb(counters['bytes'])} "
                    f"elapsed={fmt_time(elapsed)} ---"
                )

    except KeyboardInterrupt:
        print("\nCTRL-C: preserving the manifest; existing local files will be skipped on resume...")

    finally:
        elapsed = time.time() - start
        manifest.setdefault("_meta", {})["updated"] = now_iso()
        manifest["_meta"]["last_materialize_run"] = {
            "finished": now_iso(),
            "processed_shows": processed,
            "nfo_ok": counters["nfo_ok"],
            "nfo_exists": counters["nfo_exists"],
            "nfo_error": counters["nfo_error"],
            "poster_ok": counters["poster_ok"],
            "poster_exists": counters["poster_exists"],
            "poster_no_url": counters["poster_no_url"],
            "poster_error": counters["poster_error"],
            "actor_ok": counters["actor_ok"],
            "actor_exists": counters["actor_exists"],
            "actor_no_url": counters["actor_no_url"],
            "actor_error": counters["actor_error"],
            "bytes_written": counters["bytes"],
        }
        json_save_atomic(work_path, manifest)

    print()
    print("MATERIALIZE STOPPED" if LIMIT is None and processed < len(matched_items) else "MATERIALIZE COMPLETE")
    print("--------------------")
    print("Shows handled:", processed)
    print("NFO:    pass=%d exists=%d error=%d" % (
        counters["nfo_ok"], counters["nfo_exists"], counters["nfo_error"],
    ))
    print("Poster: pass=%d exists=%d none=%d error=%d" % (
        counters["poster_ok"], counters["poster_exists"],
        counters["poster_no_url"], counters["poster_error"],
    ))
    print("Faces:  pass=%d exists=%d none=%d error=%d" % (
        counters["actor_ok"], counters["actor_exists"],
        counters["actor_no_url"], counters["actor_error"],
    ))
    print("Written this run:", fmt_mb(counters["bytes"]))
    print("Elapsed:", fmt_time(elapsed))

    if elapsed > 0 and processed:
        print("Rate:", f"{processed / elapsed:.2f} title folders/sec")

    return manifest


if __name__ == "__main__":
    materialize_tv_library()
