"""Reversible mute for AO notifications only; paths follow the caller module."""
import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


def is_muted(root=None):
    flag = Path(root if root is not None else HERE) / 'telegram_muted.json'
    try:
        value = json.loads(flag.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or type(value.get('on')) is not bool:
            return True
        return value['on']
    except FileNotFoundError:
        return False
    except (OSError, ValueError, UnicodeError):
        return True


def record_if_muted(text, root=None):
    root = Path(root if root is not None else HERE)
    if not is_muted(root):
        return False
    row = {'at': datetime.now(timezone.utc).isoformat(),
           'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(), 'text': text}
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / 'muted_outbox.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        # Never fall through to Telegram when the local audit disk fails.
        pass
    return True


def set_muted(on, root=None):
    root = Path(root if root is not None else HERE)
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'telegram_muted.json'
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=root)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'on': bool(on)}, stream, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('on', 'off', 'show'))
    args = parser.parse_args()
    if args.mode != 'show':
        set_muted(args.mode == 'on')
    print(json.dumps({'on': is_muted()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
