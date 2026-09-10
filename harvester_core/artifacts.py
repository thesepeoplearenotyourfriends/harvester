"""Preparation/commit boundary for materialized library artifacts."""

from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import shutil

from .storage import save_json_atomic, write_bytes_atomic, write_library_bytes_atomic


class FilesystemCommitter:
    """Commit prepared bytes and related filesystem mutations atomically."""

    committing = True

    def mkdir(self, path):
        Path(path).mkdir(parents=True, exist_ok=True)

    def write(self, path, data):
        write_library_bytes_atomic(path, data)

    def unlink(self, path):
        Path(path).unlink()

    def exists(self, path):
        return Path(path).exists()

    def stat(self, path):
        return Path(path).stat()


class RecordingCommitter:
    """Record exact intended mutations without touching the filesystem."""

    committing = False

    def __init__(self):
        self.actions = []
        self._files = {}
        self._removed = set()
        self._directories = set()

    def mkdir(self, path):
        path = Path(path)
        self._directories.add(path)
        self.actions.append({"action": "mkdir", "path": str(path)})

    def write(self, path, data):
        path = Path(path)
        self._files[path] = data
        self._removed.discard(path)
        self.actions.append({"action": "write", "path": str(path), "bytes": data})

    def unlink(self, path):
        path = Path(path)
        self._files.pop(path, None)
        self._removed.add(path)
        self.actions.append({"action": "unlink", "path": str(path)})

    def exists(self, path):
        path = Path(path)
        if path in self._removed:
            return False
        return path in self._files or path in self._directories or path.exists()

    def stat(self, path):
        path = Path(path)
        if path in self._files:
            return SimpleNamespace(st_size=len(self._files[path]))
        if path in self._removed:
            raise FileNotFoundError(path)
        return path.stat()


def use_committer(committer):
    return committer or FilesystemCommitter()


def planned(committer):
    return list(getattr(committer, "actions", ()))


def _prepared_root(config):
    return config.app_dir / ".cache" / "bulk"


def _safe_root(config, *components):
    current = config.app_dir
    for component in (".cache", "bulk", *components):
        current = current / component
        if current.is_symlink():
            raise OSError(f"refusing symlinked preparation cache path: {current}")
        current.mkdir(mode=0o700, exist_ok=True)
    return current


def _precondition(path):
    path = Path(path)
    if not path.exists():
        return {"exists": False}
    if path.is_file():
        data = path.read_bytes()
        return {"exists": True, "kind": "file", "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}
    return {"exists": True, "kind": "directory" if path.is_dir() else "other"}


def persist_preparation(config, workflow, identities, committer, *, state="ready",
                        display_title=None, local_target=None, summary=None,
                        reason=None, requested_artifacts=None, logical_identity=None):
    """Persist one durable, independently reviewable logical Inbox item."""
    plan_id = preparation_id(workflow, identities, logical_identity)
    root = _safe_root(config, "inbox", plan_id)
    previous = None
    try:
        previous = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        pass
    blobs = root / "blobs"
    manifest_actions = []
    for action in planned(committer):
        item = {key: value for key, value in action.items() if key != "bytes"}
        item["precondition"] = _precondition(action["path"])
        if action["action"] == "write":
            data = action["bytes"]
            digest = hashlib.sha256(data).hexdigest()
            blob = blobs / digest
            if not blob.exists():
                write_bytes_atomic(blob, data)
            item.update({"blob": f"blobs/{digest}", "size": len(data),
                         "sha256": digest})
        manifest_actions.append(item)
    manifest = {"version": 2, "item_id": plan_id, "workflow": workflow,
                "identities": list(identities), "display_title": display_title or
                (str(identities[0]) if identities else workflow),
                "local_target": local_target, "state": state, "seen": False,
                "reason": reason, "summary": summary or {},
                "logical_identity": logical_identity,
                "query": (previous or {}).get("query", {}),
                "history": [*((previous or {}).get("history", [])), *([{
                    "state": previous.get("state"), "reason": previous.get("reason"),
                    "summary": previous.get("summary", {}),
                    "query": previous.get("query", {})}] if previous else [])][-10:],
                "requested_artifacts": list(requested_artifacts or ()),
                "actions": manifest_actions}
    save_json_atomic(root / "manifest.json", manifest)
    return {"plan_id": plan_id,
            "manifest": f"asset://com.harvester.app/.cache/bulk/inbox/{plan_id}/manifest.json",
            "prepared": sum(action["action"] == "write" for action in manifest_actions)}


def preparation_id(workflow, identities, logical_identity=None):
    """Return the stable Inbox identity for a logical preparation proposal."""
    stable = json.dumps({"workflow": workflow, "identities": list(identities),
                         "logical_identity": logical_identity},
                        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:32]


def migrate_inbox_identity(config, old_identity, new_identity):
    """Move a live proposal to a replacement durable identity without losing work."""
    inbox = _prepared_root(config) / "inbox"
    matches = [item for item in list_inbox(config)
               if item.get("identities") == [old_identity]]
    migrated = []
    for old in matches:
        migrated.append(_migrate_inbox_item(inbox, old, old_identity, new_identity))
    return migrated


def _migrate_inbox_item(inbox, old, old_identity, new_identity):
    """Move one manifest; split out so every matching workflow is migrated."""
    import shutil
    old_root = inbox / old["item_id"]
    new_id = preparation_id(old["workflow"], [new_identity], old.get("logical_identity"))
    new_root = inbox / new_id
    existing = None
    if new_root != old_root and (new_root / "manifest.json").is_file():
        existing = json.loads((new_root / "manifest.json").read_text(encoding="utf-8"))
    if new_root != old_root and not new_root.exists():
        old_root.rename(new_root)
    elif new_root != old_root:
        (new_root / "blobs").mkdir(parents=True, exist_ok=True)
        for blob in (old_root / "blobs").glob("*"):
            target = new_root / "blobs" / blob.name
            if not target.exists():
                shutil.copy2(blob, target)
        shutil.rmtree(old_root)
    manifest = old
    if existing:
        actions = [*existing.get("actions", []), *old.get("actions", [])]
        manifest["actions"] = list({(action.get("action"), action.get("path")): action
                                    for action in actions}.values())
        manifest["requested_artifacts"] = list(dict.fromkeys(
            [*existing.get("requested_artifacts", []), *old.get("requested_artifacts", [])]))
        if existing.get("state") == "needs_attention":
            manifest["state"] = "needs_attention"
            manifest["reason"] = existing.get("reason")
    # Adoption makes a prepared write to the dead inferred NFO target obsolete;
    # poster and other independent work remains reviewable under the new key.
    manifest["actions"] = [action for action in manifest.get("actions", []) if not (
        action.get("action") == "write" and action.get("path") == old_identity)]
    manifest.update({"item_id": new_id, "identities": [new_identity]})
    if manifest.get("local_target") == old_identity:
        manifest["local_target"] = new_identity
    save_json_atomic(new_root / "manifest.json", manifest)
    return new_id


def list_inbox(config):
    root = _prepared_root(config) / "inbox"
    if not root.is_dir() or root.is_symlink():
        return []
    items = []
    for path in sorted(root.glob("*/manifest.json")):
        try:
            if path.parent.is_symlink() or path.is_symlink():
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("version") == 2 and value.get("state") in ("ready", "needs_attention"):
                items.append(value)
        except (OSError, ValueError):
            continue
    return items


def get_inbox_item(config, item_id, *, mark_seen=False):
    if not isinstance(item_id, str) or len(item_id) != 32 or any(c not in "0123456789abcdef" for c in item_id):
        raise ValueError("invalid Bulk Inbox item id")
    path = _prepared_root(config) / "inbox" / item_id / "manifest.json"
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("refusing symlinked Bulk Inbox item")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("version") != 2 or value.get("item_id") != item_id:
        raise ValueError("invalid Bulk Inbox manifest")
    if mark_seen and not value.get("seen"):
        value["seen"] = True
        save_json_atomic(path, value)
    return value


def _inside_library(config, path):
    resolved = Path(path).resolve()
    return any(resolved == root.resolve() or root.resolve() in resolved.parents
               for root in (config.movie_root, config.tv_root))


def apply_inbox_item(config, item_id):
    """Validate and apply frozen bytes without constructing a provider client."""
    manifest = get_inbox_item(config, item_id)
    if manifest["state"] != "ready":
        raise ValueError("Bulk Inbox item is not ready to apply")
    root = _prepared_root(config) / "inbox" / item_id
    prepared = []
    for action in manifest["actions"]:
        if (not isinstance(action, dict) or action.get("action") not in
                ("mkdir", "write", "unlink") or not isinstance(action.get("path"), str)):
            raise ValueError("prepared filesystem action is invalid")
        path = Path(action["path"])
        if not _inside_library(config, path):
            raise ValueError("prepared destination is outside configured media roots")
        observed = _precondition(path)
        harmless_created_directory = (action["action"] == "mkdir" and
                                      action.get("precondition") == {"exists": False} and
                                      observed == {"exists": True, "kind": "directory"})
        if observed != action.get("precondition") and not harmless_created_directory:
            manifest.update({"state": "needs_attention",
                             "reason": "filesystem changed since preparation"})
            save_json_atomic(root / "manifest.json", manifest)
            raise ValueError(manifest["reason"])
        if action["action"] == "write":
            if action.get("blob") != f"blobs/{action.get('sha256', '')}":
                raise ValueError("prepared blob reference is invalid")
            blob = root / action["blob"]
            if blob.is_symlink() or root.resolve() not in blob.resolve().parents:
                raise ValueError("prepared blob path is unsafe")
            data = blob.read_bytes()
            if len(data) != action["size"] or hashlib.sha256(data).hexdigest() != action["sha256"]:
                raise ValueError("prepared blob failed size/hash validation")
            prepared.append((action, data))
        else:
            prepared.append((action, None))
    committer = FilesystemCommitter()
    for action, data in prepared:
        getattr(committer, action["action"])(action["path"], *( [data] if data is not None else []))
    shutil.rmtree(root)
    return {"item_id": item_id, "applied": 1}


def discard_inbox_item(config, item_id):
    get_inbox_item(config, item_id)
    shutil.rmtree(_prepared_root(config) / "inbox" / item_id)
    return {"item_id": item_id, "discarded": 1}
