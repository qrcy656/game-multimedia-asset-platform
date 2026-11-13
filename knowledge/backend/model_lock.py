"""Coordinate managed Ollama requests with the host model controller."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import os
from pathlib import Path
import stat
import time

from fastapi import HTTPException

try:
    import fcntl
except ImportError:  # Managed model control is deployed on Linux only.
    fcntl = None


def _open_lock(directory: Path, model: str) -> int:
    """Open an existing controller-owned lock without following symlinks."""
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock_fd = None
    try:
        directory_info = os.fstat(directory_fd)
        if (not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.geteuid()
                or directory_info.st_mode & 0o077):
            raise OSError('Model control lock directory has an unsafe owner or permissions')
        filename = hashlib.sha256(model.encode('utf-8')).hexdigest() + '.lock'
        lock_fd = os.open(filename, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        info = os.fstat(lock_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise OSError('Model control lock is not a private regular file owned by this service')
        return lock_fd
    except BaseException:
        if lock_fd is not None:
            os.close(lock_fd)
        raise
    finally:
        os.close(directory_fd)


@asynccontextmanager
async def shared_model_lock(model: str, *, enabled: bool, directory: Path, timeout: float = 30):
    """Hold a shared lock from the residency check through the complete response.

    The controller holds an exclusive lock through load/unload and verification.
    No directory/file is created here: a missing mount or unknown model fails closed.
    Nonblocking polling keeps the event loop responsive and cancellation closes the fd.
    """
    if not enabled:
        yield
        return
    lock_fd = None
    try:
        if fcntl is None:
            raise OSError('Managed model locks require Linux flock')
        lock_fd = _open_lock(directory, model)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HTTPException(503, '模型正在启动或关闭，请稍后重试') from None
                await asyncio.sleep(min(0.1, remaining))
    except (OSError, AttributeError):
        if lock_fd is not None:
            os.close(lock_fd)
            lock_fd = None
        raise HTTPException(503, '模型控制暂不可用，请稍后重试或联系管理员') from None
    except BaseException:
        if lock_fd is not None:
            os.close(lock_fd)
            lock_fd = None
        raise
    try:
        yield
    finally:
        # Closing releases flock even when the request raises or is cancelled.
        if lock_fd is not None:
            os.close(lock_fd)
