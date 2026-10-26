"""Daily notification timing from the durable send log."""
import json
from datetime import datetime

import reporting


def calculate(jobs=reporting.JOBS, sent=reporting.SENT, clock=reporting.now):
    today = clock().astimezone(reporting.BASELINE.tzinfo).date()
    latest = {}
    for row in reporting.rows(sent):
        latest[(row.get('id'), row.get('attempt'))] = row
    done = []
    for path in jobs.glob('*'):
        status = reporting.read(path / 'status.json')
        if status.get('state') not in reporting.STATES or not status.get('attempt_id'):
            continue
        if reporting.legacy(path, status['attempt_id']):
            continue
        finished = reporting.parse(status.get('finished_at'))
        if finished and finished.astimezone(reporting.BASELINE.tzinfo).date() == today:
            done.append((path.name, status['attempt_id'], finished))
    delays = []
    for ident, attempt, finished in done:
        row = latest.get((ident, attempt))
        if row and row.get('ok'):
            at = reporting.parse(row.get('at'))
            if at:
                delays.append(max(0, (at-finished).total_seconds()/60))
    result = {'date': today.isoformat(), 'finished_today': len(done),
              'notified_within_5_minutes': sum(x <= 5 for x in delays),
              'average_delay_minutes': round(sum(delays)/len(delays), 2) if delays else None,
              'maximum_delay_minutes': round(max(delays), 2) if delays else None,
              'not_notified': len(done)-len(delays)}
    return result


if __name__ == '__main__':
    result = calculate()
    (reporting.HERE / 'metrics.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
