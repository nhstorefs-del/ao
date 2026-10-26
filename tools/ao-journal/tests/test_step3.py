"""Step 3 attempt and checkpoint behavior through temporary CLI state."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Step3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = self.root / "j.jsonl"
        self.queue = self.root / "q.json"
        self.brief = self.root / "brief.md"
        self.brief.write_text("brief", encoding="utf-8")
        self.output = self.root / "output.txt"
        self.output.write_text("result", encoding="utf-8")

    def cli(self, script, *args):
        options = ["--journal", str(self.journal)]
        if script == "queue.py":
            options = ["--queue", str(self.queue)] + options
        return subprocess.run([sys.executable, str(ROOT / script), *options, *args],
                              capture_output=True, text=True, encoding="utf-8")

    def rows(self):
        return [json.loads(line) for line in self.journal.read_text(encoding="utf-8").splitlines()] if self.journal.exists() else []

    def dispatch(self, task="A", *extra):
        return self.cli("journal.py", "append", "--task-id", task, "--worker", "w",
                        "--brief", str(self.brief), "--signal", f"file:{self.output}", *extra)

    def complete(self, task="A", attempt="A#1", status="done"):
        return self.cli("journal.py", "complete", "--task-id", task, "--attempt-id", attempt,
                        "--status", status, "--summary", "result")

    def assert_rejected_unchanged(self, code, action):
        before = self.journal.read_bytes() if self.journal.exists() else b""
        result = action()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(code, result.stdout + result.stderr)
        self.assertEqual(self.journal.read_bytes() if self.journal.exists() else b"", before)

    def test_attempt_identity_retries_and_old_results(self):
        self.assertEqual(self.dispatch().returncode, 0)
        first = self.rows()[0]
        self.assertEqual(first["attempt_id"], "A#1")
        self.assertEqual(first["idempotency_key"], hashlib.sha256(("A" + first["brief_hash"]).encode()).hexdigest())
        self.assert_rejected_unchanged("ATTEMPT_IN_FLIGHT", self.dispatch)
        self.assert_rejected_unchanged("ATTEMPT_IN_FLIGHT", lambda: self.dispatch("A", "--retry", "--reason", "too soon"))
        self.assert_rejected_unchanged("STALE_RESULT", lambda: self.complete(attempt="A#9"))
        self.assert_rejected_unchanged("--attempt-id", lambda: self.cli("journal.py", "complete", "--task-id", "A", "--status", "done", "--summary", "x"))
        self.assertEqual(self.complete(status="failed").returncode, 0)
        self.assert_rejected_unchanged("RETRY_REQUIRED", self.dispatch)
        self.assert_rejected_unchanged("REASON_REQUIRED", lambda: self.dispatch("A", "--retry"))
        self.assertEqual(self.dispatch("A", "--retry", "--reason", "try again").returncode, 0)
        second = self.rows()[-1]
        self.assertEqual((second["attempt_id"], second["retry_of"], second["retry_reason"]),
                         ("A#2", "A#1", "try again"))
        self.assertEqual(second["idempotency_key"], first["idempotency_key"])
        self.assert_rejected_unchanged("STALE_RESULT", lambda: self.complete(attempt="A#1"))
        self.assertEqual(self.complete(attempt="A#2").returncode, 0)
        self.assertEqual(self.cli("journal.py", "verify-task", "--task-id", "A").returncode, 0)
        self.assertEqual(self.rows()[-1]["attempt_id"], "A#2")
        self.assertEqual(self.cli("journal.py", "accept", "--task-id", "A", "--by", "owner").returncode, 0)
        self.assertEqual(self.rows()[-1]["attempt_id"], "A#2")
        self.assert_rejected_unchanged("ALREADY_ACCEPTED", self.dispatch)
        self.assert_rejected_unchanged("ALREADY_ACCEPTED", lambda: self.dispatch("A", "--retry", "--reason", "again"))
        self.assert_rejected_unchanged("STALE_RESULT", lambda: self.complete(attempt="A#2"))

    def test_checkpoint_and_state_are_read_only(self):
        self.assertEqual(self.cli("queue.py", "add", "--id", "A", "--title", "Task").returncode, 0)
        self.assertEqual(self.dispatch().returncode, 0)
        context = self.root / "context.md"
        context.write_text("context", encoding="utf-8")
        self.assert_rejected_unchanged("CONTEXT_REF_MISSING", lambda: self.cli("journal.py", "checkpoint", "--task-id", "A", "--by", "x", "--next-action", "resume", "--context-ref", str(context), "--context-ref", str(self.root / "missing")))
        self.assertEqual(self.cli("journal.py", "checkpoint", "--task-id", "A", "--by", "owner", "--next-action", "resume", "--context-ref", str(context), "--blocked-reason", "wait", "--approval-ref", "ref").returncode, 0)
        self.assertEqual(self.cli("journal.py", "checkpoint", "--workflow", "W", "--by", "owner", "--next-action", "next", "--context-ref", str(context)).returncode, 0)
        before = self.journal.read_bytes()
        snapshot = json.loads(self.cli("queue.py", "state", "--json", "--stale-hours", "0").stdout)
        task = snapshot["tasks"][0]
        self.assertEqual(task["status"], "dispatched")
        self.assertEqual((task["attempt_id"], task["attempt_count"]), ("A#1", 1))
        self.assertTrue(task["stale_dispatch"])
        self.assertEqual(task["latest_checkpoint"]["context_refs"], [str(context.resolve())])
        self.assertEqual(task["latest_checkpoint"]["blocked_reason"], "wait")
        self.assertEqual(snapshot["workflow_checkpoints"][0]["next_action"], "next")
        self.assertEqual(self.journal.read_bytes(), before)
        self.assertEqual(self.cli("journal.py", "verify").returncode, 0)

    def test_stale_retry_and_legacy_attempt(self):
        self.assertEqual(self.dispatch().returncode, 0)
        self.assertEqual(self.dispatch("A", "--retry", "--reason", "timed out", "--stale-hours", "0").returncode, 0)
        self.assert_rejected_unchanged("STALE_RESULT", lambda: self.complete(attempt="A#1"))
        self.assertEqual(self.complete(attempt="A#2").returncode, 0)
        # Simulate a pre-step-3 dispatch with a valid hash chain.
        sys.path.insert(0, str(ROOT))
        try:
            import journal
            journal._append_record({"task_id": "OLD", "worker": "w", "brief_path": str(self.brief),
                                    "brief_hash": hashlib.sha256(self.brief.read_bytes()).hexdigest(),
                                    "completion_signals": [f"file:{self.output}"], "status": "dispatched"}, self.journal)
        finally:
            sys.path.pop(0)
        self.assertEqual(self.complete("OLD", "OLD#1").returncode, 0)
        self.assertEqual(self.cli("journal.py", "accept", "--task-id", "OLD", "--by", "owner").returncode, 0)
        self.assertEqual(self.cli("journal.py", "verify").returncode, 0)


if __name__ == "__main__":
    unittest.main()
