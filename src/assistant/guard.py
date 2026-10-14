"""Startup guard. M3: the single-run lock. Billing and scope checks arrive in M6."""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from assistant.config import FILE_MODE

LOCK_FILE = ".run.lock"


class RunLockedError(Exception):
    def __init__(self) -> None:
        super().__init__("another run is in progress")


@contextmanager
def run_lock(data_dir: Path) -> Iterator[None]:
    """Hold an exclusive `fcntl` lock on `data/.run.lock`, or fail at once.

    The OS releases the lock if the process dies, so a crash never leaves a
    stale lock. The file itself stays; only the lock on it matters.
    """
    fd = os.open(data_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT, FILE_MODE)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunLockedError() from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
