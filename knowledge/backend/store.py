"""Markdown vault and provenance-preserving SQLite retrieval storage.

Only markdown files below the configured vault are exposed. Embedding providers are
injected so the storage layer never needs model credentials or network access.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Awaitable, Callable, Sequence
from urllib.parse import unquote


class StoreError(ValueError):
    """Invalid vault operation."""


class ConflictError(StoreError):
    """The source changed, or a destination already exists."""


class NotFoundError(StoreError):
    """A requested file or directory does not exist."""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _vector(values: Sequence[float]) -> list[float]:
    try:
        result = [float(v) for v in values]
    except (TypeError, ValueError) as exc:
        raise StoreError("Embedding 必须为数值向量") from exc
    if not result or any(not math.isfinite(v) for v in result):
        raise StoreError("Embedding 包含空向量或无效数字")
    norm = math.sqrt(sum(v * v for v in result))
    if not math.isfinite(norm) or norm == 0:
        raise StoreError("Embedding 向量的模必须大于零")
    return [v / norm for v in result]


def _terms(text: str) -> set[str]:
    """Chinese bigrams and Latin words without an additional tokenizer service."""
    result: set[str] = set()
    for match in re.findall(r"[\u3400-\u9fff]+|[a-zA-Z0-9_]+", text.lower()):
        if re.match(r"[\u3400-\u9fff]", match):
            result.update(match[i:i + 2] for i in range(max(1, len(match) - 1)))
        else:
            result.add(match)
    return result


def markdown_links(content: str) -> list[str]:
    """Return local markdown/wiki link targets as data, never as filesystem reads."""
    links: set[str] = set()
    for match in re.finditer(r"!?\[\[([^\]\n]+)\]\]", content):
        target = match.group(1).split("|", 1)[0].split("#", 1)[0].strip()
        if target and not re.match(r"^[a-zA-Z][\w+.-]*:", target):
            links.add(target)
    for match in re.finditer(r"(?<!!)\[[^\]\n]*\]\(([^)\n]+)\)", content):
        target = match.group(1).strip()
        if target.startswith("<") and ">" in target:
            target = target[1:target.index(">")]
        else:
            target = re.split(r'\s+["\']', target, maxsplit=1)[0]
        target = unquote(target.split("#", 1)[0])
        if target.lower().endswith(".md") and not re.match(r"^[a-zA-Z][\w+.-]*:|^//", target):
            links.add(target)
    return sorted(links)


def chunk_markdown(content: str, path: str, file_hash: str, target: int = 350) -> list[dict]:
    """Split at headings and line boundaries, preserving exact source line ranges.

    Long individual lines are split, sharing the same line number. Code-fence
    contents are never mistaken for headings. Heading paths carry context when
    a long section is divided into several chunks.
    """
    if target < 1:
        raise StoreError("分块长度必须大于零")
    chunks: list[dict] = []
    headings: list[tuple[int, str]] = []
    buffer: list[str] = []
    start = 1
    end = 1
    length = 0
    fence: str | None = None

    def flush() -> None:
        nonlocal buffer, length
        text = "\n".join(buffer).strip()
        if text:
            chunks.append({"path": path, "title": " / ".join(h[1] for h in headings) or PurePosixPath(path).stem,
                           "start_line": start, "end_line": end, "text": text, "hash": file_hash})
        buffer = []
        length = 0

    for line_number, line in enumerate(content.splitlines(), 1):
        fence_match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line) if fence is None else None
        if heading:
            flush()
            level = len(heading.group(1))
            headings = [h for h in headings if h[0] < level]
            headings.append((level, heading.group(2)))
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
        if buffer and length + len(line) + 1 > target:
            flush()
        pieces = [line[i:i + target] for i in range(0, len(line), target)] or [""]
        for piece_index, piece in enumerate(pieces):
            if not buffer:
                start = line_number
            end = line_number
            buffer.append(piece)
            length += len(piece) + 1
            if piece_index < len(pieces) - 1:
                flush()
    flush()
    return chunks


class Store:
    def __init__(self, vault: Path, db: Path):
        self.vault = Path(vault).absolute()
        self.db = Path(db).absolute()
        if self.vault.is_symlink():
            raise StoreError("资料库根目录不能为符号链接")
        self.vault.mkdir(parents=True, exist_ok=True)
        self.vault = self.vault.resolve()
        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.trash = self.db.parent / "trash"
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS documents (
                    path TEXT PRIMARY KEY, hash TEXT NOT NULL,
                    indexed_at REAL NOT NULL, chunk_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL,
                    title TEXT NOT NULL, start_line INTEGER NOT NULL,
                    end_line INTEGER NOT NULL, text TEXT NOT NULL,
                    hash TEXT NOT NULL, vector TEXT NOT NULL,
                    FOREIGN KEY(path) REFERENCES documents(path) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
            """)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _path(self, value: str, *, markdown: bool = False, exists: bool = False) -> tuple[str, Path]:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise StoreError("路径不能为空")
        value = value.replace("\\", "/")
        if value.startswith("/") or ":" in value:
            raise StoreError("必须使用资料库内的相对路径")
        parts = value.split("/")
        if any(not p or p in (".", "..") or p.startswith(".") for p in parts):
            raise StoreError("路径不能包含隐藏目录、空目录名或上级目录")
        path = self.vault.joinpath(*parts)
        cursor = self.vault
        for part in parts:
            cursor = cursor / part
            if cursor.is_symlink() or (hasattr(cursor, "is_junction") and cursor.is_junction()):
                raise StoreError("不能访问符号链接或目录联接")
        if not path.resolve().is_relative_to(self.vault):
            raise StoreError("路径超出资料库范围")
        if markdown and path.suffix.lower() != ".md":
            raise StoreError("只允许 Markdown (.md) 文件")
        if exists and not path.exists():
            raise NotFoundError(f"路径不存在：{value}")
        return "/".join(parts), path

    def _read(self, value: str) -> tuple[str, Path, bytes]:
        name, path = self._path(value, markdown=True, exists=True)
        if not path.is_file():
            raise StoreError("目标必须为 Markdown 文件")
        # O_NOFOLLOW prevents symlink substitution of the final component on Linux.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            data = stream.read()
        return name, path, data

    @staticmethod
    def _decode(data: bytes) -> str:
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise StoreError("Markdown 文件需要 UTF-8 编码") from exc

    def _invalidate(self, path: str, *, subtree: bool = False) -> None:
        with self._connect() as conn:
            if subtree:
                # No LIKE pattern: filenames containing % and _ remain literal.
                conn.execute("DELETE FROM documents WHERE path=? OR substr(path,1,?)=?", (path, len(path) + 1, path + "/"))
            else:
                conn.execute("DELETE FROM documents WHERE path=?", (path,))

    def _valid_documents(self) -> dict[str, dict]:
        with self._connect() as conn:
            documents = [dict(row) for row in conn.execute("SELECT * FROM documents")]
            valid: dict[str, dict] = {}
            for document in documents:
                try:
                    _, _, data = self._read(document["path"])
                    if _digest(data) == document["hash"]:
                        valid[document["path"]] = document
                        continue
                except (OSError, StoreError):
                    pass
                conn.execute("DELETE FROM documents WHERE path=?", (document["path"],))
        return valid

    def _walk(self):
        for root, directories, files in os.walk(self.vault, followlinks=False):
            directories[:] = sorted(d for d in directories if not d.startswith(".") and not (Path(root) / d).is_symlink()
                                    and not (hasattr(Path(root) / d, "is_junction") and (Path(root) / d).is_junction()))
            yield Path(root), directories, files

    def list_dirs(self) -> list[str]:
        with self._lock:
            return [str((root / d).relative_to(self.vault).as_posix()) for root, dirs, _ in self._walk() for d in dirs]

    def list_files(self, query: str = "") -> list[dict]:
        with self._lock:
            indexed = self._valid_documents()
            result = []
            for root, _, filenames in self._walk():
                for filename in sorted(filenames):
                    path = root / filename
                    if filename.startswith(".") or path.suffix.lower() != ".md" or path.is_symlink():
                        continue
                    name = path.relative_to(self.vault).as_posix()
                    try:
                        _, path, data = self._read(name)
                        if query and query.casefold() not in name.casefold() and query.casefold() not in self._decode(data).casefold():
                            continue
                        stat = path.stat()
                        file_hash = _digest(data)
                    except (OSError, StoreError):
                        continue
                    document = indexed.get(name)
                    is_indexed = bool(document and document["hash"] == file_hash)
                    result.append({"path": name, "size": len(data), "mtime": stat.st_mtime,
                                   "hash": file_hash, "indexed": is_indexed,
                                   "indexed_hash": document["hash"] if is_indexed else None})
            return sorted(result, key=lambda item: item["path"])

    def read(self, path: str) -> dict:
        with self._lock:
            name, _, data = self._read(path)
            content = self._decode(data)
            file_hash = _digest(data)
            with self._connect() as conn:
                conn.execute("DELETE FROM documents WHERE path=? AND hash<>?", (name, file_hash))
            return {"path": name, "content": content, "hash": file_hash, "links": markdown_links(content)}

    def write(self, path: str, content: str, expected_hash: str | None = None, create: bool = False) -> dict:
        if not isinstance(content, str):
            raise StoreError("内容必须为字符串")
        with self._lock:
            name, destination = self._path(path, markdown=True)
            if create and destination.exists():
                raise ConflictError("文件已存在，请使用保存操作")
            if destination.exists():
                _, _, old_data = self._read(name)
                if expected_hash is None:
                    raise ConflictError("保存已有文件时需要 expected_hash，以防覆盖其他人的修改")
                if _digest(old_data) != expected_hash:
                    raise ConflictError("文件已被修改，请重新载入后保存")
            elif not create:
                raise NotFoundError("文件不存在，请使用新建操作")
            elif expected_hash is not None:
                raise ConflictError("新文件不应带有旧版本 hash")
            if not destination.parent.is_dir():
                raise NotFoundError("父目录不存在，请先创建目录")
            data = content.encode("utf-8")
            temporary = destination.parent / (".save-" + uuid.uuid4().hex)
            try:
                with temporary.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._path(name, markdown=True)
                if create:
                    # Link creation cannot silently overwrite a concurrent new file.
                    try:
                        os.link(temporary, destination)
                    except FileExistsError as exc:
                        raise ConflictError("文件刚刚由其他操作创建，请刷新") from exc
                    temporary.unlink()
                else:
                    _, _, current = self._read(name)
                    if _digest(current) != expected_hash:
                        raise ConflictError("文件已被修改，请重新载入后保存")
                    os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    temporary.unlink()
            self._invalidate(name)
            return {"path": name, "content": content, "hash": _digest(data), "links": markdown_links(content)}

    def mkdir(self, path: str) -> dict:
        with self._lock:
            name, destination = self._path(path)
            if destination.exists():
                raise ConflictError("路径已存在")
            if not destination.parent.is_dir():
                raise NotFoundError("父目录不存在")
            destination.mkdir()
            return {"path": name}

    def move(self, path: str, new_path: str, expected_hash: str | None = None) -> dict:
        with self._lock:
            name, source = self._path(path, exists=True)
            is_directory = source.is_dir()
            if not is_directory:
                _, _, data = self._read(name)
                if expected_hash is not None and expected_hash != _digest(data):
                    raise ConflictError("文件已被修改，请刷新")
            new_name, destination = self._path(new_path, markdown=not is_directory)
            if destination.exists():
                raise ConflictError("目标路径已存在")
            if is_directory and destination.is_relative_to(source):
                raise StoreError("不能把目录移入自身")
            if not destination.parent.is_dir():
                raise NotFoundError("目标父目录不存在")
            source.rename(destination)
            self._invalidate(name, subtree=is_directory)
            self._invalidate(new_name, subtree=is_directory)
            return {"path": name, "new_path": new_name}

    def delete(self, path: str, expected_hash: str | None = None) -> dict:
        with self._lock:
            name, source = self._path(path, exists=True)
            is_directory = source.is_dir()
            if is_directory:
                if any(source.iterdir()):
                    raise StoreError("只能删除空目录，请先处理目录内的文件")
            else:
                _, _, data = self._read(name)
                if expected_hash is not None and expected_hash != _digest(data):
                    raise ConflictError("文件已被修改，请刷新")
            if self.trash.is_symlink():
                raise StoreError("回收站不能为符号链接")
            self.trash.mkdir(parents=True, exist_ok=True)
            destination = self.trash / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex}-{source.name}"
            shutil.move(str(source), str(destination))
            self._invalidate(name, subtree=is_directory)
            return {"path": name, "trash_path": destination.name}

    async def index(self, paths: Sequence[str], embed: Callable[[list[str]], Awaitable[Sequence[Sequence[float]]]]) -> dict:
        if isinstance(paths, str):
            raise StoreError("paths 必须为路径列表")
        indexed = 0
        total_chunks = 0
        skipped: list[str] = []
        for path in dict.fromkeys(paths):
            with self._lock:
                document = self.read(path)
                chunks = chunk_markdown(document["content"], document["path"], document["hash"])
            vectors: list[list[float]] = []
            for offset in range(0, len(chunks), 32):
                batch = chunks[offset:offset + 32]
                response = await embed([c["title"][:50] + "\n" + c["text"] for c in batch])
                if len(response) != len(batch):
                    raise StoreError("Embedding 返回数量与文本数量不一致")
                vectors.extend(_vector(v) for v in response)
            if vectors and len({len(v) for v in vectors}) != 1:
                raise StoreError("Embedding 向量维度不一致")
            with self._lock:
                try:
                    current = self.read(document["path"])
                except NotFoundError:
                    skipped.append(document["path"])
                    continue
                if current["hash"] != document["hash"]:
                    skipped.append(document["path"])
                    continue
                with self._connect() as conn:
                    # A file is replaced in one transaction: readers never see half an index.
                    conn.execute("DELETE FROM documents WHERE path=?", (document["path"],))
                    conn.execute("INSERT INTO documents VALUES (?,?,?,?)", (document["path"], document["hash"], time.time(), len(chunks)))
                    for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
                        identifier = hashlib.sha256(f"{document['path']}\0{document['hash']}\0{i}".encode()).hexdigest()[:24]
                        conn.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)", (identifier, chunk["path"], chunk["title"],
                                     chunk["start_line"], chunk["end_line"], chunk["text"], chunk["hash"], json.dumps(vector)))
                indexed += 1
                total_chunks += len(chunks)
        return {"indexed": indexed, "chunks": total_chunks, "skipped": skipped}

    def search(self, query_vector: Sequence[float], query: str, limit: int = 12) -> list[dict]:
        vector = _vector(query_vector)
        terms = _terms(query)
        if limit < 1:
            return []
        with self._lock:
            valid = self._valid_documents()
            with self._connect() as conn:
                rows = conn.execute("SELECT * FROM chunks").fetchall()
            results = []
            for row in rows:
                if row["path"] not in valid:
                    continue
                stored_vector = json.loads(row["vector"])
                if len(vector) != len(stored_vector):
                    continue
                cosine = sum(a * b for a, b in zip(vector, stored_vector))
                matched = terms & _terms(row["title"] + " " + row["path"] + " " + row["text"])
                lexical = len(matched) / len(terms) if terms else 0.0
                result = {key: row[key] for key in ("id", "path", "title", "start_line", "end_line", "text", "hash")}
                result["score"] = round(0.8 * cosine + 0.2 * lexical, 6)
                result["semantic_score"] = round(cosine, 6)
                results.append(result)
            return sorted(results, key=lambda item: (-item["score"], item["path"], item["start_line"]))[:limit]

    def clear_index(self) -> dict:
        with self._lock, self._connect() as conn:
            count = conn.execute("SELECT count(*) FROM documents").fetchone()[0]
            conn.execute("DELETE FROM documents")
        return {"cleared": count}

    def stats(self) -> dict:
        with self._lock:
            files = self.list_files()
            with self._connect() as conn:
                chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
            indexed = sum(1 for item in files if item["indexed"])
            return {"files": len(files), "indexed": indexed, "unindexed": len(files) - indexed,
                    "chunks": chunks, "directories": len(self.list_dirs())}
