"""Semantic recipes for one frozen UI workflow scope.

Bulk is deliberately a small recipe table rather than an argv/eval facility.  A
collection cache is immutable presentation data, so it can carry a large frozen
scope without exceeding either Severin's bridge frame or the OS argv limit.
"""

import hashlib
import json
from collections import Counter
from pathlib import Path

from ..api import get_record
from ..storage import load_json


WORKFLOWS = frozenset({
    "missing-actor-images", "failed-actors", "lost-found", "missing-posters",
    "unresolved-movies", "failed-movies", "ambiguous-tv", "not-found-tv", "tv-errors",
})


def load_scope(config, workflow, scope_file, generation, count):
    """Validate and expand an immutable UI collection into deduplicated identities."""
    if workflow not in WORKFLOWS:
        raise ValueError("unknown Bulk workflow")
    path = Path(scope_file)
    cache = config.app_dir / ".cache" / "ui"
    try:
        path.resolve().relative_to(cache.resolve())
    except ValueError as error:
        raise ValueError("Bulk scope is outside the UI cache") from error
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Bulk scope exceeds the 32 MB collection limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    items = value.get("items") if isinstance(value, dict) else None
    actual_generation = hashlib.sha256(json.dumps(
        items, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()[:20]
    if (value.get("version") != 1 or not isinstance(items, list) or len(items) != count or
            value.get("generation") != generation or actual_generation != generation):
        raise ValueError("Bulk scope no longer matches its descriptor")
    identities = []
    for row in items:
        candidates = row.get("manifest_identities") if row.get("grouped") else None
        candidates = candidates if isinstance(candidates, list) else [
            row.get("identifier") or row.get("name") or row.get("local_target")]
        identities.extend(value for value in candidates if isinstance(value, str) and value)
    identities = list(dict.fromkeys(identities))
    if sum(len(value.encode("utf-8")) for value in identities) > 16 * 1024 * 1024:
        raise ValueError("Bulk identity scope exceeds the 16 MB limit")
    return identities


def _movie_targets(config, identities):
    return [get_record(config, "movie", value)["local_target"] for value in identities]


def _show_targets(config, identities):
    return [get_record(config, "show", value)["local_target"] for value in identities]


def _combined(*results, message="Finished"):
    counts = Counter()
    processed = 0
    for prefix, result in results:
        processed += int(result.get("processed", 0))
        phase_counts = result.get("counts") or result.get("status_counts") or {}
        for name, value in phase_counts.items():
            counts[name if name.startswith(prefix + "_") else f"{prefix}_{name}"] += value
    return {"processed": processed, "counts": dict(counts), "message": message}


def _item_result(identities, *results, message="Finished"):
    """Aggregate phase detail without double-counting scoped identities."""
    combined = _combined(*results, message=message)
    combined["processed"] = len(identities)
    return combined


def run(config, workflow, identities, reporter=None):
    """Run the allowlisted recipe while preserving pre-existing artifacts."""
    if workflow not in WORKFLOWS:
        raise ValueError("unknown Bulk workflow")
    if workflow == "missing-actor-images":
        from .movie_actor_scan import run as scan
        from .movie_actor_fetch import run as fetch
        from ..providers.tmdb import TMDBClient
        from ..transport import transport_from_config
        transport = transport_from_config(config)
        urls = load_json(config.state_path("actor_thumb_urls_tmdb.json"), {})
        missing_sources = [name for name in identities if not urls.get(name)]
        scanned = {"processed": 0, "counts": {}}
        if missing_sources:
            provider = TMDBClient(config.tmdb_api_key, config.tmdb_bearer_token,
                                  config.state_path("tmdb_api_cache.json"), transport)
            scanned = scan(config, provider, reporter, refresh=True, retry_failed=True,
                           targets=missing_sources)
        urls = load_json(config.state_path("actor_thumb_urls_tmdb.json"), {})
        available = [name for name in identities if urls.get(name)]
        unresolved = len(identities) - len(available)
        fetched = ({"processed": 0, "counts": {}} if not available else
                   fetch(config, reporter, retry_failed=True, overwrite=False,
                         targets=available, transport=transport))
        fetched.setdefault("counts", {})["unresolved_source"] = unresolved
        return _item_result(identities, ("identity", scanned), ("image", fetched),
                            message=(f"{unresolved} actor(s) have no image source"
                                     if unresolved else "Finished"))
    if workflow == "failed-actors":
        from .movie_actor_scan import run as scan
        from ..providers.tmdb import TMDBClient
        from ..transport import transport_from_config
        transport = transport_from_config(config)
        provider = TMDBClient(config.tmdb_api_key, config.tmdb_bearer_token,
                              config.state_path("tmdb_api_cache.json"), transport)
        return _item_result(identities, ("identity", scan(
            config, provider, reporter, refresh=True, retry_failed=True, targets=identities)))
    if workflow in {"lost-found", "unresolved-movies", "failed-movies"}:
        from .movie_scan import run as scan
        from ..providers.tmdb import TMDBClient
        from ..transport import transport_from_config
        transport = transport_from_config(config)
        targets = _movie_targets(config, identities)
        provider = TMDBClient(config.tmdb_api_key, config.tmdb_bearer_token,
                              config.state_path("tmdb_api_cache.json"), transport)
        scanned = scan(config, provider, reporter, refresh=True, targets=targets)
        if workflow != "lost-found":
            return _item_result(identities, ("identity", scanned))
        from .movie_materialize import run as materialize
        written = materialize(config, reporter, overwrite_nfo=False, overwrite_poster=False,
                              targets=targets, transport=transport, write_nfo=True,
                              write_poster=False)
        return _item_result(identities, ("identity", scanned), ("nfo", written))
    if workflow == "missing-posters":
        from .movie_materialize import run as materialize
        from ..transport import transport_from_config
        records = [get_record(config, "movie", value) for value in identities]
        targets = [record["local_target"] for record in records if record.get("poster_path")]
        unresolved = len(records) - len(targets)
        result = ({"processed": 0, "counts": {}} if not targets else
                  materialize(config, reporter, overwrite_nfo=False, overwrite_poster=False,
                              targets=targets, transport=transport_from_config(config),
                              write_nfo=False, write_poster=True))
        result["processed"] = int(result.get("processed", 0)) + unresolved
        result.setdefault("counts", {})["poster_unresolved_target"] = unresolved
        message = (f"{unresolved} item(s) have no safe poster target" if unresolved
                   else "Finished")
        combined = _item_result(identities, ("poster", result), message=message)
        combined["ok"] = not unresolved
        return combined
    from .tv_scan import run as scan
    from ..providers.tvdb import TVDBClient
    from ..transport import transport_from_config
    transport = transport_from_config(config)
    provider = TVDBClient(config.tvdb_api_key, config.tvdb_pin,
                          config.state_path("tvdb_api_cache.json"), transport)
    return _item_result(identities, ("identity", scan(
        config, provider, reporter, refresh=True, retry_errors=True,
        retry_ambiguous=True, retry_not_found=True, targets=_show_targets(config, identities),
    )))
