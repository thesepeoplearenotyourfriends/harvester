"""TV Stage 2: materialize only the metadata and URLs frozen by Stage 1."""
from collections import Counter
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import time
import xml.etree.ElementTree as ET

from ..downloads import download_image
from ..events import emit
from ..images import normalize_actor_image, safe_actor_filename
from ..artifacts import planned, use_committer
from ..storage import load_json, save_json_atomic

USER_AGENT = "local-tv-tvdb-materializer/1.0"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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



@dataclass(frozen=True)
class MaterializeOptions:
    overwrite_nfo: bool = True
    overwrite_poster: bool = True
    retry_failed: bool = True
    normalize_actors: bool = True
    request_attempts: int = 4
    request_timeout: int = 30
    sleep_between_requests: float = 0.5


def download_bytes(url, options=None, reporter=None, sleep=None, transport=None):
    """Download one image with bounded retries for transient failures."""
    options = options or MaterializeOptions()
    return download_image(
        url, user_agent=USER_AGENT,
        request_attempts=options.request_attempts,
        request_timeout=options.request_timeout,
        reporter=reporter, sleep=sleep, transport=transport,
    )


def image_extension(data, content_type):
    """Use the bytes/header rather than trusting a remote URL suffix."""
    media_type = (content_type or "").lower().split(";", 1)[0].strip()
    if data.startswith(b"\x89PNG\r\n\x1a\n") or media_type == "image/png":
        return ".png"
    return ".jpg"


def poster_paths(show_dir):
    return show_dir / "poster.jpg", show_dir / "poster.png"


def artifact_state(record):
    state = record.setdefault("materialize", {})
    state.setdefault("nfo", {})
    state.setdefault("poster", {})
    state.setdefault("actors", {})
    return state


def mark_item(item, status, **details):
    item.clear()
    item.update({"status": status, "updated": now_iso(), **details})


def may_retry(item, retry_failed):
    status = item.get("status")
    return status not in ("ok", "exists", "no_url") and (
        status != "error" or retry_failed
    )


def update_overall_status(record):
    state = artifact_state(record)
    nfo_status = state["nfo"].get("status")
    poster_status = state["poster"].get("status")
    actor_counts = Counter(
        (actor.get("download") or {}).get("status", "pending")
        for actor in (record.get("assets") or {}).get("actor_urls", [])
    )
    if nfo_status in ("ok", "exists") and poster_status in (
        "ok", "exists", "no_url"
    ):
        state["status"] = "partial" if actor_counts.get("error") else "complete"
    elif nfo_status == "error" or poster_status == "error":
        state["status"] = "error"
    else:
        state["status"] = "partial"
    state["updated"] = now_iso()
    state["actor_counts"] = dict(sorted(actor_counts.items()))


def materialize_show(
    show_path,
    record,
    actors_dir,
    counters,
    options,
    reporter=None,
    downloader=None,
    sleep=None,
    transport=None,
    write_nfo=True,
    write_poster=True,
    write_actors=True,
    committer=None,
):
    """Materialize one record without provider access or terminal output."""
    committer = use_committer(committer)
    show_dir = Path(show_path)
    folder_name = record.get("folder_name") or show_dir.name
    state = record.setdefault("materialize", {})
    if write_nfo:
        state.setdefault("nfo", {})
    if write_poster:
        state.setdefault("poster", {})
    assets = record.get("assets") or {}
    if not show_dir.is_dir():
        if write_nfo:
            mark_item(state["nfo"], "error", error="show directory no longer exists")
        if write_poster:
            mark_item(state["poster"], "error", error="show directory no longer exists")
        if write_nfo and write_poster and write_actors:
            update_overall_status(record)
        counters["show_missing"] += 1
        emit(reporter, "error", "show directory no longer exists", show=folder_name)
        return 2

    changes = 0

    def fetch(url):
        if downloader:
            return downloader(url)
        return download_bytes(url, options, reporter, sleep, transport)

    if write_nfo:
        nfo_path = show_dir / "show.nfo"
        try:
            if committer.exists(nfo_path) and not options.overwrite_nfo:
                mark_item(state["nfo"], "exists", bytes=committer.stat(nfo_path).st_size)
                counters["nfo_exists"] += 1
            elif not record.get("nfo"):
                mark_item(state["nfo"], "error", error="matched record has no NFO payload")
                counters["nfo_error"] += 1
            else:
                xml = render_show_nfo(record["nfo"])
                committer.write(nfo_path, xml)
                mark_item(state["nfo"], "ok" if committer.committing else "planned", bytes=len(xml))
                counters["nfo_ok"] += 1
            changes += 1
        except Exception as error:
            mark_item(state["nfo"], "error", error=repr(error))
            counters["nfo_error"] += 1
            changes += 1
        emit(reporter, "artifact" if committer.committing else "prepared", "NFO handled", show=folder_name,
             status=state["nfo"].get("status"))

    if write_poster:
        poster_jpg, poster_png = poster_paths(show_dir)
        poster_state = state["poster"]
        poster_url = assets.get("poster_url")
        try:
            existing = next((path for path in (poster_jpg, poster_png)
                             if committer.exists(path)), None)
            if existing and not options.overwrite_poster:
                mark_item(poster_state, "exists", bytes=committer.stat(existing).st_size,
                          file=existing.name)
                counters["poster_exists"] += 1
            elif not poster_url:
                mark_item(poster_state, "no_url", error="TVDB record has no poster URL")
                counters["poster_no_url"] += 1
            elif may_retry(poster_state, options.retry_failed):
                data, content_type = fetch(poster_url)
                target = show_dir / ("poster" + image_extension(data, content_type))
                alternate = poster_png if target == poster_jpg else poster_jpg
                committer.write(target, data)
                if committer.exists(alternate):
                    try:
                        committer.unlink(alternate)
                    except OSError:
                        pass
                mark_item(poster_state, "ok" if committer.committing else "planned",
                          bytes=len(data), file=target.name,
                          content_type=content_type)
                counters["poster_ok"] += 1
                counters["bytes"] += len(data)
                changes += 1
            else:
                counters["poster_skipped"] += 1
        except Exception as error:
            mark_item(poster_state, "error", error=repr(error), url=poster_url)
            counters["poster_error"] += 1
            changes += 1
        emit(reporter, "artifact" if committer.committing else "prepared", "poster handled", show=folder_name,
             status=poster_state.get("status"))

    for actor in (assets.get("actor_urls") or []) if write_actors else []:
        if not isinstance(actor, dict):
            continue
        name = (actor.get("name") or "").strip()
        if not name:
            continue
        actor_state = actor.setdefault("download", {})
        try:
            # Path creation belongs inside this boundary: malformed actor data
            # must not abort all remaining shows and actors.
            output = actors_dir / safe_actor_filename(name)
            if committer.exists(output):
                mark_item(actor_state, "exists", bytes=committer.stat(output).st_size)
                counters["actor_exists"] += 1
            elif not actor.get("url"):
                mark_item(actor_state, "no_url",
                          error="TVDB cast record had no profile URL")
                counters["actor_no_url"] += 1
            elif not may_retry(actor_state, options.retry_failed):
                counters["actor_skipped"] += 1
            else:
                source, content_type = fetch(actor["url"])
                data = normalize_actor_image(source, options.normalize_actors)
                committer.write(output, data)
                mark_item(actor_state, "ok" if committer.committing else "planned", bytes=len(data),
                          source_bytes=len(source), content_type=content_type)
                counters["actor_ok"] += 1
                counters["bytes"] += len(data)
                changes += 1
                if options.sleep_between_requests:
                    sleep(options.sleep_between_requests)
        except KeyboardInterrupt:
            raise
        except Exception as error:
            mark_item(actor_state, "error", error=repr(error), url=actor.get("url"))
            counters["actor_error"] += 1
            changes += 1
        emit(reporter, "artifact" if committer.committing else "prepared", "actor image handled", show=folder_name,
             actor=name, status=actor_state.get("status"))
    if write_nfo and write_poster and write_actors:
        update_overall_status(record)
    return changes


def run(
    config,
    reporter=None,
    limit=None,
    overwrite_nfo=True,
    overwrite_poster=True,
    retry_failed=True,
    downloader=None,
    normalize=True,
    request_attempts=4,
    sleep_between_requests=0.5,
    sleep=None,
    transport=None,
    request_timeout=30,
    save_every_changes=25,
    targets=None,
    write_nfo=True,
    write_poster=True,
    write_actors=True,
    committer=None,
):
    """Materialize a frozen manifest and return a compact structured result."""
    committer = use_committer(committer)
    options = MaterializeOptions(
        overwrite_nfo=overwrite_nfo,
        overwrite_poster=overwrite_poster,
        retry_failed=retry_failed,
        normalize_actors=normalize,
        request_attempts=request_attempts,
        request_timeout=request_timeout,
        sleep_between_requests=sleep_between_requests,
    )
    sleep = sleep or time.sleep
    work_path = config.state_path("tv_show_urls_tvdb.json")
    manifest = copy.deepcopy(load_json(work_path, None))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("shows"), dict):
        raise RuntimeError(f"No usable Stage 1 work file at {work_path}; run 'tv scan' first")
    if not config.tv_root.is_dir():
        raise FileNotFoundError(f"TV root is not a readable directory: {config.tv_root}")
    actors_dir = config.tv_root / ".actors"
    if write_actors:
        committer.mkdir(actors_dir)
    matched = [
        (path, record) for path, record in manifest["shows"].items()
        if record.get("status") == "matched" and record.get("nfo")
        and (not targets or path in set(targets) or str(record.get("tvdb_id")) in set(targets))
    ]
    matched.sort(key=lambda pair: (pair[1].get("folder_name") or pair[0]).casefold())
    counters = Counter()
    changed_since_save = 0
    try:
        for show_path, record in matched:
            if limit is not None and counters["shows_seen"] >= limit:
                break
            counters["shows_seen"] += 1
            changes = materialize_show(
                show_path, record, actors_dir, counters, options, reporter, downloader,
                sleep, transport,
                write_nfo, write_poster, write_actors,
                committer,
            )
            changed_since_save += changes
            manifest.setdefault("_meta", {})["updated"] = now_iso()
            if changed_since_save >= save_every_changes:
                if committer.committing:
                    save_json_atomic(work_path, manifest)
                changed_since_save = 0
            emit(reporter, "progress", record.get("folder_name", show_path),
                 status=record.get("materialize", {}).get("status"))
    except KeyboardInterrupt:
        emit(reporter, "interrupted", "TV materialization interrupted; preserving manifest")
    finally:
        manifest.setdefault("_meta", {})["last_materialize_run"] = {
            "finished": now_iso(), "processed_shows": counters["shows_seen"],
            "nfo_ok": counters["nfo_ok"], "nfo_exists": counters["nfo_exists"],
            "nfo_error": counters["nfo_error"], "poster_ok": counters["poster_ok"],
            "poster_exists": counters["poster_exists"],
            "poster_no_url": counters["poster_no_url"],
            "poster_error": counters["poster_error"],
            "actor_ok": counters["actor_ok"], "actor_exists": counters["actor_exists"],
            "actor_no_url": counters["actor_no_url"],
            "actor_error": counters["actor_error"],
            "bytes_written": counters["bytes"],
        }
        if committer.committing:
            save_json_atomic(work_path, manifest)
    result = {
        "processed": counters["shows_seen"],
        "bytes_written": counters["bytes"],
        "nfo_ok": counters["nfo_ok"],
        "poster_ok": counters["poster_ok"],
        "actor_ok": counters["actor_ok"],
    }
    if not committer.committing:
        return {"processed": counters["shows_seen"],
                "planned_counts": {"nfo": counters["nfo_ok"],
                                   "poster": counters["poster_ok"],
                                   "actor": counters["actor_ok"]},
                "planned": planned(committer)}
    return result
