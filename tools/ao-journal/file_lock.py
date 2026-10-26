"""Small, reentrant interprocess file locks for journal and queue operations."""

from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
from inspect import signature
import os
from pathlib import Path
import random
import threading
import time
from typing import Iterator
from uuid import uuid4


_held = threading.local()
_TIMEOUT = 10.0
_STALE_AFTER = 60.0


@contextmanager
def file_lock(path: Path | str) -> Iterator[None]:
    target = Path(path).expanduser().resolve()
    locks = getattr(_held, "locks", None)
    if locks is None:
        locks = _held.locks = {}
    if target in locks:
        locks[target] += 1
        try:
            yield
        finally:
            locks[target] -= 1
        return

    lock_path = Path(str(target) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _TIMEOUT
    token = f"{os.getpid()} {time.time()} {uuid4().hex}\n"
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            try:
                observed = lock_path.stat()
                if time.time() - observed.st_mtime > _STALE_AFTER:
                    # Recheck identity before reclaiming; an owner running longer
                    # than the stale threshold can still be displaced.
                    current = lock_path.stat()
                    if (current.st_ino, current.st_mtime_ns) == (observed.st_ino, observed.st_mtime_ns):
                        lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise ValueError(f"LOCK_TIMEOUT path={lock_path}")
            time.sleep(0.02 + random.uniform(0, 0.03))
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(token)
        locks[target] = 1
        try:
            yield
        finally:
            del locks[target]
    finally:
        # Do not delete a lock that replaced this one after stale recovery.
        try:
            if lock_path.read_text(encoding="ascii") == token:
                lock_path.unlink()
        except FileNotFoundError:
            pass


def locked_paths(*names: str):
    """Lock named path arguments in the supplied (journal, queue) order."""
    def decorate(function):
        parameters = signature(function)

        @wraps(function)
        def wrapped(*args, **kwargs):
            bound = parameters.bind(*args, **kwargs)
            bound.apply_defaults()
            from contextlib import ExitStack
            with ExitStack() as stack:
                for name in names:
                    stack.enter_context(file_lock(bound.arguments[name]))
                return function(*args, **kwargs)

        return wrapped
    return decorate
