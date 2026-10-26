"""Compare protected AO configuration against an approved hash-chain baseline."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from approval_ledger import Ledger, REQUEST, canonical as canonical_request, locked  # noqa: E402
import integrity_ledger as baseline_store  # noqa: E402


def differences(old, new):
    return [{'file': key, 'kind': 'thêm' if key not in old else 'xóa' if key not in new else 'sửa',
             'old': old.get(key), 'new': new.get(key)}
            for key in sorted(old.keys() | new.keys()) if old.get(key) != new.get(key)]


def record_drift(changes, base, path):
    fingerprint = hashlib.sha256(json.dumps(changes, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    path = Path(path)
    with locked(path):
        last = None
        if path.exists():
            for raw in path.read_text(encoding='utf-8').splitlines():
                try:
                    row = json.loads(raw)
                    if row.get('fingerprint') == fingerprint:
                        last = datetime.fromisoformat(row['at'])
                except (ValueError, KeyError, TypeError):
                    continue
        now = datetime.now(timezone.utc)
        if last is not None and now - last < timedelta(minutes=10):
            return
        with path.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps({'at': now.isoformat(), 'baseline_seq': base['seq'],
                                     'fingerprint': fingerprint, 'changes': changes}, ensure_ascii=False) + '\n')


def check(root=baseline_store.ROOT, ledger=baseline_store.DEFAULT_LEDGER, config=None,
          drift=None, brief=False, log_drift=True):
    try:
        rows = baseline_store.read(ledger)
        if not rows:
            raise ValueError('chưa có đường chuẩn')
        base = rows[-1]
        current = baseline_store.snapshot(root, config)
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        print('CẢNH BÁO CẤU HÌNH: chưa có đường chuẩn hoặc sổ hỏng — ' + str(exc))
        return 8
    changes = differences(base['files'], current)
    if not changes:
        if not brief:
            print(f'CẤU HÌNH KHỚP đường chuẩn #{base["seq"]}')
        return 0
    if brief:
        at = datetime.fromisoformat(base['at']).astimezone().strftime('%d/%m %H:%M')
        hooks = sum('/hooks/' in item['file'] for item in changes)
        label = f'{hooks} tệp hook' if hooks == len(changes) else f'{len(changes)} tệp'
        print(f'CẢNH BÁO CẤU HÌNH: {label} lệch so với đường chuẩn {at}')
        print(r'Chạy tools\ao-intake\integrity\integrity_check.py check để xem chi tiết')
    else:
        print(f'CẢNH BÁO CẤU HÌNH: {len(changes)} tệp lệch đường chuẩn #{base["seq"]}')
        for item in changes:
            print(f'{item["kind"]}: {item["file"]}  {(item["old"] or "-")[:12]} -> {(item["new"] or "-")[:12]}')
    if log_drift:
        try:
            record_drift(changes, base, drift or Path(ledger).with_name('drift.jsonl'))
        except (OSError, ValueError) as exc:
            print(f'Không ghi được nhật ký lệch: {exc}', file=sys.stderr)
    return 9


def baseline(root, ledger, reason, *, approval_id=None, request_id=None,
             approval_ledger=None, bootstrap=False, note=None, config=None):
    if not reason or not reason.strip():
        raise ValueError('cần --reason')
    ledger = Path(ledger)
    with locked(ledger):
        rows = baseline_store.read(ledger)
        if bootstrap:
            if rows:
                raise ValueError('--bootstrap chỉ dùng cho đường chuẩn đầu tiên')
            if not note or not note.strip():
                raise ValueError('--bootstrap cần --note ghi căn cứ Owner duyệt')
            approved_by = 'bootstrap-owner'
        else:
            if not rows:
                raise ValueError('đường chuẩn đầu tiên cần --bootstrap sau khi Owner duyệt')
            if not approval_id or not request_id or not REQUEST.fullmatch(request_id):
                raise ValueError('cần --approval-id và --request-id hợp lệ')
            source = Ledger(approval_ledger)
            item = source.get(approval_id)
            if not item or canonical_request(request_id) not in item['request_ids']:
                raise ValueError('thẻ duyệt không đúng yêu cầu')
            if 'APPROVAL BY OWNER' not in item['text'].upper() and 'APROVAL BY OWNER' not in item['text'].upper():
                raise ValueError('thẻ thiếu câu duyệt Owner')
            approval_text = item['text'].casefold()
            if not any(term in approval_text for term in ('đường chuẩn', 'kiem hash', 'kiểm hash', 'integrity baseline')):
                raise ValueError('lời duyệt không nêu cập nhật đường chuẩn kiểm hash')
            # consume verifies expiry, remaining uses, request ID and chain again under its own lock.
            approved_by = approval_id
        files = baseline_store.snapshot(root, config)
        if bootstrap:
            reason = f'{reason.strip()} | bootstrap: {note.strip()}'
        else:
            source.consume(approval_id, 'integrity_baseline', reason.strip(), request_id=request_id)
        return baseline_store.append(files, approved_by, reason, ledger)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Kiểm tra chuỗi băm cấu hình AO')
    parser.add_argument('--root', type=Path, default=baseline_store.ROOT, help=argparse.SUPPRESS)
    parser.add_argument('--ledger', type=Path, default=baseline_store.DEFAULT_LEDGER, help=argparse.SUPPRESS)
    parser.add_argument('--config', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--drift', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--approval-ledger', type=Path, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest='command', required=True)
    pcheck = sub.add_parser('check')
    pcheck.add_argument('--brief', action='store_true')
    pbase = sub.add_parser('baseline')
    pbase.add_argument('--reason', required=True)
    pbase.add_argument('--approval-id')
    pbase.add_argument('--request-id')
    pbase.add_argument('--bootstrap', action='store_true')
    pbase.add_argument('--note')
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except AttributeError:
        pass
    if args.command == 'check':
        return check(args.root, args.ledger, args.config, args.drift, args.brief)
    try:
        row = baseline(args.root, args.ledger, args.reason, approval_id=args.approval_id,
                       request_id=args.request_id, approval_ledger=args.approval_ledger,
                       bootstrap=args.bootstrap, note=args.note, config=args.config)
    except (OSError, ValueError, KeyError, UnicodeError) as exc:
        print(f'TỪ CHỐI ĐƯỜNG CHUẨN: {exc}', file=sys.stderr)
        return 7
    print(f'Đã ghi đường chuẩn #{row["seq"]} ({len(row["files"])} tệp)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
