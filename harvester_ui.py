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

_cache_lock = threading.Lock()


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
        for option in ("status", "missing"):
            if data.get(option):
                suffix += ["--" + option, data[option]]
        return tuple(suffix)
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


ACTION_REGISTRY = {
    "providers": _no_args(("providers",)), "inventory": _no_args(("inventory",)),
    "list.movies": _list("movie"), "list.shows": _list("show"),
    "list.actors": _list("actor"), "get.movie": _identifier("movie"),
    "get.show": _identifier("show"), "get.actor": _identifier("actor"),
    "search": _search, "rescan": _rescan,
    "refresh.actor.image": _refresh_actor,
    "actor.install_image": None,
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
    suffix = builder(data)
    return [sys.executable, str(HARVESTER_PATH), "api", *suffix]


def install_actor_image(data):
    """Install one known actor image at its canonical destination."""
    if set(data) != {"identifier", "data_url"} or not all(isinstance(v, str) for v in data.values()):
        raise BridgeError("actor.install_image requires identifier and data_url")
    from harvester_core.api import get_record
    from harvester_core.config import load_config
    from harvester_core.images import safe_actor_filename
    from harvester_core.storage import write_bytes_atomic
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
    write_bytes_atomic(destination, source)
    return {"actor": actor["name"], "local_file": str(destination), "bytes": len(source)}


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


def publish_collection(action, data, result):
    """Publish large queue payloads outside Severin's bounded reply frame."""
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        raise BridgeError(f"{action} returned an invalid collection")
    identity = json.dumps(
        {"version": COLLECTION_CACHE_VERSION, "action": action, "data": data},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    cache_name = f"collection-v{COLLECTION_CACHE_VERSION}-{hashlib.sha256(identity).hexdigest()[:20]}.json"
    path = CACHE_DIR / cache_name
    generation = hashlib.sha256(json.dumps(
        result["items"], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()[:20]
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
    try:
        message = decode_message(json_text)
        message_id = message["id"]
        result = run_action(message["action"], message["data"])
        reply = make_reply(message_id, result=result)
    except Exception as error:
        reply = make_reply(message_id, error=error)
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
