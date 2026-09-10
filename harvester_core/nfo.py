"""Shared filesystem NFO classification matching the Movies UI consumer."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def inspect_nfo_candidate(path):
    """Describe one candidate using the consumer's sole ET.parse acceptance rule."""
    path = Path(path)
    root = None
    parse_error = None
    line = column = None
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as error:
        parse_error = str(error)
        if isinstance(error, ET.ParseError) and getattr(error, "position", None):
            line, column = error.position
    raw = None
    try:
        stat = path.stat()
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        token_source = f"{path.name}\0{stat.st_size}\0{stat.st_mtime_ns}\0{digest}"
        token = hashlib.sha256(token_source.encode("utf-8")).hexdigest()[:32]
    except OSError:
        token = None
    title = None
    if root is not None:
        value = root.findtext("title")
        title = value.strip() if value and value.strip() else None
    valid_for_manual_intake = False
    if raw is not None and len(raw) <= 1_000_000:
        try:
            text = raw.decode("utf-8")
            folded = text.casefold()
            manual_root = ET.fromstring(text)
            valid_for_manual_intake = (
                "<!doctype" not in folded and "<!entity" not in folded and
                manual_root.tag in ("movie", "tvshow") and
                bool((manual_root.findtext("title") or "").strip()))
        except (UnicodeDecodeError, ET.ParseError):
            pass
    return {"name": path.name, "present": path.is_file(), "symlink": path.is_symlink(),
            "parseable": root is not None, "indexable_by_movies_ui": root is not None,
            "valid_for_manual_intake": valid_for_manual_intake,
            "parse_error": parse_error, "parse_line": line, "parse_column": column,
            "root": root.tag if root is not None else None, "title": title,
            "token": token}


def inspect_nfo_directory(directory):
    """Return deterministic candidate descriptions and a set generation token."""
    directory = Path(directory)
    try:
        paths = sorted((path for path in directory.iterdir()
                        if path.is_file()
                        and path.suffix.casefold() == ".nfo"),
                       key=lambda path: (path.name.casefold(), path.name))
    except OSError:
        paths = []
    candidates = [inspect_nfo_candidate(path) for path in paths]
    stable = [{key: item.get(key) for key in ("name", "present", "token")}
              for item in candidates]
    generation = hashlib.sha256(json.dumps(
        stable, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:32]
    return candidates, generation


def first_indexable_nfo(directory):
    """Return the first candidate accepted by Movies UI, plus all diagnostics."""
    candidates, generation = inspect_nfo_directory(directory)
    selected = next((item for item in candidates if item["indexable_by_movies_ui"]), None)
    return selected, candidates, generation
