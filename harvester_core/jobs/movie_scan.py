"""TMDB movie Stage 1: discover local targets and freeze provider data."""
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

from ..events import emit
from ..storage import load_json, save_json_atomic
from .movie_actor_scan import clean_year, resolve_movie_tmdb_id

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
    for directory, dirs, files in __import__("os").walk(root):
        dirs[:] = [name for name in dirs if not name.startswith(".")]
        base = Path(directory)
        nfos = sorted(base / name for name in files if name.lower().endswith(".nfo"))
        videos = sorted(
            base / name for name in files
            if Path(name).suffix.casefold() in {".mkv", ".mp4", ".avi", ".m4v", ".mov"}
        )
        # A lone video supplies an unambiguous sibling NFO target. Multiple
        # videos without an NFO remain untouched because no title owns the path.
        targets = nfos or ([videos[0].with_suffix(".nfo")] if len(videos) == 1 else [])
        for nfo_path in targets:
            title = nfo_path.stem
            original_title = None
            year = clean_year(base.name) or clean_year(title)
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
            # Existing paths are receipts. There is no repository-wide movie
            # poster naming rule, so only retain a unique existing poster and
            # leave a missing/ambiguous target unresolved for later policy.
            posters = sorted(
                path for path in base.iterdir()
                if path.is_file() and "poster" in path.stem.casefold()
                and path.suffix.casefold() in (".jpg", ".jpeg", ".png")
            )
            poster = (
                posters[0].resolve()
                if len(targets) == 1 and len(posters) == 1
                else None
            )
            found[str(nfo_path.resolve())] = {
                "kind": "movie", "local_target": str(nfo_path.resolve()),
                "nfo_path": str(nfo_path.resolve()),
                "poster_path": str(poster) if poster else None,
                "poster_target_status": "resolved" if poster else "unresolved",
                "title": title, "original_title": original_title,
                "year": year, "imdb_id": imdb_id,
                "local_tmdb_id": int(tmdb_id) if str(tmdb_id or "").isdigit() else None,
                "status": "pending", "tries": 0, "tmdb_id": None, "match": None,
                "candidates": [], "nfo": None, "poster_url": None,
                "last_error": None, "materialize": {}, "updated": None,
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
    for key, record in discovered.items():
        existing = manifest["movies"].get(key)
        if existing is None:
            manifest["movies"][key] = record
            continue
        # Earlier manifests may contain a derived, nonexistent poster path.
        # Re-evaluate only this local-target receipt while retaining frozen
        # provider metadata and retry history.
        existing["poster_path"] = record["poster_path"]
        existing["poster_target_status"] = record["poster_target_status"]
    selected = set(targets or [])
    processed = 0
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
            if not refresh and Path(record["nfo_path"]).exists() and poster_path and poster_path.exists():
                continue
            if limit is not None and processed >= limit:
                break
            record["tries"] = int(record.get("tries") or 0) + 1
            record["updated"] = now_iso()
            try:
                identity = resolve_movie_tmdb_id(provider, {
                    "title": record.get("title"),
                    "original_title": record.get("original_title"),
                    "year": record.get("year"),
                    "imdb_id": record.get("imdb_id"),
                    "tmdb_id": record.get("local_tmdb_id"),
                })
                record["match"] = identity.get("method")
                record["candidates"] = identity.get("top") or []
                if not identity.get("ok"):
                    record["status"] = "unresolved"
                    record["last_error"] = identity.get("reason")
                else:
                    movie_id = identity["movie_id"]
                    details = provider.get(f"/movie/{movie_id}", {})
                    credits = provider.get(f"/movie/{movie_id}/credits", {})
                    record.update({"status": "ok", "tmdb_id": movie_id,
                                   "nfo": build_nfo(details, credits), "last_error": None})
                    poster_path = details.get("poster_path")
                    record["poster_url"] = base.rstrip("/") + "/" + size + poster_path if poster_path else None
            except Exception as error:
                record["status"] = "error"
                record["last_error"] = f"{type(error).__name__}: {error}"
            processed += 1
            save_json_atomic(path, manifest)
            emit(reporter, "progress", key, status=record["status"], target_kind="movie", id=key)
    except KeyboardInterrupt:
        save_json_atomic(path, manifest)
        raise
    manifest["_meta"]["updated"] = now_iso()
    save_json_atomic(path, manifest)
    return {"processed": processed, "movies": len(manifest["movies"]), "status_counts": dict(Counter(x.get("status") for x in manifest["movies"].values()))}
