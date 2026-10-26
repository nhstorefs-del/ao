#!/usr/bin/env python3
"""Evaluate the completion signals declared by a dispatched task."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from journal import DEFAULT_JOURNAL, _read_records


COMMAND_PATTERN = re.compile(r"^cmd:(?P<command>.+)\s+exit=(?P<exit>-?\d+)\s*$", re.DOTALL)


def _check_signal(signal: str) -> tuple[bool, str]:
    if signal.startswith("file:"):
        raw_path = signal[5:].strip()
        if raw_path.endswith(" exists"):
            raw_path = raw_path[:-7].rstrip()
        if not raw_path:
            return False, "thiếu đường dẫn"
        path = Path(raw_path).expanduser()
        return path.exists(), f"exists={path.exists()} path={path.resolve()}"

    if COMMAND_PATTERN.match(signal):
        return False, "COMMAND_SIGNAL_DISABLED_BY_DELEGATION_GATE"
    return False, "invalid signal"


def evaluate_signals(signals: list[str]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Measure each dispatch signal once and retain evidence for acceptance."""
    evidence: list[dict[str, Any]] = []
    output_hashes: dict[str, str] = {}
    for signal in signals:
        item: dict[str, Any] = {"signal": signal, "passed": False}
        if signal.startswith("file:"):
            raw_path = signal[5:].strip()
            if raw_path.endswith(" exists"):
                raw_path = raw_path[:-7].rstrip()
            if raw_path:
                path = Path(raw_path).expanduser().resolve()
                if path.is_file():
                    try:
                        output_hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
                        item["passed"] = True
                    except OSError:
                        pass
        else:
            item["refusal"] = "COMMAND_SIGNAL_DISABLED_BY_DELEGATION_GATE"
        evidence.append(item)
    return evidence, output_hashes


def check_task(task_id: str, journal_path: Path | str = DEFAULT_JOURNAL) -> bool:
    records = _read_records(Path(journal_path))
    dispatch = next(
        (
            row
            for row in reversed(records)
            if row.get("task_id") == task_id and row.get("status") == "dispatched"
        ),
        None,
    )
    if dispatch is None:
        print(f"ERROR: không tìm thấy receipt dispatched cho task {task_id}")
        return False
    signals = dispatch.get("completion_signals", [])
    if not isinstance(signals, list) or not signals:
        print(f"FAIL task={task_id}: không có completion_signals")
        return False

    evidence, _ = evaluate_signals([str(signal) for signal in signals])
    all_passed = all(item["passed"] for item in evidence)
    for index, item in enumerate(evidence, 1):
        state = "PASS" if item["passed"] else "FAIL"
        detail = f" exit={item['exit_code']}" if "exit_code" in item else ""
        print(f"[{state}] signal {index}: {item['signal']}{detail}")
    print(f"CHECK_COMPLETION {'PASS' if all_passed else 'FAIL'}: task={task_id}")
    return all_passed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kiểm tra completion signals của task.")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--journal", default=str(DEFAULT_JOURNAL), help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    args = parse_args()
    try:
        return 0 if check_task(args.task_id, args.journal) else 1
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
