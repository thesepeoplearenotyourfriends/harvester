"""TMDB movie Stage 2. This module consumes frozen records only."""
from collections import Counter
import copy
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

from ..downloads import download_image
from ..events import emit
from ..artifacts import planned, use_committer
from ..storage import load_json, save_json_atomic
from .tv_materialize import image_extension

USER_AGENT = "local-tmdb-movie-materializer/1.0"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _add(parent, tag, value):
    if value is None or value == "":
        return
    node = ET.SubElement(parent, tag)
    node.text = str(value)


def render_movie_nfo(payload):
    if not isinstance(payload, dict):
        raise ValueError("record has no usable NFO payload")
    root = ET.Element("movie")
    for tag in ("title", "originaltitle", "year", "premiered", "plot", "tagline", "runtime"):
        _add(root, tag, payload.get(tag))
    for key, value in (payload.get("ids") or {}).items():
        if value is not None:
            node = ET.SubElement(root, "uniqueid", {"type": key})
            node.text = str(value)
    for source, tag in (("genre", "genre"), ("country", "country"), ("language", "language"), ("studio", "studio"), ("director", "director"), ("credits", "credits")):
        for value in payload.get(source) or []:
            _add(root, tag, value)
    for actor in payload.get("actor") or []:
        node = ET.SubElement(root, "actor")
        _add(node, "name", actor.get("name")); _add(node, "role", actor.get("role")); _add(node, "order", actor.get("order"))
    try:
        ET.indent(root, space="    ")
    except AttributeError:
        pass
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"


def run(config, reporter=None, limit=None, overwrite_nfo=False, overwrite_poster=False,
        targets=None, transport=None, downloader=None, write_nfo=True,
        write_poster=True, committer=None):
    committer = use_committer(committer)
    path = config.state_path("movie_manifest_tmdb.json")
    manifest = copy.deepcopy(load_json(path, None))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("movies"), dict):
        raise FileNotFoundError(f"movie manifest not found: {path}; run movies scan first")
    selected = set(targets or [])
    processed = 0
    counts = Counter()
    try:
        for key, record in manifest["movies"].items():
            if selected and key not in selected and str(record.get("tmdb_id")) not in selected:
                continue
            if record.get("status") != "ok":
                continue
            if limit is not None and processed >= limit:
                break
            state = record.setdefault("materialize", {})
            nfo_path = Path(record["nfo_path"])
            if write_nfo:
                if nfo_path.exists() and not overwrite_nfo:
                    state["nfo"] = {"status": "exists", "file": str(nfo_path), "bytes": nfo_path.stat().st_size, "updated": now_iso()}
                else:
                    data = render_movie_nfo(record.get("nfo"))
                    committer.write(nfo_path, data)
                    state["nfo"] = {"status": "ok", "file": str(nfo_path), "bytes": len(data), "updated": now_iso()}
            poster_value = record.get("poster_path")
            poster = Path(poster_value) if poster_value else None
            if write_poster and poster is None:
                state["poster"] = {"status": "unresolved_target", "updated": now_iso()}
                processed += 1
                counts["poster_unresolved_target"] += 1
                if write_nfo:
                    counts[state["nfo"]["status"]] += 1
                if committer.committing:
                    save_json_atomic(path, manifest)
                emit(reporter, "progress", key, status="unresolved_target",
                     target_kind="movie", id=key)
                continue
            existing = poster if poster and poster.exists() else poster.with_suffix(".png") if poster else None
            if write_poster:
                if existing.exists() and not overwrite_poster:
                    state["poster"] = {"status": "exists", "file": str(existing), "bytes": existing.stat().st_size, "updated": now_iso()}
                elif not record.get("poster_url"):
                    state["poster"] = {"status": "no_url", "updated": now_iso()}
                else:
                    try:
                        fetched = downloader(record["poster_url"]) if downloader else download_image(
                            record["poster_url"], user_agent=USER_AGENT,
                            transport=transport,
                        )
                        data, content_type = fetched if isinstance(fetched, tuple) else (fetched, "")
                        output = poster.with_suffix(image_extension(data, content_type))
                        committer.write(output, data)
                        cleanup_error = None
                        if existing and existing.exists() and existing != output:
                            try:
                                committer.unlink(existing)
                            except OSError as error:
                                cleanup_error = f"{type(error).__name__}: {error}"
                        state["poster"] = {"status": "ok", "file": str(output), "bytes": len(data), "content_type": content_type, "source_url": record["poster_url"], "updated": now_iso()}
                        if cleanup_error:
                            state["poster"]["cleanup_error"] = cleanup_error
                    except Exception as error:
                        state["poster"] = {"status": "error", "error": f"{type(error).__name__}: {error}", "source_url": record["poster_url"], "updated": now_iso()}
            processed += 1
            if write_nfo:
                counts[state["nfo"]["status"]] += 1
            if write_poster:
                counts[f"poster_{state['poster']['status']}"] += 1
            if committer.committing:
                save_json_atomic(path, manifest)
            emit(reporter, "progress", key, status="materialized", target_kind="movie", id=key)
    except KeyboardInterrupt:
        if committer.committing:
            save_json_atomic(path, manifest)
        raise
    manifest.setdefault("_meta", {})["last_materialize"] = now_iso()
    if committer.committing:
        save_json_atomic(path, manifest)
    if not committer.committing:
        return {"processed": processed, "planned_counts": dict(counts),
                "planned": planned(committer)}
    return {"processed": processed, "counts": dict(counts)}
