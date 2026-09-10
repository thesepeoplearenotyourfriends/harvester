"""Offline durable-state queries shared by the machine CLI."""
from collections import Counter
import xml.etree.ElementTree as ET
from pathlib import Path

from .images import safe_actor_filename
from .nfo import first_indexable_nfo, inspect_nfo_candidate, inspect_nfo_directory
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
        selected, _, _ = first_indexable_nfo(_movie_directory(key, record))
        result["local_receipts"] = {"nfo": bool(selected), "poster": bool(poster and (poster.is_file() or poster.with_suffix(".png").is_file()))}
    return result


def get_record(config, kind, identifier):
    items = records(config, kind)
    for key, record in items.items():
        ids = (str(record.get("tmdb_id")), str(record.get("tvdb_id")))
        if identifier == key or identifier in ids or (kind == "actor" and identifier.casefold() == key.casefold()):
            return decorate(config, kind, key, record)
    raise KeyError(f"{kind} not found: {identifier}")


def get_record_by_identity(config, kind, identifier):
    """Return a record only when *identifier* is its durable mapping key.

    Search emits these keys for follow-up mutations.  Provider identifiers are
    deliberately not accepted here: they are mutable adapter data and can be
    shared or otherwise become ambiguous over the lifetime of durable state.
    """
    items = records(config, kind)
    if identifier not in items:
        raise KeyError(f"{kind} not found: {identifier}")
    return decorate(config, kind, identifier, items[identifier])


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


def _poster_candidates(directory, preferred=None):
    """Return actual local images following Harvester's poster-name convention."""
    candidates = []
    if directory.is_dir():
        try:
            candidates.extend(path for path in directory.iterdir()
                              if path.is_file() and "poster" in path.stem.casefold()
                              and path.suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp"})
        except OSError:
            pass
    if preferred:
        target = Path(preferred)
        if target.is_file():
            candidates.append(target)
    return sorted(set(candidates), key=lambda path: str(path).casefold())


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
    ordered = source.items()
    if identifier in source:
        # A manifest identity is more specific than a repeated provider ID.
        # Keep it first so state-based queues inspect the record the user chose.
        ordered = [(identifier, source[identifier]),
                   *((key, record) for key, record in source.items() if key != identifier)]
    for key, record in ordered:
        directory = _movie_directory(key, record) if kind == "movie" else Path(key)
        ids = (key, str(record.get("tmdb_id")), str(record.get("tvdb_id")), str(directory))
        if identifier in ids:
            matches.append((key, record, directory))
    if not matches:
        raise KeyError(f"{kind} not found: {identifier}")
    selected = matches[0]
    directory = selected[2]
    # Selecting any movie receipt inspects its whole directory, which is the
    # filesystem ownership boundary used by poster queues.
    if kind == "movie":
        siblings = [(key, record, candidate) for key, record in source.items()
                    if (candidate := _movie_directory(key, record)) == directory
                    and key != selected[0]]
        matches = [selected, *siblings]
    else:
        matches = matches[:1]
    nfos = []
    for key, record, _ in matches:
        nfo_path = (Path(record.get("nfo_path") or key) if kind == "movie"
                    else directory / "show.nfo")
        fields, parse_error = _nfo_fields(nfo_path)
        consumer_present = (inspect_nfo_candidate(nfo_path)["indexable_by_movies_ui"]
                            if kind == "movie" and nfo_path.is_file() else nfo_path.is_file())
        nfos.append({"manifest_identity": key, "present": consumer_present,
                     "filesystem_present": nfo_path.is_file(),
                     "path": str(nfo_path), "fields": fields,
                     **({"parse_error": parse_error} if parse_error else {})})
    preferred = matches[0][1].get("poster_path") if kind == "movie" else None
    posters = _poster_candidates(directory, preferred)
    nfo_candidates, nfo_generation = inspect_nfo_directory(directory)
    for candidate in nfo_candidates:
        candidate["usable"] = (candidate["indexable_by_movies_ui"] if kind == "movie" else
                               candidate["valid_for_manual_intake"] and
                               candidate["root"] == "tvshow")
    usable_candidates = [candidate for candidate in nfo_candidates if candidate["usable"]]
    expected = Path(matches[0][1].get("nfo_path") or matches[0][0]) if kind == "movie" else directory / "show.nfo"
    selected_candidate = next((candidate for candidate in usable_candidates
                               if candidate["name"] == expected.name and expected.is_file()), None)
    effective = None
    if selected_candidate or len(usable_candidates) == 1:
        candidate = selected_candidate or usable_candidates[0]
        actual = directory / candidate["name"]
        fields, _ = _nfo_fields(actual)
        effective = {"present": True, "path": str(actual), "name": candidate["name"],
                     "fields": fields, "ambiguous": False, "candidate_count": len(usable_candidates)}
    elif len(usable_candidates) > 1:
        effective = {"present": True, "ambiguous": True,
                     "candidate_count": len(usable_candidates), "fields": {}}
    else:
        malformed = next((candidate for candidate in nfo_candidates if candidate["present"]), None)
        effective = {"present": False, "unusable": bool(malformed),
                     "name": malformed.get("name") if malformed else None,
                     "parse_error": malformed.get("parse_error") if malformed else None,
                     "parse_line": malformed.get("parse_line") if malformed else None,
                     "parse_column": malformed.get("parse_column") if malformed else None,
                     "fields": {}}
    videos = sorted(str(path) for path in directory.iterdir()
                    if path.is_file() and path.suffix.casefold() in
                    {".mkv", ".mp4", ".avi", ".mov", ".m4v"}) if directory.is_dir() else []
    identities = [key for key, _, _ in matches]
    ambiguous = kind == "movie" and len(identities) > 1
    label = (nfos[0]["fields"].get("title") if nfos else None) or directory.name
    return {"kind": kind, "identifier": identifier, "label": label,
            "directory": str(directory), "directory_present": directory.is_dir(),
            "selected_manifest_identity": selected[0],
            "manifest_identities": identities,
            "ownership": {"status": "ambiguous" if ambiguous else "unambiguous",
                          "reason": "multiple movie NFO records share this directory" if ambiguous else None},
            "nfo": effective,
            "nfo_candidates": nfo_candidates,
            "nfo_candidates_generation": nfo_generation,
            "nfos": nfos, "poster": {"present": bool(posters),
                                       "path": str(posters[0]) if posters else None,
                                       "candidates": [str(path) for path in posters]},
            "video_files": videos, "video_count": len(videos)}


def list_artifacts(config, kind, status=None, missing=None, group_directories=False):
    """Return compact artifact queue rows, grouping only when explicitly asked."""
    source = records(config, kind)
    directory_members = {}
    if group_directories and kind == "movie":
        for key, record in source.items():
            directory = _movie_directory(key, record)
            directory_members.setdefault(directory, []).append(key)
    selected = []
    for key, record in source.items():
        if status:
            statuses = ("failed", "error") if kind == "movie" and status == "failed" else (status,)
            if record.get("status") not in statuses:
                continue
        selected.append((key, record))
    projected = {}
    for key, record in selected:
        directory = _movie_directory(key, record) if kind == "movie" else Path(key)
        owner = str(directory) if group_directories and kind == "movie" else key
        projected.setdefault(owner, []).append((key, record, directory))
    items = []
    for owner, entries in projected.items():
        key, record, directory = entries[0]
        nfo = Path(record.get("nfo_path") or key) if kind == "movie" else directory / "show.nfo"
        posters = _poster_candidates(directory, record.get("poster_path") if kind == "movie" else None)
        selected_nfo, _, _ = first_indexable_nfo(directory) if kind == "movie" else (None, None, None)
        presence = {"nfo": bool(selected_nfo) if kind == "movie" else nfo.is_file(),
                    "poster": bool(posters)}
        if missing and presence[missing]:
            continue
        all_identities = (directory_members[directory]
                          if group_directories and kind == "movie" else [key])
        ambiguous = group_directories and kind == "movie" and len(all_identities) > 1
        items.append({"kind": kind, "identifier": owner,
                      "label": record.get("title") or record.get("name") or directory.name,
                      "directory": str(directory), "manifest_identities": all_identities,
                      "nfo_present": presence["nfo"], "poster_present": presence["poster"],
                      "grouped": bool(group_directories and kind == "movie"),
                      "ownership": "ambiguous" if ambiguous else "unambiguous"})
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
    movie_directories = {_movie_directory(key, record) for key, record in movies.items()}
    return {
        "actors": {"total": len(actors), "local": local, "pending_unresolved": actor_counts["pending"], "ok": actor_counts["ok"], "failed": actor_counts["failed"], "error": actor_counts["error"]},
        "movies": {"total": len(movies), "missing_nfo": sum(not first_indexable_nfo(_movie_directory(key, value))[0] for key, value in movies.items()), "missing_poster": sum(not _poster_candidates(directory) for directory in movie_directories), "unresolved": sum(x.get("status") == "unresolved" for x in movies.values()), "failed": sum(x.get("status") in ("failed", "error") for x in movies.values())},
        "tv": {"total": len(shows),
               "missing_nfo": sum(not (Path(key) / "show.nfo").is_file()
                                  for key in shows),
               "missing_poster": sum(not _poster_candidates(Path(key)) for key in shows),
               **dict(Counter(x.get("status", "pending") for x in shows.values()))},
    }
