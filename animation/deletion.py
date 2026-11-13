"""Transactional file removal by rename, with crash recovery and no retained copy."""
import json
import os
from pathlib import Path
import re
import uuid

from .files import atomic_json

MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov", ".webm", ".mkv", ".wav", ".mp3", ".zip"}
DIRECTORIES = {"characters", "motions", "jobs", "assets"}
def checked_path(root, value, allow_staging=False):
    """Reject traversal and links before dereferencing a path for deletion."""
    root = Path(root).resolve()
    raw = Path(value)
    target = raw if raw.is_absolute() else root / raw
    if ".." in target.parts or not target.is_relative_to(root):
        raise ValueError("Deletion path is outside DATA_ROOT")
    relative = target.relative_to(root)
    allowed = DIRECTORIES | ({".delete-staging"} if allow_staging else set())
    if len(relative.parts) < 2 or relative.parts[0] not in allowed:
        raise ValueError("Deletion requires an exact resource path")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Deletion refuses symbolic links")
    if not target.resolve().is_relative_to(root):
        raise ValueError("Deletion path escaped DATA_ROOT")
    return target


def resource_directory(root, table, object_id):
    if table not in DIRECTORIES or object_id == "uploads" or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", object_id):
        raise ValueError("Invalid resource directory")
    return checked_path(root, Path(table) / object_id)


def directory_files(root, directory, media_only=False):
    directory = checked_path(root, directory)
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise ValueError("Resource directory is not a directory")
    files = []
    for folder, directories, names in os.walk(directory, followlinks=False):
        for name in directories + names:
            checked_path(root, Path(folder) / name)
        for name in names:
            path = Path(folder) / name
            if not path.is_file():
                raise ValueError("Deletion refuses non-regular files")
            if not media_only or path.suffix.lower() in MEDIA_SUFFIXES:
                files.append(path)
    return files


class FileRemoval:
    def __init__(self, root, files, directories=(), operation_id=None):
        self.root = Path(root).resolve()
        self.operation_id = operation_id or uuid.uuid4().hex
        self.stage = checked_path(self.root, ".delete-staging/" + self.operation_id, allow_staging=True)
        self.files = sorted(set(checked_path(self.root, p) for p in files))
        self.directories = [checked_path(self.root, p) for p in directories]
        self.entries = [{"source": p.relative_to(self.root).as_posix(), "staged": str(i),
                         "bytes": p.stat().st_size} for i, p in enumerate(self.files) if p.is_file()]

    def prepare(self):
        self.stage.mkdir(parents=True, mode=0o700, exist_ok=False)
        atomic_json(self.stage / "manifest.json", {"operation_id": self.operation_id, "entries": self.entries,
                    "directories": [p.relative_to(self.root).as_posix() for p in self.directories]})
        for entry in self.entries:
            source = checked_path(self.root, entry["source"])
            os.replace(source, self.stage / entry["staged"])

    @classmethod
    def recover(cls, root, operation_id):
        if not re.fullmatch(r"[a-f0-9]{32}", operation_id):
            raise ValueError("Invalid deletion operation")
        obj = cls(root, [], operation_id=operation_id)
        manifest = obj.stage / "manifest.json"
        if not manifest.is_file():
            return obj
        content = json.loads(manifest.read_text(encoding="utf-8"))
        if content.get("operation_id") != operation_id:
            raise ValueError("Deletion manifest identity mismatch")
        obj.entries = content["entries"]
        for entry in obj.entries:
            checked_path(obj.root, entry["source"])
            if not re.fullmatch(r"[0-9]+", entry["staged"]):
                raise ValueError("Invalid staged filename")
            checked_path(obj.root, obj.stage / entry["staged"], allow_staging=True)
        obj.directories = [checked_path(obj.root, p) for p in content["directories"]]
        return obj

    def rollback(self):
        for entry in reversed(self.entries):
            staged = checked_path(self.root, self.stage / entry["staged"], allow_staging=True)
            if staged.exists():
                source = checked_path(self.root, entry["source"])
                if source.exists():
                    raise RuntimeError("Cannot restore deleted source over another file")
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, source)
        self._remove_stage()

    def purge(self):
        for entry in self.entries:
            path = checked_path(self.root, self.stage / entry["staged"], allow_staging=True)
            path.unlink(missing_ok=True)
        self._remove_stage()
        for directory in self.directories:
            if directory.exists():
                for folder, _, _ in os.walk(directory, topdown=False, followlinks=False):
                    path = checked_path(self.root, folder)
                    try:
                        path.rmdir()
                    except OSError:
                        pass  # A retained shared file intentionally keeps its parent.

    def _remove_stage(self):
        (self.stage / "manifest.json").unlink(missing_ok=True)
        if self.stage.exists():
            self.stage.rmdir()
        try:
            self.stage.parent.rmdir()
        except OSError:
            pass
