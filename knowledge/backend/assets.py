"""Read-only, provenance-preserving images from the vault's 素材库 directory."""
from __future__ import annotations

import hashlib
import io
import math
import os
import warnings
from pathlib import Path, PurePosixPath

from PIL import Image, ImageOps, UnidentifiedImageError

from .store import ConflictError, NotFoundError, Store, StoreError


MAX_FILE_BYTES = 80 * 1024 * 1024
MAX_IMAGE_PIXELS = 120_000_000
MAX_OUTPUT_SIDE = 4096
ASSET_DIRECTORY = "素材库"
FORMATS = {".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".webp": "WEBP", ".gif": "GIF"}
MIMES = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}


class AssetStore:
    """Expose safe image metadata and processed JPEGs without changing originals.

    Paths use the same vault-relative, no-hidden-files/no-links policy as Store.
    Images are never added to the vector index. Hashes always identify the exact
    original file; processed dimensions and crop identify what the model saw.
    """

    def __init__(self, vault: Path):
        self.vault = Path(vault).absolute()
        if self.vault.is_symlink() or self._is_junction(self.vault):
            raise StoreError("资料库根目录不能为符号链接或目录联接")
        self.vault = self.vault.resolve()

    @staticmethod
    def _is_junction(path: Path) -> bool:
        return hasattr(path, "is_junction") and path.is_junction()

    def _path(self, value: str) -> tuple[str, Path]:
        # Store._path only relies on self.vault; sharing its validation keeps the
        # Markdown and image access boundaries identical without a second DB.
        name, path = Store._path(self, value, exists=True)
        if PurePosixPath(name).parts[0] != ASSET_DIRECTORY:
            raise StoreError("只能读取资料库内的素材库图片")
        if path.suffix.lower() not in FORMATS:
            raise StoreError("只允许 PNG、JPEG、WebP 或 GIF 图片")
        if not path.is_file():
            raise StoreError("素材必须为图片文件")
        return name, path

    def _source(self, value: str) -> tuple[str, bytes]:
        name, path = self._path(value)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                if os.fstat(stream.fileno()).st_size > MAX_FILE_BYTES:
                    raise StoreError("素材图片超过 80 MiB 文件大小上限")
                data = stream.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise StoreError("素材图片超过 80 MiB 文件大小上限")
        except FileNotFoundError as exc:
            raise NotFoundError(f"路径不存在：{name}") from exc
        except OSError as exc:
            raise StoreError(f"无法读取素材图片：{name}") from exc
        return name, data

    @staticmethod
    def _open(data: bytes) -> Image.Image:
        # Pillow's warning threshold is lower than our explicit 120M-pixel cap.
        # Suppress only that warning locally, and enforce the cap before decode.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(data), formats=list(MIMES))
        width, height = image.size
        if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
            image.close()
            raise StoreError("素材图片超过 120M 像素上限")
        return image

    @staticmethod
    def _metadata(name: str, data: bytes, image: Image.Image) -> dict:
        if image.format != FORMATS[PurePosixPath(name).suffix.lower()]:
            raise StoreError("图片实际格式与文件扩展名不一致")
        width, height = image.size
        # PNG's overridden getexif() decodes all pixels when no EXIF is present.
        # The base implementation reads only the metadata already in the header.
        if Image.Image.getexif(image).get(274) in (5, 6, 7, 8):
            width, height = height, width
        return {
            "path": name, "hash": hashlib.sha256(data).hexdigest(),
            "title": PurePosixPath(name).stem, "kind": "image",
            "width": width, "height": height, "size": len(data),
            "mime": MIMES[image.format],
        }

    def metadata(self, path: str, expected_hash: str | None = None) -> dict:
        """Validate and identify a source without decoding or re-encoding pixels."""
        name, data = self._source(path)
        if expected_hash is not None and hashlib.sha256(data).hexdigest() != expected_hash:
            raise ConflictError("素材图片已发生变化，请重新读取最新来源")
        try:
            with self._open(data) as image:
                metadata = self._metadata(name, data, image)
                image.verify()
                return metadata
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError) as exc:
            if isinstance(exc, StoreError):
                raise
            raise StoreError(f"素材图片损坏或格式不受支持：{name}") from exc

    def list_assets(self, query: str = "") -> list[dict]:
        if not isinstance(query, str):
            raise StoreError("素材查询必须为文本")
        root = self.vault / ASSET_DIRECTORY
        if not root.is_dir() or root.is_symlink() or self._is_junction(root):
            return []
        needle = query.strip().casefold()
        result = []
        for directory, directories, files in os.walk(root, followlinks=False):
            parent = Path(directory)
            directories[:] = sorted(
                child for child in directories
                if not child.startswith(".")
                and not (parent / child).is_symlink()
                and not self._is_junction(parent / child)
            )
            for filename in sorted(files):
                path = parent / filename
                if filename.startswith(".") or path.suffix.lower() not in FORMATS:
                    continue
                name = path.relative_to(self.vault).as_posix()
                if needle and needle not in name.casefold():
                    continue
                try:
                    result.append(self.metadata(name))
                except (StoreError, OSError):
                    # A corrupt or concurrently removed asset cannot hide the
                    # remaining usable assets. Direct reads give a clear error.
                    continue
        return sorted(result, key=lambda item: item["path"].casefold())

    @staticmethod
    def _crop(crop) -> list | None:
        if crop is None:
            return None
        if not isinstance(crop, (list, tuple)) or len(crop) != 4:
            raise StoreError("裁剪区域需要 [左, 上, 右, 下] 四个 0–1000 坐标")
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not 0 <= value <= 1000 or not math.isfinite(value) for value in crop):
            raise StoreError("裁剪坐标必须为 0–1000 之间的有限数字")
        if crop[0] >= crop[2] or crop[1] >= crop[3]:
            raise StoreError("裁剪区域的右下角必须位于左上角之后")
        return list(crop)

    def read(self, path: str, expected_hash: str | None = None, crop=None,
             max_side: int = 1600) -> tuple[dict, bytes]:
        crop = self._crop(crop)
        if isinstance(max_side, bool) or not isinstance(max_side, int) or not 1 <= max_side <= MAX_OUTPUT_SIDE:
            raise StoreError(f"图片输出边长必须为 1–{MAX_OUTPUT_SIDE} 之间的整数")
        name, data = self._source(path)
        digest = hashlib.sha256(data).hexdigest()
        if expected_hash is not None and digest != expected_hash:
            raise ConflictError("素材图片已发生变化，请重新读取最新来源")
        try:
            with self._open(data) as image:
                metadata = self._metadata(name, data, image)
                image.seek(0)  # Animated GIF/WebP always use the first frame.
                image.load()
                ImageOps.exif_transpose(image, in_place=True)
                if crop is not None:
                    width, height = image.size
                    box = (math.floor(width * crop[0] / 1000), math.floor(height * crop[1] / 1000),
                           math.ceil(width * crop[2] / 1000), math.ceil(height * crop[3] / 1000))
                    selected = image.crop(box)
                else:
                    selected = image
                try:
                    selected.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                    if "A" in selected.getbands() or "transparency" in selected.info:
                        rgba = selected.convert("RGBA")
                        rgb = Image.new("RGB", selected.size, "white")
                        rgb.paste(rgba, mask=rgba.getchannel("A"))
                        rgba.close()
                    else:
                        rgb = selected.convert("RGB")
                    try:
                        output = io.BytesIO()
                        rgb.save(output, format="JPEG", quality=88, optimize=True)
                        metadata.update(crop=crop, processed_width=rgb.width, processed_height=rgb.height)
                        return metadata, output.getvalue()
                    finally:
                        rgb.close()
                finally:
                    if selected is not image:
                        selected.close()
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError) as exc:
            if isinstance(exc, StoreError):
                raise
            raise StoreError(f"素材图片损坏或格式不受支持：{name}") from exc
