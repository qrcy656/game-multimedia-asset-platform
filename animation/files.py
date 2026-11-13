import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import quote

ALLOWED = {"assets", "jobs", "characters", "motions"}


def safe_path(root: Path, value: str | Path, must_exist=True) -> Path:
    root = root.resolve()
    raw = str(value)
    candidate = Path(raw)
    candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("Path is outside DATA_ROOT")
    relative = candidate.relative_to(root)
    if not relative.parts or relative.parts[0] not in ALLOWED:
        raise ValueError("Path is not in a permitted media directory")
    if must_exist and not candidate.is_file():
        raise ValueError("Media file does not exist")
    return candidate


def relative_path(root, path):
    return safe_path(root, path, must_exist=False).relative_to(root.resolve()).as_posix()


def file_url(root, path):
    return "/animation/files/" + quote(relative_path(root, path), safe="/")


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2, default=str)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def prose(value, limit=3000):
    """No arbitrary prompt structure can be introduced through descriptive fields."""
    value = str(value or "")
    if re.search(r"(?im)^\s*(subject_definitions|summary|retention_analysis|detailed_description|overall_soundscape|non_diegetic_music)\s*:", value):
        raise ValueError("Description must contain prose, not prompt sections")
    return " ".join(value.replace("<", "").replace(">", "").split())[:limit]
