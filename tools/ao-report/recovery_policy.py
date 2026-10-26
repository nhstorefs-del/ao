"""Owner-approved safety rules for automatic recovery."""
import hashlib
import json
from pathlib import Path

import reporting

MAX_TOTAL_RUNS = 3  # Includes the first worker run. Never taken from a brief.
HERE = Path(__file__).resolve().parent
POLICY = HERE / 'recovery_policy.json'
LOCK = HERE / 'policy.lock'


def policy_digest():
    return hashlib.sha256(Path(__file__).read_bytes() + POLICY.read_bytes()).hexdigest()


def verify_lock():
    try:
        record = json.loads(LOCK.read_text(encoding='utf-8'))
        if record.get('sha256') != policy_digest():
            raise ValueError('policy.lock không khớp chính sách; cần Owner xác nhận')
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError('policy.lock thiếu hoặc hỏng; từ chối chạy worker') from exc
    return True


def check(job, action, target, when=None, policy_path=POLICY, ledger=reporting.RECOVERY):
    """No cost ceiling: prices are observations only, never a stop condition."""
    if job.get('requires_owner_approval') or job.get('irreversible') or job.get('spends_money'):
        return False, 'Việc có tác động cần Owner duyệt'
    policy = json.loads(Path(policy_path).read_text(encoding='utf-8'))
    data_class = job.get('data_class', 'sensitive')
    kind = job.get('work_type', 'code')
    if action == 'FALLBACK':
        allowed = policy['fallback_allowlist'].get(kind, {}).get(data_class, [])
        if target not in allowed:
            return False, f'Worker {target} không thuộc allowlist {kind}/{data_class}'
    price = float(policy.get('estimated_cost_usd_per_attempt', {}).get(target, 0))
    return True, {'work_type': kind, 'data_class': data_class, 'estimated_cost_usd': price}
