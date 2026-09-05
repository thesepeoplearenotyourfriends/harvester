"""Offline durable-state queries shared by the machine CLI."""
from collections import Counter
import xml.etree.ElementTree as ET
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


def _nfo_fields(path):
    """Read the small, useful subset of local NFO data without trusting state."""
    if not path.is_file():
        return {}, None
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as error:
        return {}, str(error)
    fields = {}
    for name in ("title", "originaltitle", "sorttitle", "year", "premiered",
                 "plot", "runtime", "mpaa", "studio", "status"):
        value = root.findtext(name)
        if value and value.strip():
            fields[name] = value.strip()
    unique_ids = {}
    for node in root.findall("uniqueid"):
        if node.text and node.text.strip():
            unique_ids[node.get("type", "unknown")] = node.text.strip()
    if unique_ids:
        fields["unique_ids"] = unique_ids
    return fields, None


def _poster_in(directory, preferred=None):
    candidates = []
    if preferred:
        target = Path(preferred)
        candidates.extend((target, target.with_suffix(".jpg"), target.with_suffix(".png")))
    candidates.extend(directory / name for name in
                      ("poster.jpg", "poster.png", "folder.jpg", "folder.png"))
    seen = set()
    for path in candidates:
        if path not in seen and path.is_file():
            return path
        seen.add(path)
    return None


def _movie_directory(key, record):
    nfo = Path(record.get("nfo_path") or key)
    return nfo.parent if nfo.suffix.casefold() == ".nfo" else nfo


def inspect_item(config, kind, identifier):
    """Return an offline, read-only view of artifacts that exist right now.

    Unlike ``get_record``, this is a presentation contract rather than a durable
    work-record contract.  A movie directory can own multiple manifest entries;
    callers get all identities and an explicit ambiguity marker in that case.
    """
    if kind not in ("movie", "show"):
        raise KeyError(f"artifact inspection is unavailable for {kind}")
    source = records(config, kind)
    matches = []
    for key, record in source.items():
        directory = _movie_directory(key, record) if kind == "movie" else Path(key)
        ids = (key, str(record.get("tmdb_id")), str(record.get("tvdb_id")), str(directory))
        if identifier in ids:
            matches.append((key, record, directory))
    if not matches:
        raise KeyError(f"{kind} not found: {identifier}")
    directory = matches[0][2]
    # Selecting any movie receipt inspects its whole directory, which is the
    # filesystem ownership boundary used by poster queues.
    if kind == "movie":
        matches = [(key, record, candidate) for key, record in source.items()
                   if (candidate := _movie_directory(key, record)) == directory]
    else:
        matches = matches[:1]
    nfos = []
    for key, record, _ in matches:
        nfo_path = (Path(record.get("nfo_path") or key) if kind == "movie"
                    else directory / "show.nfo")
        fields, parse_error = _nfo_fields(nfo_path)
        nfos.append({"manifest_identity": key, "present": nfo_path.is_file(),
                     "path": str(nfo_path), "fields": fields,
                     **({"parse_error": parse_error} if parse_error else {})})
    preferred = matches[0][1].get("poster_path") if kind == "movie" else None
    poster = _poster_in(directory, preferred)
    videos = sorted(str(path) for path in directory.iterdir()
                    if path.is_file() and path.suffix.casefold() in
                    {".mkv", ".mp4", ".avi", ".mov", ".m4v"}) if directory.is_dir() else []
    identities = [key for key, _, _ in matches]
    ambiguous = kind == "movie" and len(identities) > 1
    label = (nfos[0]["fields"].get("title") if nfos else None) or directory.name
    return {"kind": kind, "identifier": str(directory), "label": label,
            "directory": str(directory), "directory_present": directory.is_dir(),
            "manifest_identities": identities,
            "ownership": {"status": "ambiguous" if ambiguous else "unambiguous",
                          "reason": "multiple movie NFO records share this directory" if ambiguous else None},
            "nfo": nfos[0] if len(nfos) == 1 else {"present": any(x["present"] for x in nfos),
                                                    "count": len(nfos)},
            "nfos": nfos, "poster": {"present": poster is not None,
                                       "path": str(poster) if poster else None},
            "video_files": videos, "video_count": len(videos)}


def list_artifacts(config, kind, status=None, missing=None):
    """Project durable identities onto unique filesystem artifact owners."""
    projected = {}
    for key, record in records(config, kind).items():
        if status:
            statuses = ("failed", "error") if kind == "movie" and status == "failed" else (status,)
            if record.get("status") not in statuses:
                continue
        directory = _movie_directory(key, record) if kind == "movie" else Path(key)
        projected.setdefault(str(directory), []).append(key)
    items = []
    for directory in projected:
        item = inspect_item(config, kind, directory)
        if missing and item[missing]["present"]:
            continue
        items.append(item)
    return items


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


def inventory(config):
    actors = records(config, "actor")
    actor_counts = Counter(value.get("status", "pending") for value in actors.values())
    local = sum((config.movie_root / ".actors" / safe_actor_filename(name)).is_file() for name in actors)
    movies = records(config, "movie")
    shows = records(config, "show")
    movie_artifacts = list_artifacts(config, "movie")
    return {
        "actors": {"total": len(actors), "local": local, "pending_unresolved": actor_counts["pending"], "ok": actor_counts["ok"], "failed": actor_counts["failed"], "error": actor_counts["error"]},
        "movies": {"total": len(movies), "missing_nfo": sum(not Path(x.get("nfo_path", "")).is_file() for x in movies.values()), "missing_poster": sum(not x["poster"]["present"] for x in movie_artifacts), "unresolved": sum(x.get("status") == "unresolved" for x in movies.values()), "failed": sum(x.get("status") in ("failed", "error") for x in movies.values())},
        "tv": dict(Counter(x.get("status", "pending") for x in shows.values())),
    }
