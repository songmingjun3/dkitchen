"""One cross-platform exclusive file lock, replacing direct `fcntl.flock` use.

On POSIX this is `fcntl.flock` exactly as before. On Windows it is `msvcrt.locking` over a
one-byte region, which is the closest portable equivalent: locks are per-region, exclusive,
and released when the handle closes or the process dies.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sys
from typing import Iterator

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

#: Byte offset of the region Windows locks; one byte is enough to name the lock.
_WINDOWS_LOCK_BYTE = 0


class LockBusy(RuntimeError):
    """Another holder owns the lock and the caller asked not to wait."""


@contextmanager
def exclusive(path: Path, *, blocking: bool = False) -> Iterator[None]:
    """Hold an exclusive lock on `path`, creating it if needed.

    `blocking=False` raises `LockBusy` instead of waiting, matching `ding`'s refusal to run
    two writers for one target.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    handle = os.fdopen(descriptor, "r+b")
    try:
        _acquire(handle, blocking=blocking, path=path)
        try:
            yield
        finally:
            _release(handle)
    finally:
        handle.close()


def _acquire(handle, *, blocking: bool, path: Path) -> None:
    if sys.platform == "win32":
        handle.seek(_WINDOWS_LOCK_BYTE)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            msvcrt.locking(handle.fileno(), mode, 1)
        except OSError as error:
            raise LockBusy(f"another writer is active for {path.parent}") from error
        return
    mode = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        fcntl.flock(handle.fileno(), mode)
    except BlockingIOError as error:
        raise LockBusy(f"another writer is active for {path.parent}") from error


def _release(handle) -> None:
    try:
        if sys.platform == "win32":
            handle.seek(_WINDOWS_LOCK_BYTE)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        # A closed or already-released handle is not an error worth failing the caller for.
        pass


def is_locked(path: Path) -> bool:
    """Whether some process currently holds the lock, without keeping it."""
    if not path.exists():
        return False
    try:
        with exclusive(path, blocking=False):
            return False
    except LockBusy:
        return True
