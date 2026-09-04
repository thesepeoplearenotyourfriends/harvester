"""Offline reconciliation of durable censuses with the local filesystem."""

from collections import Counter
import os
from pathlib import Path

from .api import inventory
from .jobs.movie_actor_scan import (
    build_actor_work_queue,
    write_final_actor_db_from_queue,
)
from .jobs.movie_scan import discover_movies, now_iso as movie_now_iso
from .jobs.tv_scan import new_manifest, new_record, scan_immediate_show_dirs
from .storage import load_json, save_json_atomic


def _preserve_remote_state(fresh, previous, local_fields):
    """Keep provider work for an identity while replacing local observations."""
    if not isinstance(previous, dict):
        return fresh
    merged = dict(previous)
    for field in local_fields:
        merged[field] = fresh[field]
    return merged


def _require_readable_directory(path, label):
    path = Path(path)
    if not path.is_dir() or not os.access(path, os.R_OK | os.X_OK):
        raise FileNotFoundError(f"{label} is not a readable directory: {path}")


def _reconciled_meta(previous, fresh, census_fields):
    """Retain run history while replacing fields owned by local discovery."""
    old_meta = previous.get("_meta", {}) if isinstance(previous, dict) else {}
    new_meta = fresh.get("_meta", {})
    merged = dict(old_meta) if isinstance(old_meta, dict) else {}
    if "created" not in merged and new_meta.get("created"):
        merged["created"] = new_meta["created"]
    for field in census_fields:
        if field in new_meta:
            merged[field] = new_meta[field]
    return merged


def _refuse_suspicious_empty(previous, collection, discovered_count, label):
    existing = previous.get(collection, {}) if isinstance(previous, dict) else {}
    if isinstance(existing, dict) and existing and discovered_count == 0:
        raise RuntimeError(
            f"refusing to replace non-empty {label} census with empty discovery"
        )


def rescan_actors(config, fresh_queue=None):
    _require_readable_directory(config.movie_root, "MOVIE_ROOT")
    path = config.state_path("movie_actor_queue.json")
    previous = load_json(path, {})
    previous_actors = previous.get("actors", {}) if isinstance(previous, dict) else {}
    queue = fresh_queue or build_actor_work_queue([str(config.movie_root)])
    _refuse_suspicious_empty(previous, "actors", len(queue["actors"]), "actor")
    for name, fresh in queue["actors"].items():
        queue["actors"][name] = _preserve_remote_state(
            fresh, previous_actors.get(name), ("contexts",),
        )
    queue["_meta"] = _reconciled_meta(
        previous, queue,
        ("version", "updated", "source_dirs", "nfo_count", "actor_count"),
    )
    save_json_atomic(path, queue)
    write_final_actor_db_from_queue(
        queue, config.state_path("actor_thumb_urls_tmdb.json"),
    )
    counts = inventory(config)["actors"]
    return {"kind": "actors", **counts}


def rescan_movies(config, discovered=None):
    _require_readable_directory(config.movie_root, "MOVIE_ROOT")
    path = config.state_path("movie_manifest_tmdb.json")
    previous = load_json(path, {})
    previous_movies = previous.get("movies", {}) if isinstance(previous, dict) else {}
    discovered = discovered if discovered is not None else discover_movies(config.movie_root)
    _refuse_suspicious_empty(previous, "movies", len(discovered), "movie")
    local_fields = (
        "kind", "local_target", "nfo_path", "poster_path",
        "poster_target_status", "title", "original_title", "year",
        "imdb_id", "local_tmdb_id",
    )
    movies = {
        key: _preserve_remote_state(fresh, previous_movies.get(key), local_fields)
        for key, fresh in discovered.items()
    }
    fresh_manifest = {"_meta": {
        "version": 1, "source": "TMDB", "created": movie_now_iso(),
        "updated": movie_now_iso(),
        "library_root": str(config.movie_root), "movie_count": len(movies),
    }}
    manifest = {"_meta": _reconciled_meta(
        previous, fresh_manifest,
        ("version", "source", "updated", "library_root", "movie_count"),
    ), "movies": movies}
    save_json_atomic(path, manifest)
    return {"kind": "movies", "total": len(movies),
            "status_counts": dict(Counter(x.get("status", "pending") for x in movies.values()))}


def rescan_shows(config, directories=None):
    _require_readable_directory(config.tv_root, "TV_ROOT")
    path = config.state_path("tv_show_urls_tvdb.json")
    previous = load_json(path, {})
    previous_shows = previous.get("shows", {}) if isinstance(previous, dict) else {}
    directories = (
        directories
        if directories is not None
        else scan_immediate_show_dirs(config.tv_root)
    )
    _refuse_suspicious_empty(previous, "shows", len(directories), "show")
    manifest = new_manifest(config.tv_root)
    local_fields = ("folder_name", "query_title", "query_year", "local_season")
    for directory in directories:
        key = str(directory)
        manifest["shows"][key] = _preserve_remote_state(
            new_record(directory), previous_shows.get(key), local_fields,
        )
    manifest["_meta"]["show_count"] = len(manifest["shows"])
    manifest["_meta"] = _reconciled_meta(
        previous, manifest,
        ("version", "source", "updated", "library_root", "notes", "show_count"),
    )
    save_json_atomic(path, manifest)
    shows = manifest["shows"]
    return {"kind": "shows", "total": len(shows),
            "status_counts": dict(Counter(x.get("status", "pending") for x in shows.values()))}


def rescan(config):
    # Validate and discover every storage boundary before the first durable
    # write. An empty mounted directory is treated like an unavailable one when
    # it would erase a census that previously contained records.
    _require_readable_directory(config.movie_root, "MOVIE_ROOT")
    _require_readable_directory(config.tv_root, "TV_ROOT")
    actor_queue = build_actor_work_queue([str(config.movie_root)])
    movies = discover_movies(config.movie_root)
    show_directories = scan_immediate_show_dirs(config.tv_root)
    previous = {
        "actors": load_json(config.state_path("movie_actor_queue.json"), {}),
        "movies": load_json(config.state_path("movie_manifest_tmdb.json"), {}),
        "shows": load_json(config.state_path("tv_show_urls_tvdb.json"), {}),
    }
    _refuse_suspicious_empty(previous["actors"], "actors", len(actor_queue["actors"]), "actor")
    _refuse_suspicious_empty(previous["movies"], "movies", len(movies), "movie")
    _refuse_suspicious_empty(previous["shows"], "shows", len(show_directories), "show")
    results = {
        "actors": rescan_actors(config, actor_queue),
        "movies": rescan_movies(config, movies),
        "shows": rescan_shows(config, show_directories),
    }
    return {"rescanned": results, "inventory": inventory(config)}
