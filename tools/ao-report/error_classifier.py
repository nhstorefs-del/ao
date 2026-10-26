"""Pure, ordered classification of AO worker failures."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

PATTERNS = Path(__file__).with_name('error_patterns.json')


def classify(worker, tail='', code=None, missing=(), cause=None, patterns=PATTERNS):
    text = ' '.join((str(cause or ''), str(tail or '')[-4000:])).lower()
    table = json.loads(Path(patterns).read_text(encoding='utf-8'))
    human_hit = next((p for p in table['NEEDS_HUMAN']['patterns'] if p in text), None)
    if human_hit:
        kind, matched = 'NEEDS_HUMAN', human_hit
    elif code == 0 and missing:
        kind = 'QC_FAIL' if any(str(x).startswith(('cmd:', 'qc:')) for x in missing) else 'FALSE_DONE'
        matched = 'missing result' if kind == 'FALSE_DONE' else 'failed signal'
    else:
        kind, matched = 'UNKNOWN', ''
        for category in ('NEEDS_HUMAN', 'QUOTA', 'TRANSIENT', 'QC_FAIL'):
            hit = next((p for p in table[category]['patterns'] if p in text), None)
            if hit:
                kind, matched = category, hit
                break
    if cause and ('treo:' in cause or cause.startswith('MAX_RUNTIME:')):
        kind, matched = 'TRANSIENT', 'stall or runtime'
    normalized = re.sub(r'\b\d+\b', '#', matched or re.sub(r'\s+', ' ', text[-100:]))[:100]
    digest = hashlib.sha256(f'{kind}|{worker}|{normalized}'.encode()).hexdigest()[:16]
    learned = table.get('learned', {}).get(digest)
    approved = None
    if learned and learned.get('auto'):
        state = learned.get('state', 'PENDING_CONFIRMATION')
        created = datetime.fromisoformat(learned.get('approved_at'))
        pending = state == 'PENDING_CONFIRMATION'
        permitted = state == 'CONFIRMED' or (pending and learned.get('action') in {'retry', 'ignore_benign'}
                    and int(learned.get('applied_count', 0)) < 3
                    and datetime.now(timezone.utc) - created <= timedelta(days=7))
        if permitted:
            approved = learned['action']
            kind = learned['type']
            if pending:
                from reporting import locked
                with locked(Path(patterns).with_suffix('.lock')):
                    latest = json.loads(Path(patterns).read_text(encoding='utf-8'))
                    item = latest.get('learned', {}).get(digest)
                    if item and item.get('state') == 'PENDING_CONFIRMATION' and int(item.get('applied_count', 0)) < 3:
                        item['applied_count'] = int(item.get('applied_count', 0)) + 1
                        temporary = Path(str(patterns) + '.tmp')
                        temporary.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding='utf-8')
                        temporary.replace(patterns)
                    else:
                        approved = None
        elif pending and datetime.now(timezone.utc) - created > timedelta(days=7):
            # Expiration is explicit in the table so the weekly digest can show it.
            from reporting import locked
            with locked(Path(patterns).with_suffix('.lock')):
                latest = json.loads(Path(patterns).read_text(encoding='utf-8'))
                if digest in latest.get('learned', {}):
                    latest['learned'][digest]['state'] = 'EXPIRED'
                    latest['learned'][digest]['status'] = 'EXPIRED'
                    temporary = Path(str(patterns) + '.tmp')
                    temporary.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding='utf-8')
                    temporary.replace(patterns)
            kind = 'NEEDS_HUMAN'
        elif pending and int(learned.get('applied_count', 0)) >= 3:
            kind = 'NEEDS_HUMAN'
    return {'type': kind, 'signature': digest, 'matched': matched,
            'known_fix': table.get(kind, {}).get('known_fix', ''),
            'approved_action': approved}


def action(kind, same_worker_attempts, fallback_available, max_retries):
    if kind == 'NEEDS_HUMAN':
        return 'NEEDS_HUMAN'
    if same_worker_attempts >= max_retries:
        return 'ESCALATE'
    if kind == 'QUOTA':
        return 'FALLBACK' if fallback_available else 'ESCALATE'
    if kind == 'FALSE_DONE':
        return 'RETRY' if same_worker_attempts < 1 else 'ESCALATE'
    if kind == 'UNKNOWN':
        return 'RETRY' if same_worker_attempts < 1 else 'ESCALATE'
    return 'RETRY' if same_worker_attempts < 2 else 'ESCALATE'
