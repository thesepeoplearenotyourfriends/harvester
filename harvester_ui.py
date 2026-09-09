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


def _scope_path(scope):
    prefix = f"asset://{PACKAGE_ID}/.cache/ui/"
    asset = scope.get("asset") if isinstance(scope, dict) else None
    suffix = asset[len(prefix):] if isinstance(asset, str) and asset.startswith(prefix) else ""
    if not suffix or "/" in suffix:
        raise BridgeError("invalid frozen scope asset")
    return CACHE_DIR / suffix


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
    "config.get": None, "config.save": None,
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
    if action == "config.get":
        return get_configuration(data)
    if action == "config.save":
        return save_configuration(data)
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


def run_streaming_action(action, data, on_event):
    """Consume Bulk stdout incrementally while preserving its strict terminal contract."""
    argv = action_argv(action, data)
    process = subprocess.Popen(argv, cwd=PROJECT_DIR, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, bufsize=1)
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
            raise BridgeError("Harvester returned no terminal result")
        if terminal["type"] == "error" or not terminal.get("ok", False):
            raise BridgeError(str(terminal.get("error") or "Harvester API request failed"))
        if terminal["type"] != "result" or "result" not in terminal:
            raise BridgeError("Harvester returned an invalid result record")
        if returncode:
            raise BridgeError(f"Harvester API exited with status {returncode}")
        return terminal["result"]
    finally:
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
        if message["action"] in ("bulk.workflow", "bulk.item"):
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
            result = run_streaming_action(message["action"], message["data"], publish_event)
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
