"""Preparation/commit boundary for materialized library artifacts."""

from pathlib import Path
from types import SimpleNamespace
import hashlib
import uuid

from .storage import save_json_atomic, write_bytes_atomic


class FilesystemCommitter:
    """Commit prepared bytes and related filesystem mutations atomically."""

    committing = True

    def mkdir(self, path):
        Path(path).mkdir(parents=True, exist_ok=True)

    def write(self, path, data):
        write_bytes_atomic(path, data)

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


def persist_preparation(config, workflow, identities, committer):
    """Persist a disposable review manifest and content-addressed artifact blobs."""
    plan_id = uuid.uuid4().hex
    current = config.app_dir
    for component in (".cache", "bulk", plan_id):
        current = current / component
        if current.is_symlink():
            raise OSError(f"refusing symlinked preparation cache path: {current}")
        current.mkdir(mode=0o700, exist_ok=True)
    root = current
    blobs = root / "blobs"
    manifest_actions = []
    for action in planned(committer):
        item = {key: value for key, value in action.items() if key != "bytes"}
        if action["action"] == "write":
            data = action["bytes"]
            digest = hashlib.sha256(data).hexdigest()
            blob = blobs / digest
            if not blob.exists():
                write_bytes_atomic(blob, data)
            item.update({"blob": f"blobs/{digest}", "size": len(data),
                         "sha256": digest})
        manifest_actions.append(item)
    manifest = {"version": 1, "disposable": True, "workflow": workflow,
                "identities": list(identities), "actions": manifest_actions}
    save_json_atomic(root / "manifest.json", manifest)
    return {"plan_id": plan_id,
            "manifest": f"asset://com.harvester.app/.cache/bulk/{plan_id}/manifest.json",
            "prepared": sum(action["action"] == "write" for action in manifest_actions)}
