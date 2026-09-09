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
from ..artifacts import RecordingCommitter, persist_preparation
from ..storage import load_json
from ..storage import save_json_atomic
from ..events import emit


WORKFLOWS = frozenset({
    "missing-actor-images", "failed-actors", "lost-found", "missing-posters",
    "unresolved-movies", "failed-movies", "ambiguous-tv", "not-found-tv", "tv-errors",
    "missing-tv-nfo", "missing-tv-posters",
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


def load_scope_items(config, workflow, scope_file, generation, count):
    """Return manifest identities grouped by their frozen logical UI row."""
    path = Path(scope_file)
    # Reuse the complete validation performed by load_scope before retaining
    # the presentation grouping needed by the durable Inbox.
    load_scope(config, workflow, path, generation, count)
    items = json.loads(path.read_text(encoding="utf-8"))["items"]
    grouped = []
    for row in items:
        candidates = row.get("manifest_identities") if row.get("grouped") else None
        candidates = candidates if isinstance(candidates, list) else [
            row.get("identifier") or row.get("name") or row.get("local_target")]
        identities = list(dict.fromkeys(value for value in candidates
                                        if isinstance(value, str) and value))
        grouped.append({"identities": identities, "display_title":
                        row.get("display_name") or row.get("label") or row.get("name") or
                        row.get("local_target") or (identities[0] if identities else "Item"),
                        "local_target": row.get("local_target")})
    return grouped


def _movie_targets(config, identities):
    return [get_record(config, "movie", value)["local_target"] for value in identities]


def _show_targets(config, identities):
    return [get_record(config, "show", value)["local_target"] for value in identities]


def _combined(*results, message="Finished"):
    counts = Counter()
    processed = 0
    phase_results = {}
    for prefix, result in results:
        processed += int(result.get("processed", 0))
        # Non-committing materializers can expose their normal/planned summary
        # alongside a small augmentation in ``counts``. Merge distinct keys,
        # letting the conventional ``counts`` value win when names overlap.
        phase_counts = {}
        for mapping_name in ("planned_counts", "status_counts", "counts"):
            mapping = result.get(mapping_name)
            if isinstance(mapping, dict):
                phase_counts.update(mapping)
        for name, value in phase_counts.items():
            counts[name if name.startswith(prefix + "_") else f"{prefix}_{name}"] += value
        if result.get("planned_statuses"):
            phase_results[prefix] = result["planned_statuses"]
    return {"processed": processed, "counts": dict(counts), "message": message,
            "phase_results": phase_results}


def _item_result(identities, *results, message="Finished"):
    """Aggregate phase detail without double-counting scoped identities."""
    combined = _combined(*results, message=message)
    combined["processed"] = len(identities)
    return combined


def _finish(config, workflow, identities, recorder, result, attention=0):
    # Scanner summaries may describe the entire durable provider manifest. Only
    # explicit failures from this item's own recipe are safe to classify here;
    # run_scoped resolves provider-record outcome after loading this row alone.
    state = "needs_attention" if attention or result.get("ok") is False else "ready"
    plan = persist_preparation(
        config, workflow, identities, recorder, state=state,
        display_title=result.pop("_display_title", None),
        local_target=result.pop("_local_target", None), summary={
            "counts": result.get("counts", {}), "message": result.get("message")},
        reason=result.get("message") if state == "needs_attention" else None,
        logical_identity=result.pop("_logical_identity", None),
        requested_artifacts={"missing-actor-images": ["actor_image"],
                             "lost-found": ["nfo"], "missing-posters": ["poster"],
                             "missing-tv-nfo": ["nfo"],
                             "missing-tv-posters": ["poster"]}.get(
                                 workflow, ["identity"]),
    )
    detail = result.get("message")
    detail = f"\n\n{detail}" if detail and detail != "Finished" else ""
    result["counts"].update({"prepared": plan["prepared"],
                             "needs_attention": attention, "applied": 0})
    result.update({"preparation": plan,
                   "message": (f"Prepared: {plan['prepared']}\nNeeds attention: {attention}\n"
                               "Applied: 0\n\nNothing has been written to the media library."
                               f"{detail}")})
    return result


def _scoped_record_outcome(workflow, identities, records):
    """Classify only provider records owned by one frozen logical row."""
    successful = {"actor": {"ok"}, "movie": {"ok"}, "show": {"matched"}}
    kind = "actor" if "actor" in workflow else "show" if workflow in {
        "ambiguous-tv", "not-found-tv", "tv-errors", "missing-tv-nfo",
        "missing-tv-posters"} else "movie"
    if identities and len(records) != len(identities):
        return True, "Scoped provider record is missing"
    for record in records:
        status = record.get("status")
        if status not in successful[kind]:
            reason = (record.get("error") or record.get("reason") or status or
                      "Provider result needs attention")
            return True, str(reason)
    return False, None


def _scoped_artifact_outcome(workflow, result):
    """Classify requested artifact preparation without consulting provider totals."""
    expected = {
        "missing-actor-images": ("image", ("failed", "unresolved_source")),
        "missing-posters": ("poster", ("error", "no_url", "unresolved_target")),
        "lost-found": ("nfo", ("error", "unresolved_target")),
        "missing-tv-nfo": ("nfo", ("error",)),
        "missing-tv-posters": ("poster", ("error", "no_url")),
    }.get(workflow)
    if expected is None:
        return False, None
    prefix, failures = expected
    diagnostics = result.get("counts", {})
    for failure in failures:
        key = f"{prefix}_{failure}"
        if diagnostics.get(key, 0):
            details = result.get("phase_results", {}).get(prefix, {})
            reason = next((value.get("error") or value.get("status")
                           for value in details.values()
                           if value.get("status") in failures), None)
            return True, str(reason or f"{key.replace('_', ' ')}: {diagnostics[key]}")
    successful = sum(int(diagnostics.get(f"{prefix}_{name}", 0))
                     for name in ("planned", "exists"))
    if not successful:
        return True, f"No {prefix.replace('_', ' ')} artifact was prepared"
    return False, None


def run(config, workflow, identities, reporter=None):
    """Run the allowlisted recipe while preserving pre-existing artifacts."""
    if workflow not in WORKFLOWS:
        raise ValueError("unknown Bulk workflow")
    recorder = RecordingCommitter()
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
                         targets=available, transport=transport, committer=recorder))
        fetched.setdefault("counts", {})["unresolved_source"] = unresolved
        return _finish(config, workflow, identities, recorder,
                       _item_result(identities, ("identity", scanned), ("image", fetched)),
                       unresolved)
    if workflow == "failed-actors":
        from .movie_actor_scan import run as scan
        from ..providers.tmdb import TMDBClient
        from ..transport import transport_from_config
        transport = transport_from_config(config)
        provider = TMDBClient(config.tmdb_api_key, config.tmdb_bearer_token,
                              config.state_path("tmdb_api_cache.json"), transport)
        return _finish(config, workflow, identities, recorder, _item_result(
            identities, ("identity", scan(config, provider, reporter, refresh=True,
                                            retry_failed=True, targets=identities))))
    if workflow in {"lost-found", "unresolved-movies", "failed-movies"}:
        from .movie_scan import run as scan
        from ..providers.tmdb import TMDBClient
        from ..transport import transport_from_config
        transport = transport_from_config(config)
        targets = _movie_targets(config, identities)
        provider = TMDBClient(config.tmdb_api_key, config.tmdb_bearer_token,
                              config.state_path("tmdb_api_cache.json"), transport)
        scanned = scan(config, provider, reporter, refresh=True, targets=targets)
        records = [get_record(config, "movie", value) for value in identities]
        should_prepare_nfo = workflow == "lost-found" or any(
            record.get("status") == "ok" and not Path(record["nfo_path"]).is_file()
            for record in records)
        if not should_prepare_nfo:
            return _finish(config, workflow, identities, recorder,
                           _item_result(identities, ("identity", scanned)))
        from .movie_materialize import run as materialize
        written = materialize(config, reporter, overwrite_nfo=False, overwrite_poster=False,
                              targets=targets, transport=transport, write_nfo=True,
                              write_poster=False, committer=recorder)
        return _finish(config, workflow, identities, recorder,
                       _item_result(identities, ("identity", scanned), ("nfo", written)))
    if workflow == "missing-posters":
        from .movie_materialize import run as materialize
        from ..transport import transport_from_config
        records = [get_record(config, "movie", value) for value in identities]
        targets = [record["local_target"] for record in records if record.get("poster_path")]
        unresolved = len(records) - len(targets)
        result = ({"processed": 0, "counts": {}} if not targets else
                  materialize(config, reporter, overwrite_nfo=False, overwrite_poster=False,
                              targets=targets, transport=transport_from_config(config),
                              write_nfo=False, write_poster=True, committer=recorder))
        result["processed"] = int(result.get("processed", 0)) + unresolved
        result.setdefault("counts", {})["poster_unresolved_target"] = unresolved
        message = (f"{unresolved} item(s) have no safe poster target" if unresolved
                   else "Finished")
        combined = _item_result(identities, ("poster", result), message=message)
        combined["ok"] = not unresolved
        return _finish(config, workflow, identities, recorder, combined, unresolved)
    from .tv_scan import run as scan
    from ..providers.tvdb import TVDBClient
    from ..transport import transport_from_config
    transport = transport_from_config(config)
    provider = TVDBClient(config.tvdb_api_key, config.tvdb_pin,
                          config.state_path("tvdb_api_cache.json"), transport)
    records = [get_record(config, "show", value) for value in identities]
    needs_resolution = [record for record in records if record.get("status") != "matched"]
    scanned = {"processed": 0, "counts": {}}
    if needs_resolution:
        scanned = scan(
        config, provider, reporter, refresh=True, retry_errors=True,
        retry_ambiguous=True, retry_not_found=True, targets=_show_targets(config, identities),
        )
    records = [get_record(config, "show", value) for value in identities]
    targets = [record["local_target"] for record in records if record.get("status") == "matched"]
    write_nfo = workflow == "missing-tv-nfo" or (
        workflow in {"ambiguous-tv", "not-found-tv", "tv-errors"} and
        any(not (Path(record["local_target"]) / "show.nfo").is_file() for record in records
            if record.get("status") == "matched"))
    write_poster = workflow == "missing-tv-posters"
    results = [("identity", scanned)]
    if targets and (write_nfo or write_poster):
        from .tv_materialize import run as materialize
        prepared = materialize(config, reporter, targets=targets, transport=transport,
                               write_nfo=write_nfo, write_poster=write_poster,
                               write_actors=False, overwrite_nfo=False,
                               overwrite_poster=False, committer=recorder)
        results.append(("nfo" if write_nfo else "poster", prepared))
    return _finish(config, workflow, identities, recorder,
                   _item_result(identities, *results))


def run_scoped(config, workflow, items, logical_count, reporter=None):
    """Run a UI scope while reporting collection rows as the terminal progress unit.

    Grouped presentation rows may deliberately expand to several manifest identities.
    Phase counters retain that identity-specific detail, but the top-level processed
    value must use the same logical-row unit shown by the UI's progress denominator.
    """
    if not isinstance(logical_count, int) or logical_count < 0:
        raise ValueError("invalid logical Bulk scope count")
    if items and not isinstance(items[0], dict):
        items = [{"identities": list(items), "display_title": str(items[0]),
                  "local_target": None}]
    counts = Counter()
    preparations = []
    all_identities = []
    ok = True
    for row_index, item in enumerate(items):
        identities = item["identities"]
        all_identities.extend(identities)
        # Empty/invalid rows are still durable review outcomes rather than
        # silently disappearing from the acquisition result.
        if not identities:
            recorder = RecordingCommitter()
            result = {"processed": 0, "counts": {}, "ok": False,
                      "message": "Frozen row has no usable manifest identity",
                      "_display_title": item.get("display_title"),
                      "_local_target": item.get("local_target"),
                      "_logical_identity": item.get("local_target") or item.get("display_title")}
            result = _finish(config, workflow, identities, recorder, result, 1)
        else:
            try:
                result = run(config, workflow, identities, reporter)
            except Exception as error:
                recorder = RecordingCommitter()
                result = {"processed": len(identities), "counts": {"producer_error": 1},
                          "ok": False, "message": str(error),
                          "_display_title": item.get("display_title"),
                          "_local_target": item.get("local_target")}
                result = _finish(config, workflow, identities, recorder, result, 1)
        plan = result.get("preparation") or {}
        if plan.get("plan_id"):
            manifest_path = (config.app_dir / ".cache" / "bulk" / "inbox" /
                             plan["plan_id"] / "manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["display_title"] = item.get("display_title") or manifest["display_title"]
            manifest["local_target"] = item.get("local_target") or manifest.get("local_target")
            manifest["summary"] = {"stage_diagnostics": result.get("counts", {}),
                                   "artifact_results": result.get("phase_results", {}),
                                   "message": result.get("message")}
            records = []
            kind = "actor" if "actor" in workflow else "show" if workflow in {
                "ambiguous-tv", "not-found-tv", "tv-errors", "missing-tv-nfo",
                "missing-tv-posters"} else "movie"
            for identity in identities:
                try:
                    records.append(get_record(config, kind, identity))
                except (KeyError, OSError, ValueError):
                    pass
            manifest["summary"]["provider_results"] = records
            if records:
                record = records[0]
                if kind == "actor":
                    inferred = {"name": identities[0]}
                else:
                    inferred = {
                        "title": (record.get("query_title") or record.get("title") or
                                  record.get("folder_name") or item.get("display_title")),
                        "year": record.get("query_year") if kind == "show" else record.get("year"),
                    }
                manifest.setdefault("query", {})["inferred"] = inferred
                override = record.get("query_override")
                if override:
                    manifest["query"]["override"] = override
                else:
                    manifest["query"].pop("override", None)
            record_attention, record_reason = _scoped_record_outcome(
                workflow, identities, records)
            artifact_attention, artifact_reason = _scoped_artifact_outcome(workflow, result)
            if artifact_attention and manifest["state"] == "ready":
                manifest["state"] = "needs_attention"
                manifest["reason"] = artifact_reason
            elif record_attention and manifest["state"] == "ready":
                manifest["state"] = "needs_attention"
                manifest["reason"] = record_reason
            result.setdefault("counts", {})["needs_attention"] = int(
                manifest["state"] == "needs_attention")
            save_json_atomic(manifest_path, manifest)
            emit(reporter, "inbox", manifest["display_title"], id=plan["plan_id"],
                 status=manifest["state"], target_kind="inbox",
                 row_index=row_index, logical_ids=identities,
                 label=manifest["display_title"])
        preparations.append(result.get("preparation"))
        counts.update(result.get("counts", {}))
        ok = ok and result.get("ok", True)
    counts["scoped_identities"] = len(all_identities)
    result = {"ok": ok, "processed": logical_count, "counts": dict(counts),
              "preparations": preparations,
              "message": f"Prepared {logical_count} item(s) for the Bulk Inbox"}
    result["processed"] = logical_count
    return result
