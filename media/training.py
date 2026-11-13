"""Validate training datasets and inert download artifacts without extraction."""
import io
import json
import math
from pathlib import Path, PurePosixPath
import stat
import struct
import zipfile

MAX_BYTES = 256 * 1024 * 1024
AUDIO_SUFFIXES = {'.wav', '.mp3', '.flac'}


def zip_entries(source, allowed):
    archive = None
    try:
        archive = zipfile.ZipFile(source)
        entries, total = [], 0
        for info in archive.infolist():
            name = PurePosixPath(info.filename)
            if (name.is_absolute() or '..' in name.parts or '\\' in info.orig_filename or ':' in info.orig_filename
                    or '\0' in info.orig_filename or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1):
                raise ValueError('ZIP 包含不安全路径、链接或加密文件。')
            if info.is_dir():
                continue
            if name.suffix.lower() not in allowed or info.file_size <= 0:
                raise ValueError('ZIP 包含不支持的文件类型或空文件。')
            total += info.file_size
            if total > 2 * 1024 ** 3 or len(entries) >= 500 or info.file_size > MAX_BYTES:
                raise ValueError('ZIP 最多 500 个文件，解压总计最多 2 GiB，单个文件最多 256 MiB。')
            entries.append(info)
        if not entries or len({entry.filename for entry in entries}) != len(entries):
            raise ValueError('ZIP 为空或包含重复文件名。')
        return archive, entries
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        if archive is not None:
            archive.close()
        if isinstance(exc, ValueError):
            raise
        raise ValueError('无法读取有效 ZIP 文件。') from exc


def validate_dataset(path):
    path = Path(path)
    if path.suffix.lower() != '.zip' or not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError('训练数据必须为不超过 256 MiB 的音频 ZIP。')
    archive, entries = zip_entries(path, AUDIO_SUFFIXES)
    with archive:
        for info in entries:
            with archive.open(info) as stream:
                header = stream.read(12)
            suffix = PurePosixPath(info.filename).suffix.lower()
            valid = ((suffix == '.wav' and header[:4] == b'RIFF' and header[8:12] == b'WAVE')
                or (suffix == '.flac' and header[:4] == b'fLaC')
                or (suffix == '.mp3' and (header[:3] == b'ID3' or len(header) > 1 and header[0] == 255 and header[1] & 224 == 224)))
            if not valid:
                raise ValueError('ZIP 中音频内容与 WAV、MP3 或 FLAC 格式不符。')


def safetensors(data):
    if len(data) < 10:
        return False
    size = struct.unpack('<Q', data[:8])[0]
    if not 2 <= size <= min(16 * 1024 * 1024, len(data) - 8):
        return False
    try:
        header = json.loads(data[8:8 + size])
        if not isinstance(header, dict):
            return False
        tensors = [value for key, value in header.items() if key != '__metadata__']
        intervals = []
        sizes = {'BOOL': 1, 'U8': 1, 'I8': 1, 'U16': 2, 'I16': 2, 'U32': 4, 'I32': 4,
                 'U64': 8, 'I64': 8, 'F16': 2, 'BF16': 2, 'F32': 4, 'F64': 8, 'F8_E4M3': 1, 'F8_E5M2': 1}
        for value in tensors:
            shape, offsets = value['shape'], value['data_offsets']
            if not isinstance(value['dtype'], str) or not isinstance(shape, list) or any(type(n) is not int or n < 0 for n in shape):
                return False
            if len(offsets) != 2 or any(type(n) is not int for n in offsets) or not 0 <= offsets[0] <= offsets[1] <= len(data) - 8 - size:
                return False
            if value['dtype'] not in sizes or offsets[1] - offsets[0] != math.prod(shape) * sizes[value['dtype']]:
                return False
            intervals.append(offsets)
        intervals.sort()
        return bool(tensors) and all(a[1] <= b[0] for a, b in zip(intervals, intervals[1:]))
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def training_artifact(data):
    if safetensors(data):
        return 'file', '.safetensors'
    if data[:4] == b'PK\x03\x04':
        archive, entries = zip_entries(io.BytesIO(data), {'.safetensors', '.json', '.txt', '.yaml', '.yml'})
        with archive:
            weights = [info for info in entries if PurePosixPath(info.filename).suffix.lower() == '.safetensors']
            if weights and all(safetensors(archive.read(info)) for info in weights):
                return 'file', '.zip'
    raise ValueError('训练 API 必须返回有效 safetensors 权重或含权重的 ZIP；不接受可执行或 pickle 模型文件。')
