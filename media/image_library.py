"""Shared, annotated image collections with owner-controlled writes."""
import contextlib
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import uuid

from PIL import Image, UnidentifiedImageError

from .common import JOB_ID

MAX_IMAGE_BYTES = 200 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
MAX_BATCH_IMAGES = 16
MAX_PROFILE_IMAGES = 256


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class LibraryError(ValueError):
    def __init__(self, detail, status=422):
        super().__init__(detail)
        self.status = status


def text(value, label, maximum, *, required=True, multiline=False):
    if not isinstance(value, str) or len(value.strip()) > maximum or any(ord(c) < 32 and not (multiline and c in "\n\r\t") for c in value):
        raise LibraryError(f"{label}格式无效，最多 {maximum} 个字符。")
    value = value.strip()
    if required and not value:
        raise LibraryError(f"请填写{label}。")
    return value


def inspect_image(path):
    try:
        size = path.stat().st_size
        if not 0 < size <= MAX_IMAGE_BYTES:
            raise LibraryError("每张图片必须非空且不超过 200 MiB。")
        with Image.open(path) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > MAX_IMAGE_PIXELS:
                raise LibraryError("图片须为 PNG、JPEG 或 WebP，且不超过 2500 万像素。")
            result = {"width": image.width, "height": image.height, "format": image.format,
                      "size": size, "sha256": file_hash(path)}
            image.verify()
        return result
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise LibraryError("图片内容无效，无法导入。") from None


class ImageLibrary:
    def __init__(self, data):
        self.data = Path(data)
        self.root = self.data / "profiles"
        self.lock = threading.RLock()

    @contextlib.contextmanager
    def db(self):
        self.data.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.data / "profiles.sqlite3", timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, owner TEXT NOT NULL, updated REAL NOT NULL, body TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS profile_audit (id INTEGER PRIMARY KEY, at REAL, actor TEXT, profile TEXT, action TEXT, detail TEXT);
                    CREATE TABLE IF NOT EXISTS profile_deletions (profile TEXT NOT NULL, filename TEXT NOT NULL, PRIMARY KEY(profile,filename));
                """)
                connection.execute("BEGIN IMMEDIATE")
                # Durable intent is committed before deleting files. Every library request
                # retries interrupted cleanup; a failed metadata transaction cannot lose images.
                for pending in connection.execute("SELECT profile,filename FROM profile_deletions").fetchall():
                    try:
                        target = self.checked_path(pending["profile"], pending["filename"])
                        target.unlink(missing_ok=True)
                        if target.parent.is_dir() and not any(target.parent.iterdir()):
                            target.parent.rmdir()
                    except (OSError, LibraryError):
                        continue
                    connection.execute("DELETE FROM profile_deletions WHERE profile=? AND filename=?", (pending["profile"], pending["filename"]))
                yield connection
        finally:
            connection.close()

    @staticmethod
    def public(profile, user):
        result = {**profile, "editable": profile["owner"]["id"] == user["id"] or bool(user.get("admin"))}
        result["images"] = [{**image, "url": f"/image/api/profiles/{profile['id']}/images/{image['id']}/file"} for image in profile["images"]]
        return result

    @staticmethod
    def get(conn, profile_id, user=None, edit=False):
        if not isinstance(profile_id, str) or not JOB_ID.fullmatch(profile_id):
            raise LibraryError("图片档案不存在。", 404)
        row = conn.execute("SELECT body FROM profiles WHERE id=?", (profile_id,)).fetchone()
        profile = json.loads(row["body"]) if row else None
        if not profile or profile.get("deleted_at"):
            raise LibraryError("图片档案不存在。", 404)
        if edit and profile["owner"]["id"] != user["id"] and not user.get("admin"):
            raise LibraryError("仅档案创建者或管理员可以修改此档案。", 403)
        return profile

    @staticmethod
    def save(conn, profile, user, action, detail):
        profile["updated_at"] = time.time()
        conn.execute("INSERT OR REPLACE INTO profiles VALUES (?,?,?,?)", (profile["id"], profile["owner"]["id"], profile["updated_at"], json.dumps(profile, ensure_ascii=False)))
        conn.execute("INSERT INTO profile_audit(at,actor,profile,action,detail) VALUES (?,?,?,?,?)", (time.time(), user["id"], profile["id"], action, json.dumps(detail, ensure_ascii=False)))

    def list(self, user):
        with self.lock, self.db() as conn:
            rows = conn.execute("SELECT body FROM profiles ORDER BY updated DESC").fetchall()
            return [self.public(profile, user) for row in rows if not (profile := json.loads(row["body"])).get("deleted_at")]

    def read(self, profile_id, user):
        with self.lock, self.db() as conn:
            return self.public(self.get(conn, profile_id), user)

    def checked_path(self, profile_id, filename):
        if not JOB_ID.fullmatch(profile_id) or Path(filename).name != filename:
            raise LibraryError("图片文件不存在。", 404)
        folder = self.root / profile_id
        path = folder / filename
        if any(item.is_symlink() for item in (path, folder, self.root, self.data)) or path.resolve().parent != folder.resolve():
            raise LibraryError("图片文件路径异常。", 409)
        return path

    def add(self, user, items, *, name=None, description="", profile_id=None):
        if not 1 <= len(items) <= MAX_BATCH_IMAGES:
            raise LibraryError("每次请导入 1 至 16 张图片。")
        checked = [(path, text(label, "标签", 80), text(filename, "文件名", 240), inspect_image(path)) for path, label, filename in items]
        if not profile_id:
            name, description = text(name, "档案名称", 100), text(description, "档案说明", 2000, required=False, multiline=True)
        written = []
        with self.lock, self.db() as conn:
            profile = self.get(conn, profile_id, user, True) if profile_id else {
                "id": uuid.uuid4().hex, "name": name, "description": description,
                "owner": {"id": user["id"], "name": user["name"]}, "created_at": time.time(), "images": []}
            if len(profile["images"]) + len(checked) > MAX_PROFILE_IMAGES:
                raise LibraryError("每个档案最多保存 256 张图片。")
            try:
                for source, label, original, metadata in checked:
                    image_id = uuid.uuid4().hex
                    filename = image_id + {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[metadata["format"]]
                    target = self.checked_path(profile["id"], filename)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    written.append(target)
                    if file_hash(target) != metadata["sha256"]:
                        raise LibraryError("图片内容在导入期间发生变化，请重新上传。", 409)
                    profile["images"].append({"id": image_id, "label": label, "filename": filename, "original_name": original, "created_at": time.time(), **metadata})
                self.save(conn, profile, user, "add_images" if profile_id else "create", {"image_ids": [path.stem for path in written]})
            except Exception:
                for path in written:
                    path.unlink(missing_ok=True)
                raise
            return self.public(profile, user)

    def image(self, conn, profile_id, image_id, user=None, edit=False):
        profile = self.get(conn, profile_id, user, edit)
        image = next((item for item in profile["images"] if item["id"] == image_id), None)
        if image is None:
            raise LibraryError("档案图片不存在。", 404)
        return profile, image

    def file(self, profile_id, image_id):
        with self.lock, self.db() as conn:
            _, image = self.image(conn, profile_id, image_id)
            path = self.checked_path(profile_id, image["filename"])
            if not path.is_file():
                raise LibraryError("档案图片文件不存在。", 404)
            return path

    def snapshot(self, profile_id, image_id, target):
        with self.lock, self.db() as conn:
            profile, image = self.image(conn, profile_id, image_id)
            source = self.checked_path(profile_id, image["filename"])
            if not source.is_file():
                raise LibraryError("档案图片文件不存在。", 404)
            shutil.copyfile(source, target)
            if file_hash(target) != image["sha256"]:
                target.unlink(missing_ok=True)
                raise LibraryError("档案图片校验失败，请重新导入原图。", 409)
            return {"profile_id": profile_id, "profile_name": profile["name"], "image_id": image_id, "label": image["label"], "sha256": image["sha256"]}

    def update(self, profile_id, user, body, image_id=None):
        with self.lock, self.db() as conn:
            profile = self.get(conn, profile_id, user, True)
            if image_id:
                profile, image = self.image(conn, profile_id, image_id, user, True)
                image["label"] = text(body.get("label"), "标签", 80)
            else:
                profile["name"] = text(body.get("name", profile["name"]), "档案名称", 100)
                profile["description"] = text(body.get("description", profile["description"]), "档案说明", 2000, required=False, multiline=True)
            self.save(conn, profile, user, "update_label" if image_id else "update", {"image_id": image_id})
            return self.public(profile, user)

    def delete(self, profile_id, user, image_id=None):
        with self.lock, self.db() as conn:
            profile = self.get(conn, profile_id, user, True)
            images = [self.image(conn, profile_id, image_id)[1]] if image_id else profile["images"]
            for image in images:
                self.checked_path(profile_id, image["filename"])
                conn.execute("INSERT OR IGNORE INTO profile_deletions VALUES (?,?)", (profile_id, image["filename"]))
            if image_id:
                profile["images"] = [image for image in profile["images"] if image["id"] != image_id]
            else:
                profile["deleted_at"] = time.time()
            self.save(conn, profile, user, "delete_image" if image_id else "delete", {"images": images})
        with self.lock, self.db() as conn:
            if conn.execute("SELECT 1 FROM profile_deletions WHERE profile=? LIMIT 1", (profile_id,)).fetchone():
                raise LibraryError("图片已标记删除，磁盘文件暂时被占用；刷新档案会自动继续清理。", 409)
        return self.public(profile, user) if image_id else {"id": profile_id, "deleted": True}
