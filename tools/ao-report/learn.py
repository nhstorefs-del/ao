"""Owner-reviewed responses to exact error signatures."""
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import error_classifier
import reporting

SAFE = {'retry': 'TRANSIENT', 'fallback': 'QUOTA', 'rework': 'QC_FAIL',
        'ignore_benign': 'NEEDS_HUMAN', 'needs_human': 'NEEDS_HUMAN'}
AUDIT = reporting.HERE / 'learned_audit.jsonl'


def _quote(value):
    if not value or re.search(r'\b(không|khong|chưa|chua|not|bác bỏ|bac bo)\s+(?:\w+\s+){0,2}(duyệt|duyet|đồng ý|dong y|cho phép|cho phep|approve)', value, re.I) or not re.search(r'\b(APPROVAL BY OWNER|APROVAL BY OWNER|duyệt|đồng ý|cho phép|approve|ok|thử lại|xác nhận|được)\b', value, re.I):
        raise ValueError('Cần nguyên văn câu Owner có ý duyệt')
    return value


def _write(path, data):
    temp = Path(str(path) + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def resolve(signature, action, owner_quote, note='', auto=False, force=False,
            patterns=error_classifier.PATTERNS, unknown=reporting.UNKNOWN):
    if action not in SAFE: raise ValueError('Hành động ngoài danh sách an toàn')
    owner_quote = _quote(owner_quote)
    records = [x for x in reporting.rows(unknown) if x.get('signature') == signature]
    if not records: raise ValueError('Chữ ký chưa có trong sổ lỗi lạ')
    with reporting.locked(Path(patterns).with_suffix('.lock')):
        data = json.loads(Path(patterns).read_text(encoding='utf-8'))
        learned = data.setdefault('learned', {})
        if signature in learned and not force:
            old = learned[signature]
            if old.get('action') == action and old.get('approved_by_owner_quote') == owner_quote and old.get('auto') == bool(auto):
                return old
            raise ValueError('Chữ ký đã có cách xử lý khác; cần --force và quyết định Owner')
        source = records[-1]
        item = {'id': signature, 'type': SAFE[action], 'action': action, 'auto': bool(auto),
                'state': 'PENDING_CONFIRMATION', 'status': 'PENDING_CONFIRMATION',
                'applied_count': 0, 'approved_by': 'owner',
                'approved_by_owner_quote': owner_quote,
                'approved_at': datetime.now(timezone.utc).isoformat(),
                'source_escalation_id': source.get('escalation_id'),
                'worker': source.get('worker'), 'sample': source.get('reason'), 'note': note}
        learned[signature] = item
        _write(patterns, data)
    reporting.append(AUDIT, {'action': 'resolve', 'id': signature,
                             'at': item['approved_at'], 'owner_quote': owner_quote}, durable=True)
    return item


def change(signature, state, owner_quote, patterns=error_classifier.PATTERNS):
    if not owner_quote or not owner_quote.strip():
        raise ValueError('Thiếu nguyên văn câu Owner')
    if state != 'REJECTED':
        owner_quote = _quote(owner_quote)
    with reporting.locked(Path(patterns).with_suffix('.lock')):
        data = json.loads(Path(patterns).read_text(encoding='utf-8'))
        item = data.get('learned', {}).get(signature)
        if not item: raise ValueError('Không có mục học này')
        if item['state'] != 'PENDING_CONFIRMATION': raise ValueError('Mục không còn chờ xác nhận')
        item['state'] = item['status'] = state
        item['decision_quote'] = owner_quote
        item['decision_at'] = datetime.now(timezone.utc).isoformat()
        _write(patterns, data)
    reporting.append(AUDIT, {'action': state, 'id': signature,
                             'at': item['decision_at'], 'owner_quote': owner_quote}, durable=True)
    return item


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='cmd', required=True)
    q = sub.add_parser('resolve')
    q.add_argument('--signature', required=True); q.add_argument('--action', required=True)
    q.add_argument('--owner-quote', required=True); q.add_argument('--note', default='')
    q.add_argument('--auto', choices=['yes', 'no'], default='no'); q.add_argument('--force', action='store_true')
    for name in ('confirm', 'reject'):
        q = sub.add_parser(name); q.add_argument('id'); q.add_argument('--owner-quote', required=True)
    args = parser.parse_args()
    if args.cmd == 'resolve': item = resolve(args.signature, args.action, args.owner_quote,
                                              args.note, args.auto == 'yes', args.force)
    else: item = change(args.id, 'CONFIRMED' if args.cmd == 'confirm' else 'REJECTED', args.owner_quote)
    print(json.dumps(item, ensure_ascii=False))
