"""Offline durable-state queries shared by the machine CLI."""
from collections import Counter
from pathlib import Path

from .images import safe_actor_filename
from .storage import load_json


FILES = {"actor": "movie_actor_queue.json", "movie": "movie_manifest_tmdb.json", "show": "tv_show_urls_tvdb.json"}
COLLECTIONS = {"actor": "actors", "movie": "movies", "show": "shows"}


def records(config, kind):
    data = load_json(config.state_path(FILES[kind]), {})
    value = data.get(COLLECTIONS[kind], {}) if isinstance(data, dict) else {}
    return value if isinstance(value, dict) else {}


def decorate(config, kind, key, record):
    result = {"kind": kind, **record}
    result.setdefault("name" if kind == "actor" else "local_target", key)
    if kind == "actor":
        path = config.movie_root / ".actors" / safe_actor_filename(key)
        result["local_file"] = str(path) if path.is_file() else None
    elif kind == "show":
        base = Path(key)
        result["local_receipts"] = {"nfo": (base / "show.nfo").is_file(), "poster": (base / "poster.jpg").is_file() or (base / "poster.png").is_file()}
    else:
        poster = Path(record.get("poster_path", "")) if record.get("poster_path") else None
        result["local_receipts"] = {"nfo": Path(record.get("nfo_path", key)).is_file(), "poster": bool(poster and (poster.is_file() or poster.with_suffix(".png").is_file()))}
    return result


def get_record(config, kind, identifier):
    items = records(config, kind)
    for key, record in items.items():
        ids = (str(record.get("tmdb_id")), str(record.get("tvdb_id")))
        if identifier == key or identifier in ids or (kind == "actor" and identifier.casefold() == key.casefold()):
            return decorate(config, kind, key, record)
    raise KeyError(f"{kind} not found: {identifier}")


def brief_record(config, kind, key, record):
    """Return only fields useful for queue display, never frozen provider data."""
    decorated = decorate(config, kind, key, record)
    if kind == "actor":
        return {"kind": kind, "name": decorated["name"],
                "status": decorated.get("status", "pending"),
                "local_file": bool(decorated["local_file"])}
    receipts = decorated["local_receipts"]
    return {"kind": kind, "local_target": decorated["local_target"],
            "label": record.get("title") or record.get("name") or Path(key).name,
            "status": decorated.get("status", "pending"),
            "local_receipts": receipts,
            **({"tmdb_id": record.get("tmdb_id")} if kind == "movie" else
               {"tvdb_id": record.get("tvdb_id")})}


def list_records(config, kind, status=None, limit=None, missing=None, brief=False):
    source = records(config, kind)
    values = [(key, record, decorate(config, kind, key, record))
              for key, record in source.items()]
    if status:
        statuses = ("failed", "error") if kind == "movie" and status == "failed" else (status,)
        values = [item for item in values if item[2].get("status") in statuses]
    if missing:
        if kind == "actor":
            values = [item for item in values if not item[2].get("local_file")]
        else:
            values = [item for item in values
                      if not item[2].get("local_receipts", {}).get(missing)]
    if brief:
        values = [brief_record(config, kind, key, record)
                  for key, record, decorated in values]
    else:
        values = [decorated for key, record, decorated in values]
    return values[:limit] if limit is not None else values


def search(config, query, limit=50):
    """Search durable identities only. No provider is constructed or contacted."""
    needle = query.strip().casefold()
    if not needle:
        return []
    found = []
    for kind in ("actor", "movie", "show"):
        for key, record in records(config, kind).items():
            identity = [key, record.get("name"), record.get("title"),
                        record.get("tmdb_id"), record.get("tvdb_id"), record.get("imdb_id")]
            if not any(needle in str(value).casefold() for value in identity if value is not None):
                continue
            item = brief_record(config, kind, key, record)
            label = item.get("name") or item.get("label") or Path(key).name
            missing = kind == "actor" and not item["local_file"]
            found.append({"kind": kind, "identifier": key, "label": label,
                          "secondary": kind + (" · image missing" if missing else ""),
                          "status": item.get("status", "pending")})
    return found[:max(0, min(limit, 100))]


def _movie_poster_exists(record):
    value = record.get("poster_path")
    if not value:
        return False
    path = Path(value)
    return path.is_file() or path.with_suffix(".png").is_file()


def inventory(config):
    actors = records(config, "actor")
    actor_counts = Counter(value.get("status", "pending") for value in actors.values())
    local = sum((config.movie_root / ".actors" / safe_actor_filename(name)).is_file() for name in actors)
    movies = records(config, "movie")
    shows = records(config, "show")
    return {
        "actors": {"total": len(actors), "local": local, "pending_unresolved": actor_counts["pending"], "ok": actor_counts["ok"], "failed": actor_counts["failed"], "error": actor_counts["error"]},
        "movies": {"total": len(movies), "missing_nfo": sum(not Path(x.get("nfo_path", "")).is_file() for x in movies.values()), "missing_poster": sum(not _movie_poster_exists(x) for x in movies.values()), "unresolved": sum(x.get("status") == "unresolved" for x in movies.values()), "failed": sum(x.get("status") in ("failed", "error") for x in movies.values())},
        "tv": dict(Counter(x.get("status", "pending") for x in shows.values())),
    }
