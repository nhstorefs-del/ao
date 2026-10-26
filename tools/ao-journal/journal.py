#!/usr/bin/env python3
"""Append-only task journal protected by a SHA-256 hash chain."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from file_lock import locked_paths
import delegation_gate


DEFAULT_JOURNAL = Path(__file__).with_name("journal.jsonl")
GENESIS = "GENESIS"


def _canonical(record: dict[str, Any]) -> bytes:
    payload = {key: value for key, value in record.items() if key != "this_hash"}
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _record_hash(record: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(record)).hexdigest()


def _read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            value = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Dòng {line_number} không phải JSON hợp lệ: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"Dòng {line_number} không phải JSON object")
        records.append(value)
    return records


def _append_record(record: dict[str, Any], path: Path) -> dict[str, Any]:
    records = _read_records(path)
    record["seq"] = len(records) + 1
    record["ts"] = datetime.now(timezone.utc).astimezone().isoformat()
    record["prev_hash"] = records[-1].get("this_hash", "") if records else GENESIS
    record["this_hash"] = _record_hash(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return record


def _task_history(records: list[dict[str, Any]], task_id: str) -> list[dict[str, Any]]:
    return [row for row in records if row.get("task_id") == task_id]


def _latest_attempt(history: list[dict[str, Any]], task_id: str) -> tuple[dict[str, Any] | None, str | None]:
    dispatches = [row for row in history if row.get("status") == "dispatched"]
    if not dispatches:
        return None, None
    dispatch = dispatches[-1]
    return dispatch, dispatch.get("attempt_id") or f"{task_id}#{len(dispatches)}"


def _attempt_status(history: list[dict[str, Any]], dispatch: dict[str, Any]) -> str:
    for row in reversed(history):
        if row.get("seq", 0) <= dispatch.get("seq", 0):
            break
        if row.get("status") in {"done", "failed", "accepted"}:
            return str(row["status"])
    return "dispatched"


def _is_stale(dispatch: dict[str, Any], hours: float) -> bool:
    try:
        timestamp = datetime.fromisoformat(dispatch["ts"])
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc) >= timedelta(hours=hours)
    except (KeyError, TypeError, ValueError):
        return False


@locked_paths("journal_path")
def append(
    task_id: str,
    worker: str,
    brief_path: Path | str,
    completion_signals: list[str],
    journal_path: Path | str = DEFAULT_JOURNAL,
    retry: bool = False,
    reason: str | None = None,
    stale_hours: float | None = None,
    dispatched_by: str | None = None,
    spec_version: int | None = None,
    spec_hash: str | None = None,
    worker_switched_from: str | None = None,
    worker_switch_reason: str | None = None,
) -> dict[str, Any]:
    path = Path(journal_path)
    if stale_hours is not None and stale_hours < 0:
        raise ValueError("STALE_HOURS_INVALID")
    history = _task_history(_read_records(path), task_id)
    dispatch, previous_id = _latest_attempt(history, task_id)
    if any(row.get("status") == "accepted" for row in history):
        raise ValueError(f"ALREADY_ACCEPTED task={task_id}")
    if retry and not (reason and reason.strip()):
        raise ValueError(f"REASON_REQUIRED task={task_id}")
    if dispatch is not None:
        status = _attempt_status(history, dispatch)
        if status == "dispatched" and not (retry and stale_hours is not None and _is_stale(dispatch, stale_hours)):
            raise ValueError(f"ATTEMPT_IN_FLIGHT task={task_id}")
        if not retry:
            raise ValueError(f"RETRY_REQUIRED task={task_id}")
        if status != "failed" and not (status == "dispatched" and stale_hours is not None and _is_stale(dispatch, stale_hours)):
            raise ValueError(f"RETRY_REQUIRED task={task_id}")
    brief = Path(brief_path).expanduser().resolve(strict=True)
    brief_hash = hashlib.sha256(brief.read_bytes()).hexdigest()
    attempt_number = sum(row.get("status") == "dispatched" for row in history) + 1
    receipt = {
            "task_id": task_id,
            "worker": worker,
            "brief_path": str(brief),
            "brief_hash": brief_hash,
            "attempt_id": f"{task_id}#{attempt_number}",
            "idempotency_key": hashlib.sha256((task_id + brief_hash).encode("utf-8")).hexdigest(),
            "completion_signals": list(completion_signals),
            "status": "dispatched",
    }
    if dispatch is not None:
        receipt["retry_of"] = previous_id
        receipt["retry_reason"] = reason.strip()
    if dispatched_by is not None:
        receipt["dispatched_by"] = dispatched_by
    if spec_version is not None:
        receipt["spec_version"] = spec_version
        receipt["spec_hash"] = spec_hash
    if worker_switched_from is not None:
        receipt["worker_switched_from"] = worker_switched_from
        receipt["worker_switch_reason"] = worker_switch_reason
    decision = delegation_gate.prepare(path, receipt['attempt_id'], worker, brief, dispatched_by or 'supervisor')
    receipt['delegation_journal'] = str(path.resolve())
    receipt['delegation_gate_version'] = delegation_gate.VERSION
    receipt['delegation_binding'] = decision['binding']
    return _append_record(receipt, path)


@locked_paths("journal_path")
def complete(
    task_id: str,
    result_summary: str,
    signals_passed: bool,
    difficulty: str | None = None,
    cost_tokens: int | None = None,
    quality: str | None = None,
    journal_path: Path | str = DEFAULT_JOURNAL,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    path = Path(journal_path)
    if not attempt_id:
        raise ValueError(f"ATTEMPT_ID_REQUIRED task={task_id}")
    history = _task_history(_read_records(path), task_id)
    dispatch, current_id = _latest_attempt(history, task_id)
    if dispatch is None or attempt_id != current_id or _attempt_status(history, dispatch) != "dispatched":
        raise ValueError(f"STALE_RESULT task={task_id} attempt={attempt_id}")
    record: dict[str, Any] = {
        "task_id": task_id,
        "attempt_id": current_id,
        "result_summary": result_summary,
        "signals_passed": signals_passed,
        "status": "done" if signals_passed else "failed",
    }
    if difficulty is not None:
        record["difficulty"] = difficulty
    if cost_tokens is not None:
        record["cost_tokens"] = cost_tokens
    if quality is not None:
        record["quality"] = quality
    return _append_record(record, path)


@locked_paths("journal_path")
def accept(
    task_id: str,
    accepted_by: str,
    note: str | None = None,
    journal_path: Path | str = DEFAULT_JOURNAL,
    guard_report: Path | str | None = None,
) -> dict[str, Any] | None:
    """Accept a completed task only after every verification gate passes."""
    from check_completion import evaluate_signals

    path = Path(journal_path)
    records = _read_records(path)
    task_records = [row for row in records if row.get("task_id") == task_id]
    latest = next((row for row in reversed(task_records) if row.get("status") in {"dispatched", "done", "failed", "accepted"}), None)
    if any(row.get("status") == "accepted" for row in task_records):
        print(f"ALREADY_ACCEPTED task={task_id}")
        return None
    if latest is None or latest.get("status") != "done":
        raise ValueError(f"NOT_DONE task={task_id}")
    dispatch, attempt_id = _latest_attempt(task_records, task_id)
    if dispatch is None or latest.get("seq", 0) <= dispatch.get("seq", 0):
        raise ValueError(f"NOT_DONE task={task_id}")
    signals = dispatch.get("completion_signals") if dispatch else None
    if not isinstance(signals, list) or not signals:
        raise ValueError(f"NO_SIGNALS task={task_id}")
    if not verify(path, quiet=True):
        raise ValueError(f"CHAIN_BROKEN task={task_id}")
    brief_hash = _current_hash(dispatch.get("brief_path"))
    if brief_hash != dispatch.get("brief_hash"):
        raise ValueError(f"BRIEF_CHANGED task={task_id}")
    if guard_report is not None:
        try:
            guard_lines = Path(guard_report).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            guard_lines = []
        guard_results = [line.strip() for line in guard_lines if "GUARD_RESULT:" in line]
        if not guard_results or re.fullmatch(r"GUARD_RESULT: PASS(?: exit_code=-?\d+)?", guard_results[-1]) is None:
            raise ValueError(f"GUARD_NOT_PASS task={task_id}")
    verified = next((row for row in reversed(task_records)
                     if row.get("status") == "verified" and row.get("seq", 0) > latest["seq"]
                     and row.get("attempt_id", attempt_id) == attempt_id), None)
    if verified is not None:
        if not verified.get("passed"):
            raise ValueError(f"SIGNAL_FAILED task={task_id}")
        for name, old_hash in verified.get("output_hashes", {}).items():
            if _current_hash(name) != old_hash:
                raise ValueError(f"OUTPUT_CHANGED task={task_id}")
    evidence, output_hashes = evaluate_signals([str(signal) for signal in signals])
    if not all(item["passed"] for item in evidence):
        if verified is not None and any(not Path(name).is_file() for name in verified.get("output_hashes", {})):
            raise ValueError(f"OUTPUT_CHANGED task={task_id}")
        raise ValueError(f"SIGNAL_FAILED task={task_id}")
    if verified is None:
        verified = verify_task(task_id, path)
        if not verified["passed"]:
            raise ValueError(f"SIGNAL_FAILED task={task_id}")
    if verified.get("brief_hash_at_verify") != brief_hash:
        raise ValueError(f"BRIEF_CHANGED task={task_id}")
    if output_hashes != verified.get("output_hashes", {}):
        raise ValueError(f"OUTPUT_CHANGED task={task_id}")
    if not verify(path, quiet=True):
        raise ValueError(f"CHAIN_BROKEN task={task_id}")
    return _append_record(
        {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "status": "accepted",
            "accepted_by": accepted_by,
            "note": note or "",
            "evidence": evidence,
            "output_hashes": output_hashes,
            "verified_seq": verified["seq"],
            "gate": ["DONE", "SIGNALS", "BRIEF_HASH", "OUTPUT_HASHES", "CHAIN", "GUARD" if guard_report is not None else "NO_GUARD_REQUIRED"],
        },
        path,
    )


def _current_hash(raw_path: Any) -> str | None:
    try:
        return hashlib.sha256(Path(raw_path).read_bytes()).hexdigest()
    except (OSError, TypeError, ValueError):
        return None


@locked_paths("journal_path")
def verify_task(task_id: str, journal_path: Path | str = DEFAULT_JOURNAL) -> dict[str, Any]:
    """Record measured evidence without changing the task's workflow status."""
    from check_completion import evaluate_signals

    path = Path(journal_path)
    records = _read_records(path)
    history = _task_history(records, task_id)
    latest = next((row for row in reversed(history)
                   if row.get("status") in {"dispatched", "done", "failed", "accepted"}), None)
    if latest is None or latest.get("status") != "done":
        raise ValueError(f"NOT_DONE task={task_id}")
    dispatch, attempt_id = _latest_attempt(history, task_id)
    if dispatch is None or latest.get("seq", 0) <= dispatch.get("seq", 0):
        raise ValueError(f"NOT_DONE task={task_id}")
    signals = dispatch.get("completion_signals") if dispatch else None
    if not isinstance(signals, list) or not signals:
        raise ValueError(f"NO_SIGNALS task={task_id}")
    if not verify(path, quiet=True):
        raise ValueError(f"CHAIN_BROKEN task={task_id}")
    evidence, output_hashes = evaluate_signals([str(signal) for signal in signals])
    receipt = {"task_id": task_id, "attempt_id": attempt_id, "status": "verified", "passed": all(item["passed"] for item in evidence),
               "evidence": evidence, "output_hashes": output_hashes,
               "brief_hash_at_verify": _current_hash(dispatch.get("brief_path"))}
    return _append_record(receipt, path)


@locked_paths("journal_path")
def checkpoint(*, task_id: str | None, workflow: str | None, by: str,
               next_action: str, context_refs: list[str], journal_path: Path | str = DEFAULT_JOURNAL,
               blocked_reason: str | None = None, approval_ref: str | None = None) -> dict[str, Any]:
    resolved = []
    for raw in context_refs:
        path = Path(raw).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"CONTEXT_REF_MISSING path={raw}")
        resolved.append(str(path))
    receipt: dict[str, Any] = {"status": "checkpoint", "by": by,
                               "next_action": next_action, "context_refs": resolved,
                               "blocked_reason": blocked_reason, "approval_ref": approval_ref}
    receipt["task_id" if task_id is not None else "workflow"] = task_id if task_id is not None else workflow
    return _append_record(receipt, Path(journal_path))


@locked_paths("journal_path")
def verify(journal_path: Path | str = DEFAULT_JOURNAL, quiet: bool = False) -> bool:
    path = Path(journal_path)
    try:
        records = _read_records(path)
    except (OSError, ValueError) as exc:
        if not quiet:
            print(f"VERIFY FAIL: {exc}")
        return False

    expected_previous = GENESIS
    valid = True
    for line_number, record in enumerate(records, 1):
        if record.get("seq") != line_number:
            if not quiet:
                print(
                    f"VERIFY FAIL dòng {line_number}: seq={record.get('seq')!r}, "
                    f"mong đợi {line_number}"
                )
            valid = False
        if record.get("prev_hash") != expected_previous:
            if not quiet:
                print(
                    f"VERIFY FAIL dòng {line_number}: prev_hash không khớp "
                    f"(mong đợi {expected_previous})"
                )
            valid = False
        expected_hash = _record_hash(record)
        if record.get("this_hash") != expected_hash:
            if not quiet:
                print(
                    f"VERIFY FAIL dòng {line_number}: this_hash không khớp "
                    f"(mong đợi {expected_hash})"
                )
            valid = False
        expected_previous = str(record.get("this_hash", ""))

    if valid and not quiet:
        print(f"VERIFY PASS: {len(records)} dòng, chuỗi hash nguyên vẹn")
    return valid


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Nhật ký điều phối AO có hash-chain.")
    parser.add_argument(
        "--journal", default=str(DEFAULT_JOURNAL), help=argparse.SUPPRESS
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    append_parser = subparsers.add_parser("append")
    append_parser.add_argument("--task-id", required=True)
    append_parser.add_argument("--worker", required=True)
    append_parser.add_argument("--brief", required=True)
    append_parser.add_argument("--signal", action="append", default=[])
    append_parser.add_argument("--retry", action="store_true")
    append_parser.add_argument("--reason")
    append_parser.add_argument("--stale-hours", type=float)

    complete_parser = subparsers.add_parser("complete")
    complete_parser.add_argument("--task-id", required=True)
    complete_parser.add_argument("--attempt-id", required=True)
    complete_parser.add_argument("--status", choices=("done", "failed"), required=True)
    complete_parser.add_argument("--summary", required=True)
    complete_parser.add_argument("--difficulty", choices=("easy", "medium", "hard"))
    complete_parser.add_argument("--cost-tokens", type=int)
    complete_parser.add_argument("--quality", choices=("good", "partial", "bad"))

    accept_parser = subparsers.add_parser("accept")
    accept_parser.add_argument("--task-id", required=True)
    accept_parser.add_argument("--by", required=True)
    accept_parser.add_argument("--note")
    accept_parser.add_argument("--guard-report")

    verify_task_parser = subparsers.add_parser("verify-task")
    verify_task_parser.add_argument("--task-id", required=True)

    checkpoint_parser = subparsers.add_parser("checkpoint")
    checkpoint_target = checkpoint_parser.add_mutually_exclusive_group(required=True)
    checkpoint_target.add_argument("--task-id")
    checkpoint_target.add_argument("--workflow")
    checkpoint_parser.add_argument("--by", required=True)
    checkpoint_parser.add_argument("--next-action", required=True)
    checkpoint_parser.add_argument("--blocked-reason")
    checkpoint_parser.add_argument("--approval-ref")
    checkpoint_parser.add_argument("--context-ref", action="append", default=[])

    subparsers.add_parser("verify")
    return parser.parse_args()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    args = parse_args()
    journal_path = Path(args.journal)
    try:
        if args.command == "append":
            receipt = append(
                args.task_id, args.worker, args.brief, args.signal, journal_path,
                args.retry, args.reason, args.stale_hours,
            )
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
        elif args.command == "complete":
            receipt = complete(
                args.task_id,
                args.summary,
                args.status == "done",
                args.difficulty,
                args.cost_tokens,
                args.quality,
                journal_path,
                args.attempt_id,
            )
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
        elif args.command == "accept":
            receipt = accept(args.task_id, args.by, args.note, journal_path, args.guard_report)
            if receipt is not None:
                print(json.dumps(receipt, ensure_ascii=False, indent=2))
        elif args.command == "verify-task":
            print(json.dumps(verify_task(args.task_id, journal_path), ensure_ascii=False, indent=2))
        elif args.command == "checkpoint":
            receipt = checkpoint(task_id=args.task_id, workflow=args.workflow, by=args.by,
                                 next_action=args.next_action, context_refs=args.context_ref,
                                 journal_path=journal_path, blocked_reason=args.blocked_reason,
                                 approval_ref=args.approval_ref)
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
        else:
            return 0 if verify(journal_path) else 1
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
