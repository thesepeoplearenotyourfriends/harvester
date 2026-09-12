"""TMDB movie Stage 1: discover local targets and freeze provider data."""
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from ..events import emit
from ..nfo import first_indexable_nfo
from ..storage import load_json, save_json_atomic
from .movie_actor_scan import clean_year, last_year, resolve_movie_tmdb_id


def parse_movie_filename(filename):
    """Extract a conservative provider title and the first plausible movie year."""
    value = re.sub(r"\.[^.]+$", "", str(filename))
    value = re.sub(r"[._-]+", " ", value)
    value = re.sub(r"[\[\]{}]", " ", value)
    value = re.sub(r"\(\s*\)", " ", value)
    value = " ".join(value.split())

    year_match = re.search(r"(?<!\d)(?:18|19|20)\d{2}(?!\d)", value)
    if year_match:
        title = re.sub(r"[\s(\[{]+$", "", value[:year_match.start()])
        title = re.sub(r"^[\s)\]}]+", "", title)
        return " ".join(title.split()), int(year_match.group())

    title = re.sub(r"[()[\]{}]+", " ", value)
    return " ".join(title.split()), None

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(root, *names):
    for name in names:
        value = root.findtext(name)
        if value and value.strip():
            return value.strip()
    return None


def _id(root, kind):
    for node in root.findall("uniqueid"):
        if (node.get("type") or "").casefold() == kind and (node.text or "").strip():
            return node.text.strip()
    return None


def discover_movies(root):
    """Existing NFOs are authoritative; a lone video gives one unambiguous target."""
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"MOVIE_ROOT is not a directory: {root}")
    found = {}
    for base in sorted(path for path in root.iterdir()
                       if path.is_dir() and not path.name.startswith(".")):
        files = [path.name for path in base.iterdir() if path.is_file()]
        nfos = sorted((base / name for name in files if name.lower().endswith(".nfo")),
                      key=lambda path: (path.name.casefold(), path.name))
        videos = sorted(
            base / name for name in files
            if Path(name).suffix.casefold() in {".mkv", ".mp4", ".avi", ".m4v", ".mov"}
        )
        # A lone video supplies an unambiguous sibling NFO target. Multiple
        # videos without an NFO remain untouched because no title owns the path.
        selected_nfo, nfo_diagnostics, _ = first_indexable_nfo(base)
        # Every existing NFO remains an authoritative Harvester identity.  The
        # Movies UI selection is directory-level consumer information only.
        targets = nfos or ([videos[0].with_suffix(".nfo")] if len(videos) == 1 else [])
        for nfo_path in targets:
            title = nfo_path.stem
            original_title = None
            if not nfo_path.exists():
                query_title, filename_year = parse_movie_filename(videos[0].name)
                title = query_title
            else:
                query_title, filename_year = None, None
            year = (filename_year or last_year(base.name) if not nfo_path.exists()
                    else last_year(base.name) or last_year(title))
            imdb_id = tmdb_id = None
            if nfo_path.exists():
                try:
                    node = ET.parse(nfo_path).getroot()
                    original_title = _text(node, "originaltitle")
                    title = _text(node, "title") or original_title or title
                    year = clean_year(_text(node, "year", "premiered", "releasedate")) or year
                    imdb_id = _id(node, "imdb") or _text(node, "imdbid")
                    generic_id = _text(node, "id")
                    if not imdb_id and generic_id and generic_id.startswith("tt"):
                        imdb_id = generic_id
                    tmdb_id = _id(node, "tmdb") or _text(node, "tmdbid")
                except (ET.ParseError, OSError):
                    pass
            if query_title is None:
                query_title = title
            # Existing paths are receipts. A container with one movie has the
            # conventional extensionless ``poster`` target; content decides
            # the eventual suffix during preparation.
            posters = sorted(
                path for path in base.iterdir()
                if path.is_file() and "poster" in path.stem.casefold()
                and path.suffix.casefold() in (".jpg", ".jpeg", ".png")
            )
            poster = (posters[0].resolve() if len(targets) == 1 and len(posters) == 1
                      else (base / "poster").resolve() if len(targets) == 1 and not posters
                      else None)
            found[str(nfo_path.resolve())] = {
                "kind": "movie", "local_target": str(nfo_path.resolve()),
                "nfo_path": str(nfo_path.resolve()),
                "poster_path": str(poster) if poster else None,
                "poster_target_status": "resolved" if poster else "unresolved",
                "title": title, "original_title": original_title,
                "query_title": query_title,
                "year": year, "imdb_id": imdb_id,
                "local_tmdb_id": int(tmdb_id) if str(tmdb_id or "").isdigit() else None,
                "status": "pending", "tries": 0, "tmdb_id": None, "match": None,
                "candidates": [], "nfo": None, "poster_url": None,
                "last_error": None, "materialize": {}, "updated": None,
                "movies_ui_selected_nfo": selected_nfo.get("name") if selected_nfo else None,
                "movies_ui_nfo_usable": bool(selected_nfo),
                "nfo_candidates": nfo_diagnostics,
            }
    return found


def build_nfo(details, credits):
    crew = credits.get("crew") or []
    def names(job):
        return [x.get("name") for x in crew if x.get("job") in job and x.get("name")]
    return {
        "title": details.get("title"), "originaltitle": details.get("original_title"),
        "year": clean_year(details.get("release_date")), "premiered": details.get("release_date"),
        "plot": details.get("overview"), "tagline": details.get("tagline"),
        "runtime": details.get("runtime"),
        "genre": [x.get("name") for x in details.get("genres") or [] if x.get("name")],
        "country": [x.get("name") for x in details.get("production_countries") or [] if x.get("name")],
        "language": [x.get("name") or x.get("english_name") for x in details.get("spoken_languages") or [] if x.get("name") or x.get("english_name")],
        "studio": [x.get("name") for x in details.get("production_companies") or [] if x.get("name")],
        "ids": {"tmdb": details.get("id"), **({"imdb": details.get("imdb_id")} if details.get("imdb_id") else {})},
        "director": names({"Director"}), "credits": names({"Writer", "Screenplay", "Story"}),
        "actor": [{"name": x.get("name"), "role": x.get("character"), "order": x.get("order")} for x in credits.get("cast") or [] if x.get("name")],
    }


def run(config, provider, reporter=None, limit=None, rebuild=False, refresh=False, targets=None, save_every=1):
    path = config.state_path("movie_manifest_tmdb.json")
    manifest = load_json(path, None) if not rebuild else None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("movies"), dict):
        manifest = {"_meta": {"version": 1, "created": now_iso(), "source": "TMDB"}, "movies": {}}
    discovered = discover_movies(config.movie_root)
    discovered_directory_counts = Counter(
        Path(record["nfo_path"]).parent for record in discovered.values())
    for key, record in discovered.items():
        existing = manifest["movies"].get(key)
        if (existing is None and
                discovered_directory_counts[Path(record["nfo_path"]).parent] == 1):
            same_directory = [old_key for old_key, old_record in manifest["movies"].items()
                              if Path(old_record.get("nfo_path") or old_key).parent ==
                              Path(record["nfo_path"]).parent]
            if len(same_directory) == 1:
                existing = manifest["movies"].pop(same_directory[0])
                manifest["movies"][key] = existing
        if existing is None:
            manifest["movies"][key] = record
            continue
        # NFO identity is local source data and may have been corrected by a
        # human since the prior scan. Provider results and receipts remain
        # intact until the selected item is resolved/materialized again.
        for field in (
            "local_target", "nfo_path", "title", "query_title", "original_title", "year",
            "imdb_id", "local_tmdb_id", "poster_path",
            "poster_target_status",
            "movies_ui_selected_nfo", "movies_ui_nfo_usable", "nfo_candidates",
        ):
            existing[field] = record[field]
    selected = set(targets or [])
    processed = 0
    attempt_results = {}
    try:
        config_data = provider.get("/configuration", {})
        images = config_data.get("images") or {}
        base = images.get("secure_base_url") or "https://image.tmdb.org/t/p/"
        sizes = images.get("poster_sizes") or []
        size = "w780" if "w780" in sizes else (sizes[-1] if sizes else "original")
        for key, record in manifest["movies"].items():
            if selected and key not in selected and str(record.get("tmdb_id")) not in selected:
                continue
            if not refresh and record.get("status") == "ok":
                continue
            poster_path = Path(record["poster_path"]) if record.get("poster_path") else None
            if (not refresh and first_indexable_nfo(Path(record["nfo_path"]).parent)[0]
                    and poster_path and poster_path.exists()):
                continue
            if limit is not None and processed >= limit:
                break
            record["tries"] = int(record.get("tries") or 0) + 1
            record["updated"] = now_iso()
            try:
                override = record.get("query_override") or {}
                identity = resolve_movie_tmdb_id(provider, {
                    "title": override.get("title") or record.get("query_title") or record.get("title"),
                    "original_title": (None if override.get("title") else
                                       record.get("original_title")),
                    "year": override.get("year") if "year" in override else record.get("year"),
                    "imdb_id": None if override else record.get("imdb_id"),
                    "tmdb_id": (override.get("tmdb_id") if override else
                                record.get("local_tmdb_id") or record.get("tmdb_id")),
                })
                record["match"] = identity.get("method")
                record["candidates"] = identity.get("top") or []
                if not identity.get("ok"):
                    # A prior provider identity is durable evidence. Never erase
                    # or downgrade it merely because a later lookup failed.
                    if record.get("tmdb_id"):
                        record["status"] = "ok"
                    else:
                        record["status"] = "unresolved"
                    record["last_error"] = identity.get("reason")
                    attempt_results[key] = {
                        "ok": False, "status": "unresolved",
                        "reason": identity.get("reason"),
                        "candidates": identity.get("top") or [],
                    }
                else:
                    movie_id = identity["movie_id"]
                    details = provider.get(f"/movie/{movie_id}", {})
                    credits = provider.get(f"/movie/{movie_id}/credits", {})
                    record.update({"status": "ok", "tmdb_id": movie_id,
                                   "nfo": build_nfo(details, credits), "last_error": None})
                    poster_path = details.get("poster_path")
                    record["poster_url"] = base.rstrip("/") + "/" + size + poster_path if poster_path else None
                    attempt_results[key] = {
                        "ok": True, "status": "ok", "movie_id": movie_id,
                        "method": identity.get("method"),
                    }
            except Exception as error:
                record["status"] = "error"
                record["last_error"] = f"{type(error).__name__}: {error}"
                attempt_results[key] = {
                    "ok": False, "status": "error", "reason": record["last_error"],
                }
            processed += 1
            save_json_atomic(path, manifest)
            emit(reporter, "progress", key, status=record["status"], target_kind="movie", id=key)
    except KeyboardInterrupt:
        save_json_atomic(path, manifest)
        raise
    manifest["_meta"]["updated"] = now_iso()
    save_json_atomic(path, manifest)
    return {"processed": processed, "movies": len(manifest["movies"]),
            "status_counts": dict(Counter(x.get("status") for x in manifest["movies"].values())),
            # This invocation-scoped receipt is the authority for an explicit
            # replacement. Durable status may intentionally preserve older
            # successful work after a transient or ambiguous retry.
            "attempt_results": attempt_results}
