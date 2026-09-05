"""Preparation/commit boundary for materialized library artifacts."""

from pathlib import Path

from .storage import write_bytes_atomic


class FilesystemCommitter:
    """Commit prepared bytes and related filesystem mutations atomically."""

    committing = True

    def mkdir(self, path):
        Path(path).mkdir(parents=True, exist_ok=True)

    def write(self, path, data):
        write_bytes_atomic(path, data)

    def unlink(self, path):
        Path(path).unlink()


class RecordingCommitter:
    """Record exact intended mutations without touching the filesystem."""

    committing = False

    def __init__(self):
        self.actions = []

    def mkdir(self, path):
        self.actions.append({"action": "mkdir", "path": str(Path(path))})

    def write(self, path, data):
        self.actions.append({"action": "write", "path": str(Path(path)), "bytes": data})

    def unlink(self, path):
        self.actions.append({"action": "unlink", "path": str(Path(path))})


def use_committer(committer):
    return committer or FilesystemCommitter()


def planned(committer):
    return list(getattr(committer, "actions", ()))
