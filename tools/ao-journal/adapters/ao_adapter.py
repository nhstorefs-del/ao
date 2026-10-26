#!/usr/bin/env python3
"""One foreground coordinator turn over the AO journal Core CLI."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORE = HERE.parent
sys.path.insert(0,str(CORE))
import delegation_gate
ROOT = CORE.parent.parent
FIELDS = {"action", "task_id", "reason", "next_action", "blocked_reason", "context_refs"}
ACTION_FIELDS = {
    "switch_worker": {"action", "task_id", "fallback_worker", "reason"},
    "add_signal": {"action", "task_id", "new_signal", "reason"},
    "propose_amendment": {"action", "task_id", "reason", "evidence", "proposed_brief",
                          "proposed_worker", "proposed_signals", "proposed_fallback_workers"},
}
BASE_FIELDS = set(FIELDS)
FIELDS |= set().union(*ACTION_FIELDS.values())
OPTIONAL_FIELDS = FIELDS - {"action"}
DEFAULT_BACKEND_TIMEOUT_S = 300
FLAGS = ("supports_tools", "supports_files", "supports_vision", "supports_long_session",
         "supports_structured_output", "supports_resume", "supports_subagents")
TASK_ACTIONS = {"dispatch", "retry", "verify", "accept", *ACTION_FIELDS}
WORKFLOW_ACTIONS = {"checkpoint", "needs_owner", "done"}


def run_core(script, options, command):
    proc = subprocess.run([sys.executable, str(CORE / script), *options, *command],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode:
        match = re.search(r"\b(?:ERROR:\s*)?([A-Z][A-Z0-9_]+)\b", proc.stdout + "\n" + proc.stderr)
        raise CoreRefused(match.group(1) if match else "CORE_ERROR")
    return proc.stdout


class CoreRefused(Exception):
    pass


class AdapterRejected(Exception):
    pass


class BackendFailed(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def policy_section(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.startswith("## Sổ việc bền (durable workflow)")), None)
    if start is None:
        raise AdapterRejected("POLICY_MISSING")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).rstrip()


def context_from_state(state, workflow, limit):
    checkpoints = [row for row in state.get("workflow_checkpoints", [])
                   if row.get("workflow") == workflow]
    refs = checkpoints[-1].get("context_refs") or [] if checkpoints else []
    remaining = max(0, limit)
    content = []
    for raw in refs:
        if not isinstance(raw, str):
            continue
        try:
            value = Path(raw).read_text(encoding="utf-8", errors="replace")[:remaining]
        except OSError:
            value = "[unavailable]"[:remaining]
        content.append({"ref": raw, "content": value})
        remaining = max(0, remaining - len(value))
    return content


def capability(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or any(
            not (type(value.get(key)) is bool or value.get(key) == "unknown") for key in FLAGS
        ):
            return None
        if value.get("invocation") not in ("fake", "qwen-cli"):
            return None
        if type(value.get("max_context_chars")) is not int or value["max_context_chars"] < 0:
            return None
        if value["invocation"] == "qwen-cli" and not isinstance(value.get("env_file"), str):
            return None
        timeout = value.get("timeout_s", DEFAULT_BACKEND_TIMEOUT_S)
        if type(timeout) not in (int, float) or timeout <= 0:
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None


def make_prompt(state, policy, contexts):
    schema = {"fields": sorted(FIELDS), "task_actions": sorted(TASK_ACTIONS),
              "workflow_actions": sorted(WORKFLOW_ACTIONS),
              "rule": "Print exactly one JSON object. Do not modify/write files or run commands. "
                      "switch_worker only to a listed fallback_workers worker; add_signal only ADDS a "
                      "criterion. Every other spec change requires propose_amendment, then needs_owner. "
                      "You cannot approve amendments.",
              "action_fields": {key: sorted(value) for key, value in ACTION_FIELDS.items()}}
    return json.dumps({"state": state, "policy": policy, "context_refs": contexts,
                       "action_schema": schema}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_object(raw):
    decoder = json.JSONDecoder()
    found = None
    position = 0
    while position < len(raw):
        if raw[position] not in '{["':
            position += 1
            continue
        try:
            value, end = decoder.raw_decode(raw, position)
        except (ValueError, TypeError):
            position += 1
            continue
        if isinstance(value, dict):
            if found is not None:
                return None
            found = value
        position = end  # Skip nested objects and braces inside decoded JSON strings/arrays.
    return found


def read_key(env_file):
    for line in Path(os.path.expandvars(env_file)).expanduser().read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?NVIDIA_API_KEY\s*=\s*(.*?)\s*", line)
        if match:
            return match.group(1).strip('"\'')
    raise ValueError("NVIDIA_API_KEY missing")


def backend_output(cap, args, prompt):
    if cap["invocation"] == "fake":
        if not args.fake_output_file:
            return ""
        return Path(args.fake_output_file).read_text(encoding="utf-8")
    raise BackendFailed("DELEGATION_REQUIRED_UNBOUNDED_COORDINATOR_BACKEND")
    try:
        key = read_key(cap["env_file"])
    except (OSError, ValueError):
        raise BackendFailed("NO_KEY") from None
    if not key:
        raise BackendFailed("NO_KEY")
    command = [args.qwen_command, "--openai-api-key", key,
               "--openai-base-url", "https://integrate.api.nvidia.com/v1",
               "-m", "deepseek-ai/deepseek-v4.1-flash", "--yolo", "-p",
               "Follow the instructions given on stdin. Reply with exactly one JSON object and nothing else."]

    def as_text(value):
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value or ""

    def safe_tail(value):
        return as_text(value).replace(key, "***")[-200:]

    try:
        with tempfile.TemporaryDirectory() as directory:
            proc = subprocess.run(command, cwd=directory, input=prompt, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=(args.backend_timeout if args.backend_timeout is not None else
                                           cap.get("timeout_s", DEFAULT_BACKEND_TIMEOUT_S)))
    except subprocess.TimeoutExpired as exc:
        raise BackendFailed("TIMEOUT", safe_tail(as_text(exc.stdout) + "\n" + as_text(exc.stderr))) from None
    except (OSError, ValueError):
        raise BackendFailed("START") from None
    if proc.returncode != 0:
        raise BackendFailed(f"EXIT_{proc.returncode}", safe_tail(proc.stdout + "\n" + proc.stderr))
    if not proc.stdout.strip():
        raise BackendFailed("EMPTY_OUTPUT", safe_tail(proc.stderr))
    return proc.stdout


def validate(obj, state, workflow):
    if set(obj) - FIELDS:
        raise AdapterRejected("FORBIDDEN_FIELD")
    action = obj.get("action")
    if isinstance(action, str) and action in TASK_ACTIONS | WORKFLOW_ACTIONS:
        if set(obj) - ACTION_FIELDS.get(action, BASE_FIELDS):
            raise AdapterRejected("FORBIDDEN_FIELD")
    for name in OPTIONAL_FIELDS:
        if obj.get(name) is None:
            obj.pop(name, None)
    action = obj.get("action")
    if not isinstance(action, str) or action not in TASK_ACTIONS | WORKFLOW_ACTIONS:
        raise AdapterRejected("ACTION_NOT_ALLOWED")
    if set(obj) - ACTION_FIELDS.get(action, BASE_FIELDS):
        raise AdapterRejected("FORBIDDEN_FIELD")
    for name in ("task_id", "reason", "next_action", "blocked_reason"):
        if name in obj and not isinstance(obj[name], str):
            raise AdapterRejected("INVALID_FIELD")
    if "context_refs" in obj and (not isinstance(obj["context_refs"], list) or
                                  any(not isinstance(ref, str) for ref in obj["context_refs"])):
        raise AdapterRejected("INVALID_FIELD")
    for name in ("fallback_worker", "new_signal", "evidence", "proposed_brief", "proposed_worker"):
        if name in obj and not isinstance(obj[name], str):
            raise AdapterRejected("INVALID_FIELD")
    for name in ("proposed_signals", "proposed_fallback_workers"):
        if name in obj and (not isinstance(obj[name], list) or
                            any(not isinstance(item, str) for item in obj[name])):
            raise AdapterRejected("INVALID_FIELD")
    if action in TASK_ACTIONS:
        task = next((row for row in state.get("tasks", []) if row.get("id") == obj.get("task_id")), None)
        if task is None:
            raise AdapterRejected("UNKNOWN_TASK")
        if action not in task.get("allowed_actions", []):
            raise AdapterRejected("ACTION_NOT_ALLOWED")
    elif action not in state.get("workflow_allowed", {}).get(workflow, []):
        raise AdapterRejected("ACTION_NOT_ALLOWED")
    if action == "retry" and not obj.get("reason", "").strip():
        raise AdapterRejected("REASON_REQUIRED")
    if action in ACTION_FIELDS:
        required = ACTION_FIELDS[action] - {"action", "task_id"}
        if action == "propose_amendment":
            required -= {"proposed_brief", "proposed_worker", "proposed_signals", "proposed_fallback_workers"}
            if not any(key in obj for key in ("proposed_brief", "proposed_worker", "proposed_signals", "proposed_fallback_workers")):
                raise AdapterRejected("INVALID_FIELD")
        if any(not obj.get(key, "").strip() for key in required):
            raise AdapterRejected("REASON_REQUIRED" if "reason" in required and not obj.get("reason", "").strip() else "INVALID_FIELD")
    if action == "needs_owner" and not (obj.get("blocked_reason") or obj.get("reason") or "").strip():
        raise AdapterRejected("REASON_REQUIRED")
    if action == "checkpoint" and not obj.get("next_action", "").strip():
        raise AdapterRejected("NEXT_ACTION_REQUIRED")
    return action


def worker_result(receipt, workers_path):
    task_id, attempt_id = receipt["task_id"], receipt["attempt_id"]
    try:
        journal_path=receipt.get('delegation_journal')
        if not journal_path: raise delegation_gate.GateRefused('DELEGATION_RECEIPT_REQUIRED')
        delegation_gate.receipt_check(journal_path,attempt_id,receipt['worker'],receipt['brief_path'])
        delegation_gate.claim(journal_path,attempt_id,receipt['worker'],receipt['brief_path'])
        workers = json.loads(workers_path.read_text(encoding="utf-8"))
        spec = workers.get(receipt["worker"])
        if not isinstance(spec, dict):
            return False, "WORKER_NOT_REGISTERED"
        template, timeout = spec.get("command"), spec.get("timeout_s")
        if (not isinstance(template, list) or not template or
            any(not isinstance(part, str) for part in template) or
            not isinstance(timeout, (int, float)) or timeout <= 0):
            return False, "WORKER_CONFIG_INVALID"
        values = {"brief": receipt["brief_path"], "task_id": task_id, "attempt_id": attempt_id}
        command = [part.format_map(values) for part in template]
        env = dict(os.environ, AO_TASK_ID=task_id, AO_ATTEMPT_ID=attempt_id, AO_BRIEF=receipt["brief_path"])
        delegation_gate.receipt_check(journal_path,attempt_id,receipt['worker'],receipt['brief_path'],('EXECUTING',))
        proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace')
        delegation_gate.attach_child(journal_path,attempt_id,receipt['worker'],receipt['brief_path'],proc.pid)
        try:
            stdout,stderr=proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill();proc.communicate();delegation_gate.finish(journal_path,attempt_id,receipt['worker'],receipt['brief_path'])
            raise
        proc.stdout,proc.stderr=stdout,stderr
        delegation_gate.finish(journal_path,attempt_id,receipt['worker'],receipt['brief_path'])
        tail = (proc.stdout + "\n" + proc.stderr).strip()[-300:]
        return proc.returncode == 0, f"worker exit={proc.returncode}: {tail}"
    except subprocess.TimeoutExpired:
        return False, "WORKER_TIMEOUT"
    except (OSError, KeyError, ValueError, AttributeError):
        return False, "WORKER_START_FAILED"


def apply_action(obj, args, options_queue, options_journal, state):
    action = obj["action"]
    task_id = obj.get("task_id")
    if action in ("dispatch", "retry", "switch_worker"):
        command = ["dispatch", "--id", task_id, "--by", args.coordinator]
        if action == "retry":
            command += ["--retry", "--reason", obj["reason"]]
        if action == "switch_worker":
            command += ["--worker", obj["fallback_worker"], "--reason", obj["reason"]]
            task = next(row for row in state["tasks"] if row["id"] == task_id)
            if task["status"] == "failed":
                command.append("--retry")
        receipt = json.loads(run_core("queue.py", options_queue, command))
        success, summary = worker_result(receipt, Path(args.workers))
        run_core("journal.py", options_journal,
                 ["complete", "--task-id", task_id, "--attempt-id", receipt["attempt_id"],
                  "--status", "done" if success else "failed", "--summary", summary])
        return "APPLIED" if success else "WORKER_FAILED:" + summary.split(":", 1)[0].replace("worker exit=", "EXIT_")
    if action == "add_signal":
        run_core("queue.py", options_queue, ["add-signal", "--id", task_id,
                 "--signal", obj["new_signal"], "--reason", obj["reason"], "--by", args.coordinator])
        return "APPLIED"
    if action == "propose_amendment":
        command = ["propose-amendment", "--id", task_id, "--by", args.coordinator,
                   "--reason", obj["reason"], "--evidence", obj["evidence"]]
        for key, flag in (("proposed_brief", "--brief"), ("proposed_worker", "--worker")):
            if key in obj:
                command += [flag, obj[key]]
        for key, flag in (("proposed_signals", "--signal"),
                          ("proposed_fallback_workers", "--fallback-worker")):
            for value in obj.get(key, []):
                command += [flag, value]
        run_core("queue.py", options_queue, command)
        return "APPLIED"
    if action == "verify":
        command = ["verify-task", "--task-id", task_id]
    elif action == "accept":
        command = ["accept", "--task-id", task_id, "--by", args.coordinator]
    else:
        command = ["checkpoint", "--workflow", args.workflow, "--by", args.coordinator,
                   "--next-action", ("WORKFLOW_DONE" if action == "done" else
                                     obj.get("next_action", "NEEDS_OWNER"))]
        if action == "needs_owner":
            command += ["--blocked-reason", "NEEDS_OWNER: " +
                        (obj.get("blocked_reason") or obj.get("reason")).strip()]
        elif obj.get("blocked_reason"):
            command += ["--blocked-reason", obj["blocked_reason"]]
        for ref in obj.get("context_refs", []):
            command += ["--context-ref", ref]
    run_core("journal.py", options_journal, command)
    return "APPLIED"


def turn(args):
    result = {"coordinator": args.coordinator, "workflow": args.workflow, "action": None,
              "task_id": None, "outcome": None, "degraded": False, "detail": ""}
    raw = ""
    options_queue = ["--queue", args.queue, "--journal", args.journal]
    options_journal = ["--journal", args.journal]
    try:
        state = json.loads(run_core("queue.py", options_queue,
                                    ["state", "--json", "--workflow", args.workflow]))
        policy = policy_section(Path(args.claude_md))
        cap = capability(Path(args.capabilities_dir) / f"{args.coordinator}.json")
        if cap is None:
            result["outcome"] = "CAPABILITY_MISSING"
            return result, raw
        result["degraded"] = cap["supports_structured_output"] is not True
        limit = args.max_context_chars if args.max_context_chars is not None else cap["max_context_chars"]
        prompt = make_prompt(state, policy, context_from_state(state, args.workflow, limit))
        if args.prompt_out:
            Path(args.prompt_out).write_text(prompt, encoding="utf-8")
        attempts = 1 if cap["invocation"] == "fake" else 2
        obj = None
        for attempt in range(attempts):
            try:
                raw = backend_output(cap, args, prompt)
            except BackendFailed as exc:
                if exc.code == "EMPTY_OUTPUT" and attempt + 1 < attempts:
                    continue
                raise
            obj = parse_object(raw)
            if obj is not None:
                break
        if obj is None:
            result["outcome"] = "MALFORMED_OUTPUT"
            return result, raw
        result["action"] = obj.get("action")
        result["task_id"] = obj.get("task_id")
        validate(obj, state, args.workflow)
        result["outcome"] = apply_action(obj, args, options_queue, options_journal, state)
    except AdapterRejected as exc:
        result["outcome"] = "ADAPTER_REJECTED:" + str(exc)
    except CoreRefused as exc:
        result["outcome"] = "CORE_REFUSED:" + str(exc)
    except BackendFailed as exc:
        result["outcome"] = "BACKEND_FAILED:" + exc.code
        result["detail"] = exc.detail
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result["outcome"] = "ADAPTER_REJECTED:INPUT_ERROR"
        result["detail"] = type(exc).__name__
    return result, raw


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["turn"])
    parser.add_argument("--coordinator", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--journal", default=str(CORE / "journal.jsonl"))
    parser.add_argument("--queue", default=str(CORE / "queue.json"))
    parser.add_argument("--workers", default=str(HERE / "workers.json"))
    parser.add_argument("--capabilities-dir", default=str(HERE / "capabilities"))
    parser.add_argument("--turn-log", default=str(HERE / "turn_log.jsonl"))
    parser.add_argument("--claude-md", default=str(ROOT / "CLAUDE.md"))
    parser.add_argument("--fake-output-file")
    parser.add_argument("--fake-script", dest="fake_output_file")
    parser.add_argument("--prompt-out")
    parser.add_argument("--max-context-chars", type=int)
    parser.add_argument("--qwen-command", default=os.environ.get("AO_QWEN_EXE") or shutil.which("qwen") or "qwen")
    parser.add_argument("--backend-timeout", type=float)
    args = parser.parse_args()
    result, raw = turn(args)
    log = {"coordinator": args.coordinator, "workflow": args.workflow,
           "output_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
           "action": result["action"], "task_id": result["task_id"],
           "outcome": result["outcome"]}
    Path(args.turn_log).parent.mkdir(parents=True, exist_ok=True)
    with Path(args.turn_log).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(log, ensure_ascii=False) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
