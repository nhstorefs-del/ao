"""Audited Owner-only counter reset and policy relock."""
import argparse
import json
from datetime import datetime, timezone

import recovery_policy
import reporting
import run_counter


def owner_quote(value):
    if not value or not value.strip():
        raise ValueError('Thiếu nguyên văn câu Owner')
    return value.strip()


def reset(key, quote, path=run_counter.STATE):
    quote = owner_quote(quote)
    with reporting.locked(path.with_suffix('.lock')):
        data = run_counter.read(path)
        if key not in data['groups']: raise ValueError('Không có khóa bộ đếm')
        previous = data['groups'][key]['runs_total']
        data['groups'][key]['runs_total'] = 0
        data['groups'][key]['runs'] = []
        run_counter._save(path, data)
    reporting.append(path.with_name('counter_audit.jsonl'),
                     {'at': datetime.now(timezone.utc).isoformat(), 'action': 'reset',
                      'key': key, 'previous': previous, 'owner_quote': quote}, durable=True)


def set_policy(quote):
    quote = owner_quote(quote)
    record = {'sha256': recovery_policy.policy_digest(),
              'approved_by_owner_quote': quote,
              'at': datetime.now(timezone.utc).isoformat()}
    run_counter._save(recovery_policy.LOCK, record)
    reporting.append(run_counter.STATE.with_name('counter_audit.jsonl'),
                     {'action': 'set-policy', **record}, durable=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    q = sub.add_parser('reset'); q.add_argument('key'); q.add_argument('--owner-quote', required=True)
    q = sub.add_parser('set-policy'); q.add_argument('--owner-quote', required=True)
    args = parser.parse_args()
    if args.command == 'reset': reset(args.key, args.owner_quote)
    else: set_policy(args.owner_quote)
