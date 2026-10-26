#!/usr/bin/env python3
"""Small, foreground-only dependency queue for AO tasks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from journal import DEFAULT_JOURNAL, _is_stale, _read_records, _append_record, append, verify
from file_lock import locked_paths


DEFAULT_QUEUE = Path(__file__).with_name("queue.json")
DEFAULT_APPROVALS_LOG = Path(os.environ.get("AO_APPROVALS_LOG", str(Path(__file__).resolve().parent / "data" / "owner_approvals_log.md")))
VALID_STATUSES = {"waiting", "ready", "dispatched", "done", "failed", "accepted"}
JOURNAL_STATUSES = {"dispatched", "done", "failed", "accepted"}


def _load_queue(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("queue.json phải là một JSON array")
    return data


def _save_queue(path: Path, tasks: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(tasks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _spec_hash(worker: str, brief: str, signals: list[str], fallback_workers: list[str] | None = None) -> str:
    payload = {"worker": worker, "brief": brief, "signals": signals}
    if fallback_workers:
        payload["fallback_workers"] = fallback_workers
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _has_spec(task: dict[str, Any]) -> bool:
    return all(task.get(key) for key in ("worker", "brief", "signals", "spec_hash"))


def _current_hash(task: dict[str, Any]) -> str:
    return _spec_hash(task["worker"], task["brief"], task["signals"],
                      task.get("fallback_workers") if "spec_version" in task else None)


def _valid_signals(signals: list[str]) -> bool:
    return bool(signals) and all(isinstance(s, str) and
        ((s.startswith("file:") and bool(s[5:].strip())) or
         bool(re.fullmatch(r"cmd:.+\s+exit=-?\d+\s*", s, re.DOTALL))) for s in signals)


def _task(tasks: list[dict[str, Any]], task_id: str) -> dict[str, Any]:
    task = next((row for row in tasks if row.get("id") == task_id), None)
    if task is None:
        raise ValueError(f"TASK_NOT_FOUND task={task_id}")
    return task


def _editable(task: dict[str, Any], journal_path: Path) -> None:
    status = next(row for row in state_for_task(task, journal_path)["tasks"] if row["id"] == task["id"])["status"]
    if status in {"dispatched", "done", "accepted"}:
        raise ValueError(f"TASK_BUSY task={task['id']} status={status}")


def state_for_task(task: dict[str, Any], journal_path: Path) -> dict[str, Any]:
    # A local projection avoids writing or reloading the queue during a locked edit.
    records = _read_records(journal_path)
    statuses = [r["status"] for r in records if r.get("task_id") == task["id"] and r.get("status") in JOURNAL_STATUSES]
    status = statuses[-1] if statuses else task.get("status")
    if status == "waiting" and not task.get("depends_on"):
        status = "ready"
    return {"tasks": [{"id": task["id"], "status": status}]}


def _version(task: dict[str, Any], *, by: str, reason: str, change: str,
             amendment_id: int | None = None) -> None:
    task["spec_version"] += 1
    task["spec_hash"] = _current_hash(task)
    task["spec_history"].append({"version": task["spec_version"], "worker": task["worker"],
        "brief": task["brief"], "signals": list(task["signals"]),
        "fallback_workers": list(task["fallback_workers"]), "spec_hash": task["spec_hash"],
        "created_by": by, "reason": reason, "amendment_id": amendment_id, "change": change})


def _actions(status: str, has_spec: bool) -> list[str]:
    if status == "ready":
        return ["dispatch"] if has_spec else []
    return {"done": ["verify", "accept"], "failed": ["retry"]}.get(status, [])


@locked_paths("queue_path")
def add_task(task_id: str, title: str, depends_on: list[str], queue_path: Path,
             workflow: str | None = None, worker: str | None = None,
             brief: str | None = None, signals: list[str] | None = None,
             fallback_workers: list[str] | None = None) -> None:
    tasks = _load_queue(queue_path)
    if any(task.get("id") == task_id for task in tasks):
        raise ValueError(f"Task đã tồn tại: {task_id}")
    if task_id in depends_on:
        raise ValueError("Task không thể phụ thuộc chính nó")
    task: dict[str, Any] = {"id": task_id, "title": title, "depends_on": depends_on, "status": "waiting"}
    if workflow is not None:
        task["workflow"] = workflow
    signals = signals or []
    fallback_workers = fallback_workers or []
    if worker is not None or brief is not None or signals:
        if not worker or not brief or not signals:
            raise ValueError("SPEC_INCOMPLETE")
        if not _valid_signals(signals):
            raise ValueError("SIGNAL_INVALID")
        resolved = str(Path(brief).expanduser().resolve(strict=True))
        task.update(worker=worker, brief=resolved, signals=signals,
                    fallback_workers=fallback_workers, spec_version=1,
                    spec_hash=_spec_hash(worker, resolved, signals, fallback_workers))
        task["spec_history"] = [{"version": 1, "worker": worker, "brief": resolved,
            "signals": list(signals), "fallback_workers": list(fallback_workers),
            "spec_hash": task["spec_hash"], "created_by": "add", "reason": "initial",
            "amendment_id": None, "change": "initial"}]
    elif fallback_workers:
        raise ValueError("SPEC_INCOMPLETE")
    tasks.append(task)
    _save_queue(queue_path, tasks)
    print(f"ADDED: {task_id} status=waiting depends_on={depends_on}")


@locked_paths("journal_path", "queue_path")
def dispatch(task_id: str, queue_path: Path, journal_path: Path,
             retry: bool = False, reason: str | None = None,
             by: str | None = None, worker: str | None = None) -> dict[str, Any]:
    task = next((row for row in _load_queue(queue_path) if row.get("id") == task_id), None)
    if task is None:
        raise ValueError(f"TASK_NOT_FOUND task={task_id}")
    if not _has_spec(task):
        if task.get("spec_hash"):
            raise ValueError(f"SPEC_CHANGED task={task_id}")
        raise ValueError(f"NO_SPEC task={task_id}")
    if _current_hash(task) != task["spec_hash"]:
        raise ValueError(f"SPEC_CHANGED task={task_id}")
    if worker is not None:
        if not task.get("fallback_workers"):
            raise ValueError(f"NO_FALLBACK task={task_id}")
        if worker not in task["fallback_workers"]:
            raise ValueError(f"WORKER_NOT_ALLOWED task={task_id}")
        if not reason or not reason.strip():
            raise ValueError(f"REASON_REQUIRED task={task_id}")
    current = next(row for row in state(queue_path, journal_path)["tasks"] if row["id"] == task_id)
    action = "retry" if retry else "dispatch"
    if action not in current["allowed_actions"]:
        # Keep Step 3's precise refusal codes for repeated and unsafe attempts.
        status = current["status"]
        if status == "accepted":
            raise ValueError(f"ALREADY_ACCEPTED task={task_id}")
        if status == "dispatched":
            raise ValueError(f"ATTEMPT_IN_FLIGHT task={task_id}")
        if status == "failed" and not retry:
            raise ValueError(f"RETRY_REQUIRED task={task_id}")
        if status == "done":
            raise ValueError(f"RETRY_REQUIRED task={task_id}")
        raise ValueError(f"ACTION_NOT_ALLOWED task={task_id} action={action}")
    return append(task_id, worker or task["worker"], task["brief"], task["signals"],
                  journal_path, retry, reason, dispatched_by=by,
                  spec_version=task.get("spec_version"), spec_hash=task["spec_hash"],
                  worker_switched_from=task["worker"] if worker is not None else None,
                  worker_switch_reason=reason.strip() if worker is not None else None)


@locked_paths("journal_path", "queue_path")
def add_signal(task_id: str, signal: str, reason: str, by: str,
               queue_path: Path, journal_path: Path) -> None:
    tasks = _load_queue(queue_path)
    task = _task(tasks, task_id)
    if "spec_version" not in task:
        raise ValueError(f"NO_SPEC task={task_id}")
    _editable(task, journal_path)
    if not reason.strip():
        raise ValueError("REASON_REQUIRED")
    if not _valid_signals([signal]):
        raise ValueError("SIGNAL_INVALID")
    if signal in task["signals"]:
        raise ValueError("DUPLICATE_SIGNAL")
    old = task["spec_version"]
    task["signals"].append(signal)
    _version(task, by=by, reason=reason, change="add_signal")
    _save_queue(queue_path, tasks)
    _append_record({"status": "spec_changed", "task_id": task_id, "change": "add_signal",
        "from_version": old, "to_version": task["spec_version"], "spec_hash": task["spec_hash"],
        "by": by, "reason": reason}, journal_path)


def _amendments(records: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for row in records:
        number = row.get("amendment_id")
        if isinstance(number, int) and row.get("status") == "amendment_proposed":
            result[number] = {**row, "amendment_status": "pending"}
        elif isinstance(number, int) and row.get("status") in {"amendment_approved", "amendment_rejected"} and number in result:
            result[number]["amendment_status"] = "applied" if row["status"] == "amendment_approved" else "rejected"
    return result


@locked_paths("journal_path", "queue_path")
def propose_amendment(task_id: str, by: str, reason: str, evidence: str,
                      queue_path: Path, journal_path: Path, brief: str | None = None,
                      worker: str | None = None, signals: list[str] | None = None,
                      fallback_workers: list[str] | None = None) -> int:
    task = _task(_load_queue(queue_path), task_id)
    if "spec_version" not in task:
        raise ValueError(f"NO_SPEC task={task_id}")
    _editable(task, journal_path)
    if not reason.strip() or not evidence.strip():
        raise ValueError("REASON_REQUIRED")
    if all(value is None for value in (brief, worker, signals, fallback_workers)):
        raise ValueError("AMENDMENT_EMPTY")
    proposed = {"worker": worker if worker is not None else task["worker"],
        "brief": str(Path(brief).expanduser().resolve(strict=True)) if brief is not None else task["brief"],
        "signals": signals if signals is not None else list(task["signals"]),
        "fallback_workers": fallback_workers if fallback_workers is not None else list(task["fallback_workers"])}
    if not _valid_signals(proposed["signals"]):
        raise ValueError("NO_MACHINE_SIGNAL")
    if not proposed["worker"]:
        raise ValueError("WORKER_NOT_ALLOWED")
    records = _read_records(journal_path)
    number = max(_amendments(records), default=0) + 1
    _append_record({"status": "amendment_proposed", "amendment_id": number,
        "task_id": task_id, "proposed_by": by, "reason": reason, "evidence": evidence,
        "proposed": proposed, "base_spec_version": task["spec_version"],
        "proposed_spec_hash": _spec_hash(**proposed)}, journal_path)
    return number


def _approval_valid(ref: str, number: int, log: str | None) -> bool:
    try:
        path_text, line_text = ref.rsplit(":", 1)
        path = Path(path_text).expanduser().resolve(strict=True)
        expected = Path(log).expanduser().resolve(strict=True) if log is not None else DEFAULT_APPROVALS_LOG.resolve()
        if path != expected:
            return False
        line_number = int(line_text)
        if line_number < 1:
            return False
        lines = path.read_text(encoding="utf-8").splitlines()
        line = lines[line_number - 1]
        return "approval by owner" in line.lower() and bool(re.search(rf"\bamendment\s+A?{number}\b", line, re.I))
    except (OSError, ValueError, IndexError):
        return False


@locked_paths("journal_path", "queue_path")
def decide_amendment(number: int, queue_path: Path, journal_path: Path,
                     approval_ref: str | None = None, approvals_log: str | None = None,
                     reason: str | None = None) -> None:
    amendments = _amendments(_read_records(journal_path))
    if number not in amendments:
        raise ValueError("AMENDMENT_NOT_FOUND")
    amendment = amendments[number]
    if amendment["amendment_status"] != "pending":
        raise ValueError("ALREADY_APPLIED" if amendment["amendment_status"] == "applied" else "ALREADY_REJECTED")
    if approval_ref is None:
        if not reason or not reason.strip():
            raise ValueError("REASON_REQUIRED")
        _append_record({"status": "amendment_rejected", "amendment_id": number,
            "task_id": amendment["task_id"], "reason": reason}, journal_path)
        return
    if not _approval_valid(approval_ref, number, approvals_log):
        raise ValueError("APPROVAL_REF_INVALID")
    tasks = _load_queue(queue_path)
    task = _task(tasks, amendment["task_id"])
    _editable(task, journal_path)
    if task.get("spec_version") != amendment["base_spec_version"]:
        raise ValueError("STALE_AMENDMENT")
    old = task["spec_version"]
    task.update(amendment["proposed"])
    _version(task, by="owner", reason=amendment["reason"], change="amendment", amendment_id=number)
    _save_queue(queue_path, tasks)
    _append_record({"status": "amendment_approved", "amendment_id": number,
        "task_id": task["id"], "approval_ref": approval_ref}, journal_path)
    _append_record({"status": "spec_changed", "task_id": task["id"], "change": "amendment",
        "amendment_id": number, "from_version": old, "to_version": task["spec_version"],
        "spec_hash": task["spec_hash"], "by": "owner", "reason": amendment["reason"]}, journal_path)


@locked_paths("journal_path", "queue_path")
def sync(queue_path: Path, journal_path: Path) -> None:
    tasks = _load_queue(queue_path)
    latest_status: dict[str, str] = {}
    for record in _read_records(journal_path):
        task_id = record.get("task_id")
        status = record.get("status")
        if isinstance(task_id, str) and status in JOURNAL_STATUSES:
            latest_status[task_id] = status

    for task in tasks:
        task_id = str(task.get("id", ""))
        if task_id in latest_status:
            task["status"] = latest_status[task_id]

    accepted_ids = {str(task.get("id")) for task in tasks if task.get("status") == "accepted"}
    for task in tasks:
        dependencies = task.get("depends_on", [])
        if task.get("status") == "waiting" and all(dep in accepted_ids for dep in dependencies):
            task["status"] = "ready"

    _save_queue(queue_path, tasks)
    counts = {status: 0 for status in sorted(VALID_STATUSES)}
    for task in tasks:
        status = task.get("status")
        if status in counts:
            counts[status] += 1
    print("SYNCED: " + " ".join(f"{key}={value}" for key, value in counts.items()))


@locked_paths("journal_path", "queue_path")
def state(queue_path: Path, journal_path: Path, stale_hours: float = 6,
          workflow: str | None = None) -> dict[str, Any]:
    """Project a current snapshot without persisting another source of truth."""
    if stale_hours < 0:
        raise ValueError("STALE_HOURS_INVALID")
    tasks = _load_queue(queue_path)
    journal_valid = verify(journal_path, quiet=True)
    try:
        records = _read_records(journal_path)
    except (OSError, ValueError):
        records = []
    latest: dict[str, dict[str, Any]] = {}
    latest_status: dict[str, dict[str, Any]] = {}
    latest_verified: dict[str, dict[str, Any]] = {}
    latest_checkpoint: dict[str, dict[str, Any]] = {}
    workflow_checkpoints: dict[str, dict[str, Any]] = {}
    dispatches: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        task_id = record.get("task_id")
        if record.get("status") == "checkpoint" and isinstance(record.get("workflow"), str):
            workflow_checkpoints[record["workflow"]] = record
        if isinstance(task_id, str):
            if record.get("status") not in {"spec_changed", "amendment_proposed", "amendment_approved", "amendment_rejected"}:
                latest[task_id] = record
            if record.get("status") in JOURNAL_STATUSES:
                latest_status[task_id] = record
            elif record.get("status") == "verified":
                latest_verified[task_id] = record
            elif record.get("status") == "checkpoint":
                latest_checkpoint[task_id] = record
            if record.get("status") == "dispatched":
                dispatches.setdefault(task_id, []).append(record)

    def checkpoint_view(row: dict[str, Any]) -> dict[str, Any]:
        return {key: row.get(key) for key in ("next_action", "by", "ts", "context_refs", "blocked_reason", "approval_ref")}

    accepted_ids = {
        task_id for task_id, record in latest_status.items()
        if record.get("status") == "accepted"
    }
    projected = []
    pending = [{key: row.get(key) for key in ("amendment_id", "task_id", "proposed_by", "reason", "evidence", "proposed", "base_spec_version")}
               for row in _amendments(records).values() if row["amendment_status"] == "pending"]
    for task in tasks:
        if workflow is not None and task.get("workflow") != workflow:
            continue
        task_id = str(task.get("id", ""))
        status_record = latest_status.get(task_id, {})
        status = status_record.get("status", task.get("status"))
        dependencies = task.get("depends_on", [])
        if status == "waiting" and all(dep in accepted_ids for dep in dependencies):
            status = "ready"
        is_accepted = status == "accepted"
        attempts = dispatches.get(task_id, [])
        dispatch = attempts[-1] if attempts else {}
        view = {
            "id": task_id,
            "title": task.get("title", ""),
            "depends_on": dependencies,
            "status": status,
            "latest_receipt_seq": latest.get(task_id, {}).get("seq"),
            "accepted": is_accepted,
            "accepted_by": status_record.get("accepted_by") if is_accepted else None,
            "output_hashes": status_record.get("output_hashes", {}) if is_accepted else {},
            "verified": ({"seq": latest_verified[task_id].get("seq"),
                          "passed": latest_verified[task_id].get("passed")}
                         if task_id in latest_verified else None),
            "attempt_id": (dispatch.get("attempt_id") or f"{task_id}#{len(attempts)}") if attempts else None,
            "attempt_count": len(attempts),
            "idempotency_key": dispatch.get("idempotency_key"),
            "latest_checkpoint": checkpoint_view(latest_checkpoint[task_id]) if task_id in latest_checkpoint else None,
            "stale_dispatch": status == "dispatched" and bool(dispatch) and _is_stale(dispatch, stale_hours),
            "workflow": task.get("workflow"),
            "worker": task.get("worker"),
            "brief": task.get("brief"),
            "signals": task.get("signals"),
            "spec_hash": task.get("spec_hash"),
            "spec_version": task.get("spec_version"),
            "fallback_workers": task.get("fallback_workers", []),
            "spec_history": [{"version": h["version"], "change": h["change"], "spec_hash": h["spec_hash"],
                              "amendment_id": h.get("amendment_id")} for h in task.get("spec_history", [])],
            "pending_amendments": [a for a in pending if a["task_id"] == task_id],
            "allowed_actions": _actions(status, _has_spec(task)),
        }
        if "spec_version" in task:
            if status in {"ready", "failed"} and task.get("fallback_workers"):
                view["allowed_actions"].append("switch_worker")
            if status in {"waiting", "ready", "failed"}:
                view["allowed_actions"].extend(["add_signal", "propose_amendment"])
        if status == "ready" and not _has_spec(task):
            view["blocked"] = "NO_SPEC"
        projected.append(view)
    groups: dict[str, list[dict[str, Any]]] = {}
    for task in projected:
        if task["workflow"] is not None:
            groups.setdefault(task["workflow"], []).append(task)
    workflow_allowed = {name: ["checkpoint", "needs_owner"] +
                        (["done"] if rows and all(row["accepted"] for row in rows) else [])
                        for name, rows in groups.items()}
    if workflow is not None and workflow not in workflow_allowed:
        workflow_allowed[workflow] = ["checkpoint", "needs_owner"]
    visible_checkpoints = {name: row for name, row in workflow_checkpoints.items()
                           if workflow is None or name == workflow}
    return {
        "tasks": projected,
        "pending_amendments": [a for a in pending if workflow is None or any(t["id"] == a["task_id"] for t in projected)],
        "ready": [task["id"] for task in projected if task["status"] == "ready"],
        "accepted": [task["id"] for task in projected if task["accepted"]],
        "journal_verify": "PASS" if journal_valid else "FAIL",
        "workflow_checkpoints": [{"workflow": name, **checkpoint_view(row)} for name, row in visible_checkpoints.items()],
        "workflow_allowed": workflow_allowed,
        "needs_owner": [{"workflow": name, "reason": row["blocked_reason"],
                         "by": row.get("by"), "ts": row.get("ts")}
                        for name, row in visible_checkpoints.items()
                        if isinstance(row.get("blocked_reason"), str)
                        and row["blocked_reason"].startswith("NEEDS_OWNER:")],
    }


def list_tasks(queue_path: Path, status: str | None) -> None:
    tasks = _load_queue(queue_path)
    selected = [task for task in tasks if status is None or task.get("status") == status]
    if not selected:
        print(f"NO TASKS{f' status={status}' if status else ''}")
        return
    for task in selected:
        dependencies = ",".join(task.get("depends_on", [])) or "-"
        print(
            f"{task.get('id')} | {task.get('status')} | depends_on={dependencies} | "
            f"{task.get('title')}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hàng đợi task có phụ thuộc của AO.")
    parser.add_argument("--queue", default=str(DEFAULT_QUEUE), help=argparse.SUPPRESS)
    parser.add_argument("--journal", default=str(DEFAULT_JOURNAL), help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add")
    add_parser.add_argument("--id", required=True)
    add_parser.add_argument("--title", required=True)
    add_parser.add_argument("--depends-on", action="append", default=[])
    add_parser.add_argument("--workflow")
    add_parser.add_argument("--worker")
    add_parser.add_argument("--brief")
    add_parser.add_argument("--signal", action="append", default=[])
    add_parser.add_argument("--fallback-worker", action="append", default=[])

    dispatch_parser = subparsers.add_parser("dispatch")
    dispatch_parser.add_argument("--id", required=True)
    dispatch_parser.add_argument("--retry", action="store_true")
    dispatch_parser.add_argument("--reason")
    dispatch_parser.add_argument("--by")
    dispatch_parser.add_argument("--worker")

    signal_parser = subparsers.add_parser("add-signal")
    for key in ("id", "signal", "reason", "by"):
        signal_parser.add_argument("--" + key, required=True)
    proposal_parser = subparsers.add_parser("propose-amendment")
    for key in ("id", "by", "reason", "evidence"):
        proposal_parser.add_argument("--" + key, required=True)
    proposal_parser.add_argument("--brief")
    proposal_parser.add_argument("--worker")
    proposal_parser.add_argument("--signal", action="append")
    proposal_parser.add_argument("--fallback-worker", action="append")
    approval_parser = subparsers.add_parser("approve-amendment")
    approval_parser.add_argument("--amendment-id", type=int, required=True)
    approval_parser.add_argument("--approval-ref", required=True)
    approval_parser.add_argument("--approvals-log")
    rejection_parser = subparsers.add_parser("reject-amendment")
    rejection_parser.add_argument("--amendment-id", type=int, required=True)
    rejection_parser.add_argument("--reason", required=True)

    subparsers.add_parser("sync")
    state_parser = subparsers.add_parser("state")
    state_parser.add_argument("--json", action="store_true")
    state_parser.add_argument("--stale-hours", type=float, default=6)
    state_parser.add_argument("--workflow")
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--status", choices=sorted(VALID_STATUSES))
    return parser.parse_args()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    args = parse_args()
    queue_path = Path(args.queue)
    journal_path = Path(args.journal)
    try:
        if args.command == "add":
            add_task(args.id, args.title, args.depends_on, queue_path,
                     args.workflow, args.worker, args.brief, args.signal, args.fallback_worker)
        elif args.command == "dispatch":
            print(json.dumps(dispatch(args.id, queue_path, journal_path,
                                      args.retry, args.reason, args.by, args.worker), ensure_ascii=False, indent=2))
        elif args.command == "add-signal":
            add_signal(args.id, args.signal, args.reason, args.by, queue_path, journal_path)
        elif args.command == "propose-amendment":
            print(f"amendment_id={propose_amendment(args.id, args.by, args.reason, args.evidence, queue_path, journal_path, args.brief, args.worker, args.signal, args.fallback_worker)}")
        elif args.command == "approve-amendment":
            decide_amendment(args.amendment_id, queue_path, journal_path, args.approval_ref, args.approvals_log)
        elif args.command == "reject-amendment":
            decide_amendment(args.amendment_id, queue_path, journal_path, reason=args.reason)
        elif args.command == "sync":
            sync(queue_path, journal_path)
        elif args.command == "state":
            snapshot = state(queue_path, journal_path, args.stale_hours, args.workflow)
            if args.json:
                print(json.dumps(snapshot, ensure_ascii=False, indent=2))
            else:
                print(f"JOURNAL_VERIFY: {snapshot['journal_verify']}")
                for task in snapshot["tasks"]:
                    print(
                        f"{task['id']} | {task['title']} | depends_on={task['depends_on']} "
                        f"| status={task['status']} | latest_receipt_seq={task['latest_receipt_seq']} "
                        f"| accepted={task['accepted']} | accepted_by={task['accepted_by']} "
                        f"| output_hashes={task['output_hashes']} | verified={task['verified']} "
                        f"| attempt_id={task['attempt_id']} | attempt_count={task['attempt_count']} "
                        f"| idempotency_key={task['idempotency_key']} | stale_dispatch={task['stale_dispatch']} "
                        f"| latest_checkpoint={task['latest_checkpoint']}"
                    )
                print("READY: " + ", ".join(snapshot["ready"]))
                print("ACCEPTED: " + ", ".join(snapshot["accepted"]))
                print(f"WORKFLOW_CHECKPOINTS: {snapshot['workflow_checkpoints']}")
        else:
            list_tasks(queue_path, args.status)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
