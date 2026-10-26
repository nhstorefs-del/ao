"""Vietnamese summary of direct worker invocations in the last 24 hours."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from directrun_lib import safe_command

BASE = Path(__file__).resolve().parent


def summary(path: Path = BASE / 'data' / 'directrun.jsonl', now=None) -> str:
    now = now or datetime.now(timezone.utc)
    rows = []
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                row = json.loads(line)
                at = datetime.fromisoformat(row['at'].replace('Z', '+00:00'))
                if row.get('source') == 'live' and now - timedelta(hours=24) <= at <= now:
                    rows.append(row)
            except (ValueError, KeyError, TypeError):
                continue
    tools = Counter(str(r.get('tool') or '?') for r in rows)
    sessions = Counter(str(r.get('session_id') or '?') for r in rows)
    lines = [f'24 giờ qua: {len(rows)} lần gọi thẳng thợ.',
             'Theo công cụ: ' + (', '.join(f'{k} {v}' for k, v in tools.most_common()) or 'không có'),
             'Theo phiên: ' + (', '.join(f'{k} {v}' for k, v in sessions.most_common()) or 'không có')]
    for r in rows[-5:]:
        lines.append(f"Ví dụ {r.get('at', '')[:16]}: {safe_command(str(r.get('command') or ''))}")
    return '\n'.join(lines)


if __name__ == '__main__':
    print(summary())
