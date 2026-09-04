"""Offline reconciliation of durable censuses with the local filesystem."""

from collections import Counter

from .api import inventory
from .jobs.movie_actor_scan import build_actor_work_queue
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


def rescan_actors(config):
    path = config.state_path("movie_actor_queue.json")
    previous = load_json(path, {})
    previous_actors = previous.get("actors", {}) if isinstance(previous, dict) else {}
    queue = build_actor_work_queue([str(config.movie_root)])
    for name, fresh in queue["actors"].items():
        queue["actors"][name] = _preserve_remote_state(
            fresh, previous_actors.get(name), ("contexts",),
        )
    save_json_atomic(path, queue)
    counts = inventory(config)["actors"]
    return {"kind": "actors", **counts}


def rescan_movies(config):
    path = config.state_path("movie_manifest_tmdb.json")
    previous = load_json(path, {})
    previous_movies = previous.get("movies", {}) if isinstance(previous, dict) else {}
    discovered = discover_movies(config.movie_root)
    local_fields = (
        "kind", "local_target", "nfo_path", "poster_path",
        "poster_target_status", "title", "original_title", "year",
        "imdb_id", "local_tmdb_id",
    )
    movies = {
        key: _preserve_remote_state(fresh, previous_movies.get(key), local_fields)
        for key, fresh in discovered.items()
    }
    manifest = {
        "_meta": {"version": 1, "source": "TMDB", "updated": movie_now_iso()},
        "movies": movies,
    }
    save_json_atomic(path, manifest)
    return {"kind": "movies", "total": len(movies),
            "status_counts": dict(Counter(x.get("status", "pending") for x in movies.values()))}


def rescan_shows(config):
    path = config.state_path("tv_show_urls_tvdb.json")
    previous = load_json(path, {})
    previous_shows = previous.get("shows", {}) if isinstance(previous, dict) else {}
    manifest = new_manifest(config.tv_root)
    local_fields = ("folder_name", "query_title", "query_year", "local_season")
    for directory in scan_immediate_show_dirs(config.tv_root):
        key = str(directory)
        manifest["shows"][key] = _preserve_remote_state(
            new_record(directory), previous_shows.get(key), local_fields,
        )
    save_json_atomic(path, manifest)
    shows = manifest["shows"]
    return {"kind": "shows", "total": len(shows),
            "status_counts": dict(Counter(x.get("status", "pending") for x in shows.values()))}


def rescan(config, target):
    operations = {
        "actors": rescan_actors,
        "movies": rescan_movies,
        "shows": rescan_shows,
    }
    selected = operations if target == "all" else {target: operations[target]}
    results = {name: operation(config) for name, operation in selected.items()}
    return {"rescanned": results, "inventory": inventory(config)}
