"""Small, optional Severin host adapter for Harvester's machine API.

This module deliberately does not import Severin.  Keeping the bridge and cache
code importable without the headed runtime makes the CLI and its tests retain a
standard-library-only required path.
"""

import json
import base64
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
HARVESTER_PATH = PROJECT_DIR / "harvester.py"
PACKAGE_ID = "com.harvester.app"
CACHE_DIR = PROJECT_DIR / ".cache" / "ui"
COLLECTION_CACHE_VERSION = 1
CONFIG_FIELDS = {
    "tmdb_api_key": "TMDB_API_KEY", "tmdb_bearer_token": "TMDB_BEARER_TOKEN",
    "tvdb_api_key": "TVDB_API_KEY", "tvdb_pin": "TVDB_PIN",
    "socks5": "HARVESTER_SOCKS5", "socks5_username": "HARVESTER_SOCKS5_USERNAME",
    "socks5_password": "HARVESTER_SOCKS5_PASSWORD",
}

_cache_lock = threading.Lock()
_library_commit_lock = threading.Lock()
_process_lock = threading.Lock()
_bulk_processes = {}
_retrying_items = set()


class BridgeError(RuntimeError):
    """An invalid request or failed Harvester machine API operation."""


def _no_args(suffix):
    def build(data):
        if data:
            raise BridgeError("action does not accept arguments")
        return suffix
    return build


def _identifier(kind):
    def build(data):
        if set(data) != {"identifier"} or not isinstance(data["identifier"], str):
            raise BridgeError(f"get.{kind} requires one string identifier")
        value = data["identifier"]
        if not value or "\0" in value or value.startswith("-"):
            raise BridgeError("invalid record identifier")
        return ("get", kind, value)
    return build


def _list(kind):
    def build(data):
        allowed = {"status", "missing"}
        if not set(data) <= allowed or not all(isinstance(v, str) for v in data.values()):
            raise BridgeError(f"list.{kind}s accepts string status/missing filters only")
        suffix = ["list", kind + "s", "--brief"]
        if kind in ("movie", "show"):
            suffix.append("--artifacts")
        if kind == "movie" and data.get("missing") == "poster":
            suffix.append("--group-directories")
        for option in ("status", "missing"):
            if data.get(option):
                suffix += ["--" + option, data[option]]
        return tuple(suffix)
    return build


def _inspect(kind):
    def build(data):
        _identifier(kind)(data)
        return ("inspect", kind, data["identifier"])
    return build


def _search(data):
    if set(data) != {"query"} or not isinstance(data["query"], str):
        raise BridgeError("search requires one string query")
    return ("search", data["query"], "--limit", "50")


def _refresh_actor(data):
    if set(data) != {"identifier"} or not isinstance(data["identifier"], str):
        raise BridgeError("refresh.actor.image requires one string identifier")
    _identifier("actor")({"identifier": data["identifier"]})
    return ("refresh", "actor", data["identifier"], "--aspect", "image")


def _rescan(data):
    if data:
        raise BridgeError("rescan does not accept arguments")
    return ("rescan",)


BULK_WORKFLOWS = frozenset({
    "missing-actor-images", "failed-actors", "lost-found", "missing-posters",
    "unresolved-movies", "failed-movies", "ambiguous-tv", "not-found-tv", "tv-errors",
    "missing-tv-nfo", "missing-tv-posters",
})


def _bulk_workflow(data):
    """Accept only a frozen set of identities for a known workflow operation."""
    if set(data) != {"workflow", "scope"} or data.get("workflow") not in BULK_WORKFLOWS:
        raise BridgeError("bulk.workflow requires a known workflow and frozen scope")
    scope = data["scope"]
    if not isinstance(scope, dict) or set(scope) != {"asset", "count", "generation", "version"}:
        raise BridgeError("bulk.workflow requires a collection scope descriptor")
    prefix = f"asset://{PACKAGE_ID}/.cache/ui/"
    asset = scope.get("asset")
    suffix = asset[len(prefix):] if isinstance(asset, str) and asset.startswith(prefix) else ""
    if (not suffix.startswith(f"collection-v{COLLECTION_CACHE_VERSION}-") or "/" in suffix or
            scope.get("version") != COLLECTION_CACHE_VERSION or
            not isinstance(scope.get("count"), int) or not 1 <= scope["count"] <= 1_000_000 or
            not isinstance(scope.get("generation"), str)):
        raise BridgeError("bulk.workflow received an invalid frozen scope")
    return ("bulk", data["workflow"], "--scope-file", str(CACHE_DIR / suffix),
            "--generation", scope["generation"], "--count", str(scope["count"]))


def _bulk_item(data):
    """Freeze exactly one existing logical collection row for the Bulk recipe."""
    if (set(data) != {"workflow", "scope", "index"} or
            not isinstance(data.get("index"), int) or isinstance(data.get("index"), bool) or
            data["index"] < 0):
        raise BridgeError("bulk.item requires a known workflow, frozen scope, and row index")
    _bulk_workflow({"workflow": data.get("workflow"), "scope": data.get("scope")})
    source = _scope_path(data["scope"])
    try:
        collection = json.loads(source.read_text(encoding="utf-8"))
        items = collection["items"]
        actual_generation = hashlib.sha256(json.dumps(
            items, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")).hexdigest()[:20]
        if (collection.get("version") != COLLECTION_CACHE_VERSION or
                collection.get("generation") != data["scope"]["generation"] or
                len(items) != data["scope"]["count"] or
                actual_generation != data["scope"]["generation"]):
            raise BridgeError("bulk.item source no longer matches its descriptor")
        row = collection["items"][data["index"]]
    except BridgeError:
        raise
    except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
        raise BridgeError("bulk.item could not validate the selected logical row") from error
    payload = {"version": COLLECTION_CACHE_VERSION, "items": [row]}
    generation = hashlib.sha256(json.dumps(
        payload["items"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()[:20]
    payload["generation"] = generation
    name = f"collection-v{COLLECTION_CACHE_VERSION}-item-{generation}.json"
    with _cache_lock:
        _prepare_cache_directory()
        atomic_write_json(CACHE_DIR / name, payload)
    return ("bulk", data["workflow"], "--scope-file", str(CACHE_DIR / name),
            "--generation", generation, "--count", "1")


def _item_refetch(data):
    """Derive a preparation recipe from one durable record identity."""
    if (set(data) != {"kind", "identifier"} or data.get("kind") not in
            {"actor", "movie", "show"} or not isinstance(data.get("identifier"), str)):
        raise BridgeError("item.refetch requires kind and trusted durable identifier")
    _identifier(data["kind"])({"identifier": data["identifier"]})
    from harvester_core.api import get_record
    from harvester_core.config import load_config
    config = load_config(app_dir=PROJECT_DIR)
    record = get_record(config, data["kind"], data["identifier"])
    identity = record.get("name") if data["kind"] == "actor" else (
        record.get("nfo_path") or record.get("local_target") if data["kind"] == "movie"
        else record.get("local_target"))
    workflow = {"actor": "missing-actor-images", "movie": "unresolved-movies",
                "show": "tv-errors"}[data["kind"]]
    row = {"identifier": identity, "display_name": data["identifier"],
           "local_target": record.get("local_target")}
    generation = hashlib.sha256(json.dumps(
        [row], ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()[:20]
    name = f"collection-v{COLLECTION_CACHE_VERSION}-item-{generation}.json"
    with _cache_lock:
        _prepare_cache_directory()
        atomic_write_json(CACHE_DIR / name, {"version": COLLECTION_CACHE_VERSION,
                                            "generation": generation, "items": [row]})
    return ("bulk", workflow, "--scope-file", str(CACHE_DIR / name),
            "--generation", generation, "--count", "1")


def _scope_path(scope):
    prefix = f"asset://{PACKAGE_ID}/.cache/ui/"
    asset = scope.get("asset") if isinstance(scope, dict) else None
    suffix = asset[len(prefix):] if isinstance(asset, str) and asset.startswith(prefix) else ""
    if not suffix or "/" in suffix:
        raise BridgeError("invalid frozen scope asset")
    return CACHE_DIR / suffix


def _inbox_retry(data):
    """Re-run the stored semantic recipe; accept no workflow or path from JS."""
    if set(data) != {"item_id", "query"} or not isinstance(data.get("query"), dict):
        raise BridgeError("inbox.retry requires one trusted item id and provider query")
    from harvester_core.artifacts import get_inbox_item
    from harvester_core.config import load_config
    config = load_config(app_dir=PROJECT_DIR)
    item = get_inbox_item(config, data["item_id"])
    kind = "actor" if "actor" in item["workflow"] else "show" if item["workflow"] in {
        "ambiguous-tv", "not-found-tv", "tv-errors", "missing-tv-nfo",
        "missing-tv-posters"} else "movie"
    query = data["query"]
    valid_keys = (set(query) == {"name"} if kind == "actor" else
                  "title" in query and set(query) <= {"title", "year"})
    if (not valid_keys or not all(value is None or isinstance(value, str)
                                  for value in query.values())):
        raise BridgeError("inbox.retry received an invalid provider query")
    text_key = "name" if kind == "actor" else "title"
    if not isinstance(query.get(text_key), str) or len(query[text_key]) > 300 or "\0" in query[text_key]:
        raise BridgeError("provider query is invalid")
    year = query.get("year")
    if kind != "actor" and year not in (None, "") and (not year.isdigit() or len(year) != 4):
        raise BridgeError("provider query year must be four digits")
    override = ({text_key: query[text_key].strip()} if query[text_key].strip() else {})
    if kind != "actor" and (override or year not in (None, "")):
        override["year"] = int(year) if year not in (None, "") else None
    return _prepare_inbox_rerun(config, item, kind, override)


def _prepare_inbox_rerun(config, item, kind, override):
    """Persist a human provider override and rebuild the same scoped recipe."""
    with _library_commit_lock:
        _retrying_items.add(item["item_id"])
    from harvester_core.storage import load_json, save_json_atomic
    filename, collection = {"actor": ("movie_actor_queue.json", "actors"),
                            "movie": ("movie_manifest_tmdb.json", "movies"),
                            "show": ("tv_show_urls_tvdb.json", "shows")}[kind]
    state_path = config.state_path(filename)
    state = load_json(state_path, {})
    records = state.get(collection, {})
    for identity in item["identities"]:
        keys = [identity] if identity in records else [
            key for key in records if kind == "actor" and key.casefold() == identity.casefold()]
        for key in keys:
            if override:
                records[key]["query_override"] = override
            else:
                records[key].pop("query_override", None)
    save_json_atomic(state_path, state)
    manifest_path = config.app_dir / ".cache" / "bulk" / "inbox" / item["item_id"] / "manifest.json"
    item.setdefault("query", {})["override"] = override or None
    save_json_atomic(manifest_path, item)
    row = {"grouped": len(item["identities"]) > 1,
           "manifest_identities": item["identities"],
           "identifier": item["identities"][0] if item["identities"] else None,
           "display_name": item["display_title"], "local_target": item.get("local_target")}
    generation = hashlib.sha256(json.dumps(
        [row], ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()[:20]
    name = f"collection-v{COLLECTION_CACHE_VERSION}-retry-{generation}.json"
    with _cache_lock:
        _prepare_cache_directory()
        atomic_write_json(CACHE_DIR / name, {"version": COLLECTION_CACHE_VERSION,
                                            "generation": generation, "items": [row]})
    return ("bulk", item["workflow"], "--scope-file", str(CACHE_DIR / name),
            "--generation", generation, "--count", "1")


def _inbox_candidate(data):
    """Select only by a trusted item's frozen candidate index."""
    if (set(data) != {"item_id", "candidate_index"} or
            not isinstance(data.get("candidate_index"), int) or
            isinstance(data.get("candidate_index"), bool) or data["candidate_index"] < 0):
        raise BridgeError("inbox.select_candidate requires item_id and candidate_index")
    from harvester_core.artifacts import get_inbox_item
    from harvester_core.config import load_config
    config = load_config(app_dir=PROJECT_DIR)
    item = get_inbox_item(config, data["item_id"])
    movie_workflows = {"lost-found", "unresolved-movies", "failed-movies"}
    tv_workflows = {"ambiguous-tv", "not-found-tv", "tv-errors"}
    if item["workflow"] not in movie_workflows | tv_workflows:
        raise BridgeError("candidate selection is unavailable for this Inbox workflow")
    try:
        candidate = item["summary"]["provider_results"][0]["candidates"][data["candidate_index"]]
    except (KeyError, IndexError, TypeError) as error:
        raise BridgeError("candidate index is not present in the frozen Inbox item") from error
    identity_key = "tmdb_id" if item["workflow"] in movie_workflows else "tvdb_id"
    provider_id = candidate.get("id" if identity_key == "tmdb_id" else "tvdb_id")
    if not isinstance(provider_id, int) or isinstance(provider_id, bool) or provider_id <= 0:
        raise BridgeError(f"frozen candidate has no usable {identity_key[:-3].upper()} identity")
    kind = "movie" if identity_key == "tmdb_id" else "show"
    return _prepare_inbox_rerun(config, item, kind, {identity_key: provider_id})


ACTION_REGISTRY = {
    "providers": _no_args(("providers",)), "inventory": _no_args(("inventory",)),
    "list.movies": _list("movie"), "list.shows": _list("show"),
    "list.actors": _list("actor"), "get.movie": _identifier("movie"),
    "get.show": _identifier("show"), "get.actor": _identifier("actor"),
    "inspect.movie": _inspect("movie"), "inspect.show": _inspect("show"),
    "search": _search, "rescan": _rescan,
    "refresh.actor.image": _refresh_actor,
    "bulk.workflow": _bulk_workflow,
    "bulk.item": _bulk_item,
    "item.refetch": _item_refetch,
    "bulk.stop": None,
    "inbox.retry": _inbox_retry,
    "inbox.select_candidate": _inbox_candidate,
    "config.get": None, "config.save": None,
    "inbox.list": None, "inbox.get": None, "inbox.apply": None,
    "inbox.discard": None, "inbox.apply_all": None, "inbox.discard_all": None,
    "actor.install_image": None, "inbox.install_image": None,
    "item.install_image": None,
    "preview.artifact": None,
}
BRIDGE_ACTIONS = frozenset({"__ping__", *ACTION_REGISTRY})


def decode_message(json_text):
    if not isinstance(json_text, str):
        raise BridgeError("Severin bridge frame was not JSON text")
    try:
        message = json.loads(json_text)
        # Severin versions have historically differed on whether bridge values
        # arrive as JSON text or JSON containing that text.
        if isinstance(message, str):
            message = json.loads(message)
    except (TypeError, ValueError) as error:
        raise BridgeError(f"malformed bridge JSON: {error}") from error
    if not isinstance(message, dict):
        raise BridgeError("bridge message must be a JSON object")
    if "id" not in message or not isinstance(message.get("action"), str):
        raise BridgeError("bridge message requires id and string action")
    data = message.get("data", {})
    if not isinstance(data, dict):
        raise BridgeError("bridge data must be a JSON object")
    message["data"] = data
    return message


def action_argv(action, data):
    """Translate a semantic capability into a fixed Harvester argv shape."""
    if action not in ACTION_REGISTRY or ACTION_REGISTRY[action] is None:
        raise BridgeError(f"unknown bridge action: {action}")
    builder = ACTION_REGISTRY[action]
    try:
        suffix = builder(data)
    except BaseException:
        if action == "inbox.retry":
            with _library_commit_lock:
                _retrying_items.discard(data.get("item_id"))
        raise
    return [sys.executable, str(HARVESTER_PATH), "api", *suffix]


def install_actor_image(data):
    """Install one known actor image at its canonical destination."""
    if set(data) != {"identifier", "data_url"} or not all(isinstance(v, str) for v in data.values()):
        raise BridgeError("actor.install_image requires identifier and data_url")
    from harvester_core.api import get_record
    from harvester_core.config import load_config
    from harvester_core.images import safe_actor_filename
    from harvester_core.storage import write_library_bytes_atomic
    config = load_config()
    actor = get_record(config, "actor", data["identifier"])
    try:
        header, encoded = data["data_url"].split(",", 1)
        mime = header[5:].split(";", 1)[0].lower()
        if ";base64" not in header or len(encoded) > 700_000:
            raise ValueError
        source = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise BridgeError("invalid or oversized image data") from error
    if not source or len(source) > 512_000:
        raise BridgeError("canonical actor JPEG must be between 1 byte and 512 KB")
    if mime not in ("image/jpeg", "image/jpg") or not source.startswith(b"\xff\xd8\xff"):
        raise BridgeError("actor.install_image accepts a canonical JPEG only")
    destination = config.movie_root / ".actors" / safe_actor_filename(actor["name"])
    write_library_bytes_atomic(destination, source)
    return {"actor": actor["name"], "local_file": str(destination), "bytes": len(source)}


def install_inbox_image(data):
    """Replace a prepared artwork blob; never write through to the library."""
    if set(data) != {"item_id", "data_url"} or not all(isinstance(v, str) for v in data.values()):
        raise BridgeError("inbox.install_image requires item_id and data_url")
    from harvester_core.api import get_record
    from harvester_core.artifacts import get_inbox_item
    from harvester_core.config import load_config
    from harvester_core.images import safe_actor_filename
    from harvester_core.storage import save_json_atomic, write_bytes_atomic
    config = load_config(app_dir=PROJECT_DIR)
    item = get_inbox_item(config, data["item_id"])
    try:
        header, encoded = data["data_url"].split(",", 1)
        source = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise BridgeError("invalid image data") from error
    if ";base64" not in header or len(source) > 512_000 or not source.startswith(b"\xff\xd8\xff"):
        raise BridgeError("manual Inbox image must be a canonical JPEG up to 512 KB")
    if item["workflow"] in {"missing-actor-images", "failed-actors"} and len(item["identities"]) == 1:
        target = config.movie_root / ".actors" / safe_actor_filename(item["identities"][0])
    elif item["workflow"] == "missing-posters" and len(item["identities"]) == 1:
        record = get_record(config, "movie", item["identities"][0])
        if not record.get("poster_path"):
            raise BridgeError("movie poster ownership is unresolved")
        target = Path(record["poster_path"]).with_suffix(".jpg")
    else:
        raise BridgeError("manual image is unavailable for this Inbox item")
    root = config.app_dir / ".cache" / "bulk" / "inbox" / item["item_id"]
    digest = hashlib.sha256(source).hexdigest()
    write_bytes_atomic(root / "blobs" / digest, source)
    from harvester_core.artifacts import _precondition
    action = {"action": "write", "path": str(target), "precondition": _precondition(target),
              "blob": f"blobs/{digest}", "size": len(source), "sha256": digest}
    item["actions"] = [a for a in item["actions"] if not (a.get("action") == "write" and
                       Path(a.get("path", "")).suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp"})]
    item["actions"].append(action)
    item.update({"state": "ready", "reason": None})
    save_json_atomic(root / "manifest.json", item)
    return {"item_id": item["item_id"], "prepared": 1, "bytes": len(source)}


def prepare_item_image(data):
    """Prepare a canonical Search-item image without accepting a destination."""
    if (set(data) != {"kind", "identifier", "data_url"} or data.get("kind") not in
            {"actor", "movie", "show"} or not all(
                isinstance(data.get(key), str) for key in ("identifier", "data_url"))):
        raise BridgeError("item.install_image requires kind, identifier, and image data")
    try:
        header, encoded = data["data_url"].split(",", 1)
        source = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as error:
        raise BridgeError("invalid image data") from error
    if (";base64" not in header or len(source) > 512_000 or
            not source.startswith(b"\xff\xd8\xff")):
        raise BridgeError("manual item image must be a canonical JPEG up to 512 KB")
    from harvester_core.api import get_record, inspect_item
    from harvester_core.artifacts import (RecordingCommitter, get_inbox_item, list_inbox,
                                          persist_preparation)
    from harvester_core.config import load_config
    from harvester_core.images import safe_actor_filename
    from harvester_core.storage import save_json_atomic, write_bytes_atomic
    config = load_config(app_dir=PROJECT_DIR)
    kind = data["kind"]
    record = get_record(config, kind, data["identifier"])
    if kind == "actor":
        identity = record["name"]
        target = config.movie_root / ".actors" / safe_actor_filename(identity)
        workflow = "missing-actor-images"
    else:
        detail = inspect_item(config, kind, data["identifier"])
        if detail.get("ownership", {}).get("status") == "ambiguous":
            raise BridgeError("manual poster ownership is ambiguous")
        identity = detail["selected_manifest_identity"]
        target = Path(detail["directory"]) / "poster.jpg"
        workflow = "unresolved-movies" if kind == "movie" else "tv-errors"
    previous = next((item for item in list_inbox(config)
                     if item.get("workflow") == workflow and item.get("identities") == [identity]), None)
    plan = persist_preparation(config, workflow, [identity], RecordingCommitter(),
                               display_title=data["identifier"],
                               local_target=record.get("local_target"),
                               summary={"outcome": "ready", "message": "Manual image prepared"},
                               requested_artifacts=["actor_image" if kind == "actor" else "poster"])
    item = get_inbox_item(config, plan["plan_id"])
    if previous:
        item["actions"] = previous.get("actions", [])
        item["summary"] = previous.get("summary", item["summary"])
    root = config.app_dir / ".cache" / "bulk" / "inbox" / item["item_id"]
    digest = hashlib.sha256(source).hexdigest()
    write_bytes_atomic(root / "blobs" / digest, source)
    from harvester_core.artifacts import _precondition
    action = {"action": "write", "path": str(target), "precondition": _precondition(target),
              "blob": f"blobs/{digest}", "size": len(source), "sha256": digest}
    item["actions"] = [existing for existing in item["actions"] if not (
        existing.get("action") == "write" and Path(existing.get("path", "")) == target)]
    item["actions"].append(action)
    item.update({"state": "ready", "reason": None})
    item.setdefault("summary", {})["outcome"] = "ready"
    save_json_atomic(root / "manifest.json", item)
    return {"item_id": item["item_id"], "prepared": 1, "bytes": len(source)}


def publish_artifact_preview(data):
    """Publish one already-known library image as disposable UI state."""
    if (set(data) != {"kind", "identifier"} or
            data.get("kind") not in {"actor", "movie", "show"} or
            not isinstance(data.get("identifier"), str)):
        raise BridgeError("preview.artifact requires a semantic kind and identifier")
    from harvester_core.api import get_record, inspect_item
    from harvester_core.config import load_config
    config = load_config(app_dir=PROJECT_DIR)
    if data["kind"] == "actor":
        source_value = get_record(config, "actor", data["identifier"]).get("local_file")
    else:
        source_value = inspect_item(config, data["kind"], data["identifier"])["poster"].get("path")
    source = Path(source_value) if source_value else None
    if (source is None or not source.is_file() or source.is_symlink() or
            source.suffix.casefold() not in {".jpg", ".jpeg", ".png", ".webp"}):
        return {"available": False}
    size = source.stat().st_size
    if size > 20 * 1024 * 1024:
        return {"available": False}
    content = source.read_bytes()
    identity = hashlib.sha256((data["kind"] + "\0" + data["identifier"]).encode() + content).hexdigest()[:24]
    name = f"preview-{identity}{source.suffix.casefold()}"
    with _cache_lock:
        _prepare_cache_directory()
        from harvester_core.storage import write_bytes_atomic
        write_bytes_atomic(CACHE_DIR / name, content)
    return {"available": True, "asset": f"asset://{PACKAGE_ID}/.cache/ui/{name}"}


def parse_ndjson(output):
    """Return the single terminal result while ignoring progress events."""
    terminal = None
    for number, line in enumerate(output.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as error:
            raise BridgeError(f"malformed Harvester NDJSON on line {number}") from error
        if not isinstance(record, dict) or record.get("type") not in ("event", "result", "error"):
            raise BridgeError(f"invalid Harvester NDJSON record on line {number}")
        if record["type"] == "event":
            continue
        if terminal is not None:
            raise BridgeError("Harvester returned more than one terminal record")
        terminal = record
    if terminal is None:
        raise BridgeError("Harvester returned no terminal result")
    if terminal["type"] == "error" or not terminal.get("ok", False):
        raise BridgeError(str(terminal.get("error") or "Harvester API request failed"))
    if terminal["type"] != "result" or "result" not in terminal:
        raise BridgeError("Harvester returned an invalid result record")
    return terminal["result"]


def run_action(action, data):
    if action == "__ping__":
        if data:
            raise BridgeError("__ping__ does not accept arguments")
        return {"package_id": PACKAGE_ID}
    if action not in ACTION_REGISTRY:
        raise BridgeError(f"unknown bridge action: {action}")
    if action == "actor.install_image":
        return install_actor_image(data)
    if action == "inbox.install_image":
        return install_inbox_image(data)
    if action == "item.install_image":
        return prepare_item_image(data)
    if action == "preview.artifact":
        return publish_artifact_preview(data)
    if action == "config.get":
        return get_configuration(data)
    if action == "config.save":
        return save_configuration(data)
    if action.startswith("inbox."):
        return run_inbox_action(action, data)
    argv = action_argv(action, data)
    completed = subprocess.run(
        argv, cwd=PROJECT_DIR, text=True, capture_output=True, check=False,
    )
    try:
        result = parse_ndjson(completed.stdout)
    except BridgeError:
        raise
    if completed.returncode:
        raise BridgeError(f"Harvester API exited with status {completed.returncode}")
    if action.startswith("list.") or action == "search":
        return publish_collection(action, data, result)
    return result


def run_streaming_action(action, data, on_event, process_key=None):
    """Consume Bulk stdout incrementally while preserving its strict terminal contract."""
    argv = action_argv(action, data)
    try:
        process = subprocess.Popen(argv, cwd=PROJECT_DIR, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, bufsize=1)
    except BaseException:
        if action == "inbox.retry":
            with _library_commit_lock:
                _retrying_items.discard(data.get("item_id"))
        raise
    if process_key is not None:
        with _process_lock:
            _bulk_processes[process_key] = process
    def drain_stderr():
        # stderr is intentionally discarded after being drained: errors must
        # arrive through the strict NDJSON terminal record, never an unbounded
        # diagnostic buffer or a second response contract.
        for _line in process.stderr:
            pass
    drain = threading.Thread(target=drain_stderr, daemon=True, name="harvester-bulk-stderr")
    drain.start()
    terminal = None
    try:
        for number, line in enumerate(process.stdout, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as error:
                raise BridgeError(f"malformed Harvester NDJSON on line {number}") from error
            if not isinstance(record, dict) or record.get("type") not in ("event", "result", "error"):
                raise BridgeError(f"invalid Harvester NDJSON record on line {number}")
            if record["type"] == "event":
                on_event(record)
                continue
            if terminal is not None:
                raise BridgeError("Harvester returned more than one terminal record")
            terminal = record
        returncode = process.wait()
        drain.join()
        if terminal is None:
            if returncode < 0:
                raise BridgeError("Acquisition stopped; prepared Bulk Inbox items were kept")
            raise BridgeError("Harvester returned no terminal result")
        if terminal["type"] == "error" or not terminal.get("ok", False):
            raise BridgeError(str(terminal.get("error") or "Harvester API request failed"))
        if terminal["type"] != "result" or "result" not in terminal:
            raise BridgeError("Harvester returned an invalid result record")
        if returncode:
            raise BridgeError(f"Harvester API exited with status {returncode}")
        return terminal["result"]
    finally:
        if action == "inbox.retry":
            with _library_commit_lock:
                _retrying_items.discard(data.get("item_id"))
        if process_key is not None:
            with _process_lock:
                _bulk_processes.pop(process_key, None)
        if process.poll() is None:
            process.kill()
        drain.join(timeout=1)
        process.stdout.close()
        process.stderr.close()


def get_configuration(data):
    if data:
        raise BridgeError("config.get does not accept arguments")
    from harvester_core.config import load_config, parse_key_file
    path = PROJECT_DIR / "keys_and_tokens.txt"
    stored = parse_key_file(path)
    effective = load_config(app_dir=PROJECT_DIR)
    values, overridden = {}, {}
    for field, key in CONFIG_FIELDS.items():
        values[field] = stored.get(key, "")
        runtime = getattr(effective, field) or ""
        overridden[field] = key in os.environ and runtime != values[field]
    return {"values": values, "environment_overrides": overridden}


def save_configuration(data):
    if set(data) != {"values"} or not isinstance(data["values"], dict):
        raise BridgeError("config.save requires configuration values")
    values = data["values"]
    if set(values) != set(CONFIG_FIELDS) or not all(isinstance(v, str) for v in values.values()):
        raise BridgeError("config.save received invalid configuration fields")
    if any("\n" in value or "\r" in value or "\0" in value for value in values.values()):
        raise BridgeError("configuration values must fit on one line")
    path = PROJECT_DIR / "keys_and_tokens.txt"
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        existed = True
    except FileNotFoundError:
        lines, existed = [], False
    pending = {key: values[field] for field, key in CONFIG_FIELDS.items()}
    output = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else None
        if key in pending:
            ending = "\n" if line.endswith("\n") else ""
            output.append(f"{key}={pending.pop(key)}{ending}")
        else:
            output.append(line)
    if output and not output[-1].endswith("\n"):
        output[-1] += "\n"
    output.extend(f"{key}={value}\n" for key, value in pending.items())
    descriptor, temporary = tempfile.mkstemp(prefix=".keys_and_tokens.", dir=PROJECT_DIR)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.writelines(output); target.flush(); os.fsync(target.fileno())
        os.replace(temporary, path)
        if not existed:
            os.chmod(path, 0o600)
    except BaseException:
        try: os.unlink(temporary)
        except OSError: pass
        raise
    return get_configuration({})


def run_inbox_action(action, data):
    """Expose only trusted item IDs; renderer-supplied filesystem plans are forbidden."""
    from harvester_core.artifacts import (apply_inbox_item, discard_inbox_item,
                                          get_inbox_item, list_inbox)
    from harvester_core.config import load_config
    config = load_config(app_dir=PROJECT_DIR)
    if action == "inbox.list":
        if data:
            raise BridgeError("inbox.list does not accept arguments")
        return {"items": [{key: item.get(key) for key in
                            ("item_id", "workflow", "identities", "display_title",
                             "state", "seen", "reason")}
                           for item in list_inbox(config)]}
    if action in ("inbox.apply_all", "inbox.discard_all"):
        if data:
            raise BridgeError(f"{action} does not accept arguments")
        items = list_inbox(config)
        selected = [item for item in items if action == "inbox.discard_all" or
                    item["state"] == "ready"]
        operation = apply_inbox_item if action == "inbox.apply_all" else discard_inbox_item
        counts = {"applied": 0, "discarded": 0, "needs_attention": 0, "failed": 0}
        with _library_commit_lock:
            for item in selected:
                if item["item_id"] in _retrying_items:
                    counts["failed"] += 1
                    continue
                try:
                    outcome = operation(config, item["item_id"])
                    counts["applied" if outcome.get("applied") else "discarded"] += 1
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    try:
                        current = get_inbox_item(config, item["item_id"])
                    except (OSError, ValueError, KeyError, json.JSONDecodeError):
                        counts["failed"] += 1
                    else:
                        counts["needs_attention" if current.get("state") ==
                               "needs_attention" else "failed"] += 1
        counts["processed"] = sum(counts.values())
        return counts
    if set(data) != {"item_id"}:
        raise BridgeError(f"{action} requires one trusted item id")
    item_id = data["item_id"]
    if action == "inbox.get":
        return get_inbox_item(config, item_id, mark_seen=True)
    operation = apply_inbox_item if action == "inbox.apply" else discard_inbox_item
    with _library_commit_lock:
        if item_id in _retrying_items:
            raise BridgeError("Bulk Inbox item is currently being retried")
        return operation(config, item_id)


def publish_collection(action, data, result):
    """Publish large queue payloads outside Severin's bounded reply frame."""
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise BridgeError(f"{action} returned an invalid collection")
    identity = json.dumps(
        {"version": COLLECTION_CACHE_VERSION, "action": action, "data": data},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    generation = hashlib.sha256(json.dumps(
        result["items"], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()[:20]
    cache_name = (f"collection-v{COLLECTION_CACHE_VERSION}-"
                  f"{hashlib.sha256(identity).hexdigest()[:20]}-{generation}.json")
    path = CACHE_DIR / cache_name
    payload = {"version": COLLECTION_CACHE_VERSION, "generation": generation,
               "items": result["items"]}
    with _cache_lock:
        _prepare_cache_directory()
        atomic_write_json(path, payload)
    return {
        "asset": f"asset://{PACKAGE_ID}/.cache/ui/{cache_name}",
        "count": len(result["items"]),
        "generation": generation,
        "version": COLLECTION_CACHE_VERSION,
    }


def atomic_write_json(path, value):
    """Write derived presentation data without exposing a partial JSON file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(value, target, ensure_ascii=False, separators=(",", ":"))
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _prepare_cache_directory():
    """Create the fixed package cache without following a replaced cache node."""
    current = PROJECT_DIR
    for component in (".cache", "ui"):
        current = current / component
        if current.is_symlink():
            raise OSError(f"refusing symlinked UI cache path: {current}")
        current.mkdir(mode=0o700, exist_ok=True)
    if CACHE_DIR.resolve() != current.resolve() or PROJECT_DIR.resolve() not in CACHE_DIR.resolve().parents:
        raise OSError("UI cache path is outside the package")


def make_reply(message_id, *, result=None, error=None):
    reply = {"id": message_id, "ok": error is None}
    reply["result" if error is None else "error"] = result if error is None else str(error)
    return json.dumps(reply, ensure_ascii=False, separators=(",", ":"), default=str)


def _run_bridge_job(app, receipt, json_text):
    message_id = None
    event_path = None
    events = []
    try:
        message = decode_message(json_text)
        message_id = message["id"]
        if message["action"] == "bulk.stop":
            session = message.get("session")
            request_id = message["data"].get("request_id")
            if (not isinstance(session, str) or not isinstance(request_id, int) or
                    set(message["data"]) != {"request_id"}):
                raise BridgeError("bulk.stop requires the current acquisition id")
            with _process_lock:
                process = _bulk_processes.get((session, request_id))
                if process is not None:
                    process.terminate()
            result = {"stopped": process is not None}
        elif message["action"] in ("bulk.workflow", "bulk.item", "item.refetch", "inbox.retry",
                                  "inbox.select_candidate"):
            if (not isinstance(message_id, int) or isinstance(message_id, bool) or
                    not 1 <= message_id <= 2**53 - 1):
                raise BridgeError("Bulk request id must be a positive integer")
            session = message.get("session")
            if (not isinstance(session, str) or len(session) != 32 or
                    any(character not in "0123456789abcdef" for character in session)):
                raise BridgeError("Bulk request requires a valid UI session id")
            event_path = CACHE_DIR / f"events-{session}-{message_id}.json"
            with _cache_lock:
                _prepare_cache_directory()
                atomic_write_json(event_path, {"events": events, "complete": False})
            def publish_event(event):
                events.append(event)
                with _cache_lock:
                    atomic_write_json(event_path, {"events": events, "complete": False})
            result = run_streaming_action(message["action"], message["data"], publish_event,
                                          (session, message_id))
        else:
            result = run_action(message["action"], message["data"])
        reply = make_reply(message_id, result=result)
    except Exception as error:
        reply = make_reply(message_id, error=error)
    finally:
        if event_path is not None:
            try:
                with _cache_lock:
                    atomic_write_json(event_path, {"events": events, "complete": True})
            except Exception as error:
                print(f"Harvester UI event channel completion failed: {error}", file=sys.stderr)
    try:
        app.write(receipt, reply)
    except Exception as error:
        print(f"Harvester UI bridge reply failed: {error}", file=sys.stderr)


def make_bridge_callback(app_box):
    def bridge(receipt, json_text):
        app = app_box.get("app")
        if app is None:
            return make_reply(None, error="Harvester UI host is not ready")
        threading.Thread(
            target=_run_bridge_job, args=(app, receipt, json_text), daemon=True,
            name="harvester-ui-bridge-job",
        ).start()
        return None
    return bridge
