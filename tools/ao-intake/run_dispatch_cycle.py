"""One serialized intake cycle. No scheduler or resident coordinator."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path

import recover
from dispatcher import Dispatcher
from processes import identity
from store import BASE, DB, Store, now


class CycleLock:
    """Keep a stable inode; the OS releases the byte lock on process death.

    Never unlink the lock file: replacing it would allow two owners to lock
    different files. Metadata is diagnostic, not authority for stealing a lock.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = self.path.open('a+b')
        try:
            f.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self.file = f
        try:
            # Byte zero remains reserved for the OS lock.
            f.seek(0)
            f.truncate()
            f.write(b' ' + json.dumps({'pid': os.getpid(), 'at': now(),
                                     'created': identity(os.getpid())}).encode('utf-8'))
            f.flush()
            os.fsync(f.fileno())
        except Exception:
            self.release()
            raise
        return True

    def release(self):
        if self.file is not None:
            # Closing also releases the OS lock, including on exceptional exit.
            self.file.close()
            self.file = None


def cycle_logger(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.Logger('ao-intake-cycle')
    handler = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=3,
                                 encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    logger.addHandler(handler)
    return logger


def run_cycle(db=DB, base=BASE, scan=None, dispatcher_factory=None):
    base = Path(base)
    # PAUSE must not even create a lock, log, or database.
    if (base / 'PAUSE').exists():
        return 0
    lock = CycleLock(base / 'cycle.lock')
    logger = None
    try:
        if not lock.acquire():
            return 0
        if (base / 'PAUSE').exists():
            return 0
        logger = cycle_logger(base / 'logs' / 'cycle.log')
        logger.info('start pid=%s', os.getpid())
        store = Store(db)
        with store.connect() as c:
            counts = dict(c.execute("SELECT state, COUNT(*) FROM requests "
                                    "WHERE state IN ('RECORDED','RUNNING','CLAIMED') GROUP BY state"))
        if not counts:
            logger.info('idle queued=0 active=0')
            return 0
        dispatcher = (dispatcher_factory or Dispatcher)(store=store, requests_dir=base / 'requests')
        # Reuse step 2 exactly; do not infer recovery from PID alone here.
        try:
            result = (scan or recover.scan)(store=store, apply=True, dispatcher=dispatcher)
            logger.info('recover ok count=%s', len(result))
        except Exception as exc:
            logger.error('recover error=%s', type(exc).__name__)
        try:
            result = dispatcher.once()
            logger.info('dispatch ok count=%s', len(result))
        except Exception as exc:
            logger.error('dispatch error=%s', type(exc).__name__)
        logger.info('end')
    except Exception as exc:
        # No exception text/traceback: it could contain request data or secrets.
        if logger:
            logger.error('cycle error=%s', type(exc).__name__)
        else:
            print('cycle error=' + type(exc).__name__)
    finally:
        if logger:
            for handler in logger.handlers[:]:
                handler.close()
                logger.removeHandler(handler)
        lock.release()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DB,
                        help='Use an isolated database for an empty-queue smoke test')
    parser.add_argument('--base', type=Path, default=BASE,
                        help='Override PAUSE/lock/log/request directory for isolated tests')
    args = parser.parse_args()
    return run_cycle(args.db, args.base)


if __name__ == '__main__':
    raise SystemExit(main())
