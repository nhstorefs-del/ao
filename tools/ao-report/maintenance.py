"""Atomic, advisory maintenance flag for lane A."""
import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PATH = Path(__file__).with_name('maintenance_flag.json')
VN = timezone(timedelta(hours=7))


def read(path=None):
    try:
        data = json.loads(Path(path or PATH).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def write(data, path=None):
    target = Path(path or PATH)
    fd, name = tempfile.mkstemp(prefix=target.name+'.', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name): os.unlink(name)


def warning(path=None):
    data = read(path)
    if data.get('on') is not True: return None
    try:
        stamp = datetime.fromisoformat(data['at'].replace('Z', '+00:00'))
        if stamp.tzinfo is None: return None
        local = stamp.astimezone(VN).strftime('%d/%m/%Y %H:%M')
        why = ' '.join(str(data['why']).split())
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None
    return f'ĐANG BẢO TRÌ làn A (từ {local}, lý do: {why}) — đừng giao việc mới dùng nền AO'


def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='action', required=True)
    on = sub.add_parser('on')
    on.add_argument('--why', required=True)
    on.add_argument('--by', default='Claude')
    sub.add_parser('off')
    sub.add_parser('show')
    args = parser.parse_args(argv)
    if args.action == 'on':
        write({'on': True, 'at': datetime.now(timezone.utc).isoformat(), 'by': args.by, 'why': args.why})
    elif args.action == 'off':
        write({'on': False})
    print(warning() or 'Bảo trì làn A: tắt')
    return 0


if __name__ == '__main__': raise SystemExit(main())
