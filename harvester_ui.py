"""Small, optional Severin host adapter for Harvester's machine API.

This module deliberately does not import Severin.  Keeping the bridge and cache
code importable without the headed runtime makes the CLI and its tests retain a
standard-library-only required path.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
HARVESTER_PATH = PROJECT_DIR / "harvester.py"
PACKAGE_ID = "com.harvester.app"
CACHE_VERSION = 1
CACHE_DIR = PROJECT_DIR / ".cache" / "ui"
SNAPSHOT_PATH = CACHE_DIR / "snapshot.json"

_cache_lock = threading.Lock()


class BridgeError(RuntimeError):
    """An invalid request or failed Harvester machine API operation."""


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
    fixed = {
        "providers": ("providers",),
        "inventory": ("inventory",),
        "list.movies": ("list", "movies"),
        "list.shows": ("list", "shows"),
        "list.actors": ("list", "actors"),
    }
    if action in fixed:
        if data:
            raise BridgeError(f"{action} does not accept arguments")
        suffix = fixed[action]
    elif action in ("get.movie", "get.show", "get.actor"):
        if set(data) != {"identifier"} or not isinstance(data["identifier"], str):
            raise BridgeError(f"{action} requires one string identifier")
        identifier = data["identifier"]
        if not identifier or "\0" in identifier or identifier.startswith("-"):
            raise BridgeError("invalid record identifier")
        suffix = ("get", action.removeprefix("get."), identifier)
    else:
        raise BridgeError(f"unknown bridge action: {action}")
    return [sys.executable, str(HARVESTER_PATH), "api", *suffix]


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
    cache_result(action, result)
    return result


def read_snapshot(path=SNAPSHOT_PATH):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("version") != CACHE_VERSION:
        return None
    if not isinstance(value.get("views"), dict):
        return None
    return value


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


def cache_result(action, result):
    """Best-effort cache update; an API result never depends on this write."""
    with _cache_lock:
        try:
            _prepare_cache_directory()
            snapshot = read_snapshot() or {"version": CACHE_VERSION, "views": {}}
            snapshot["views"][action] = result
            atomic_write_json(SNAPSHOT_PATH, snapshot)
        except OSError as error:
            print(f"Harvester UI cache write skipped: {error}", file=sys.stderr)


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


BRIDGE_ACTIONS = frozenset({
    "__ping__", "providers", "inventory", "list.movies", "list.shows",
    "list.actors", "get.movie", "get.show", "get.actor",
})
