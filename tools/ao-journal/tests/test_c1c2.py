"""C1/C2 CLI contract against isolated queue and journal files."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class C1C2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.queue = self.root / "queue.json"
        self.journal = self.root / "journal.jsonl"
        self.brief = self.root / "brief.md"
        self.brief.write_text("fixed brief", encoding="utf-8")
        self.output = self.root / "output.txt"
        self.output.write_text("result", encoding="utf-8")
        self.signal = f"file:{self.output}"

    def cli(self, script, *args):
        options = ["--journal", str(self.journal)]
        if script == "queue.py":
            options = ["--queue", str(self.queue), *options]
        return subprocess.run([sys.executable, str(ROOT / script), *options, *args],
                              capture_output=True, text=True, encoding="utf-8")

    def ok(self, script, *args):
        result = self.cli(script, *args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def reject(self, code, script, *args):
        before = self.journal.read_bytes() if self.journal.exists() else b""
        result = self.cli(script, *args)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(code, result.stdout + result.stderr)
        self.assertEqual(self.journal.read_bytes() if self.journal.exists() else b"", before)

    def add(self, task="A", spec=True, *extra):
        args = ["add", "--id", task, "--title", task, "--workflow", "W", *extra]
        if spec:
            args += ["--worker", "fixed-worker", "--brief", str(self.brief),
                     "--signal", self.signal]
        self.ok("queue.py", *args)

    def state(self, *extra):
        return json.loads(self.ok("queue.py", "state", "--json", *extra).stdout)

    def test_add_spec_and_legacy(self):
        self.add()
        self.add("OLD", False)
        raw = json.loads(self.queue.read_text(encoding="utf-8"))
        spec = raw[0]
        self.assertEqual(spec["brief"], str(self.brief.resolve()))
        canonical = json.dumps({"worker": "fixed-worker", "brief": spec["brief"],
                                "signals": [self.signal]}, ensure_ascii=False,
                               sort_keys=True, separators=(",", ":"))
        self.assertEqual(spec["spec_hash"], hashlib.sha256(canonical.encode()).hexdigest())
        self.assertNotIn("spec_hash", raw[1])
        self.assertEqual(self.state()["tasks"][1]["blocked"], "NO_SPEC")
        self.reject("NO_SPEC", "queue.py", "dispatch", "--id", "OLD")
        result = self.cli("queue.py", "edit", "--id", "A", "--worker", "x")
        self.assertNotEqual(result.returncode, 0)

    def test_dispatch_spec_and_errors(self):
        self.add()
        self.reject("ACTION_NOT_ALLOWED", "queue.py", "dispatch", "--id", "A", "--retry", "--reason", "early")
        first = json.loads(self.ok("queue.py", "dispatch", "--id", "A", "--by", "coordinator").stdout)
        self.assertEqual((first["worker"], first["brief_path"], first["completion_signals"],
                          first["dispatched_by"]),
                         ("fixed-worker", str(self.brief.resolve()), [self.signal], "coordinator"))
        self.reject("ATTEMPT_IN_FLIGHT", "queue.py", "dispatch", "--id", "A")
        self.reject("ATTEMPT_IN_FLIGHT", "queue.py", "dispatch", "--id", "A", "--retry", "--reason", "x")
        self.ok("journal.py", "complete", "--task-id", "A", "--attempt-id", "A#1",
                "--status", "failed", "--summary", "failed")
        self.reject("RETRY_REQUIRED", "queue.py", "dispatch", "--id", "A")
        self.reject("REASON_REQUIRED", "queue.py", "dispatch", "--id", "A", "--retry")
        second = json.loads(self.ok("queue.py", "dispatch", "--id", "A", "--retry", "--reason", "fix").stdout)
        self.assertEqual((second["attempt_id"], second["retry_reason"]), ("A#2", "fix"))
        self.ok("journal.py", "complete", "--task-id", "A", "--attempt-id", "A#2",
                "--status", "done", "--summary", "done")
        self.reject("RETRY_REQUIRED", "queue.py", "dispatch", "--id", "A")
        self.ok("journal.py", "accept", "--task-id", "A", "--by", "owner")
        self.reject("ALREADY_ACCEPTED", "queue.py", "dispatch", "--id", "A")

    def test_changed_spec_and_waiting(self):
        self.add()
        self.add("B", True, "--depends-on", "A")
        self.reject("ACTION_NOT_ALLOWED", "queue.py", "dispatch", "--id", "B")
        raw = json.loads(self.queue.read_text(encoding="utf-8"))
        raw[0]["worker"] = "tampered"
        self.queue.write_text(json.dumps(raw), encoding="utf-8")
        self.reject("SPEC_CHANGED", "queue.py", "dispatch", "--id", "A")

    def test_actions_workflow_owner_and_filter(self):
        self.add()
        self.add("B", True, "--depends-on", "A")
        self.add("LEGACY", False)
        self.assertEqual({t["id"]: t["allowed_actions"] for t in self.state()["tasks"]},
                         {"A": ["dispatch", "add_signal", "propose_amendment"],
                          "B": ["add_signal", "propose_amendment"], "LEGACY": []})
        self.assertNotIn("done", self.state()["workflow_allowed"]["W"])
        self.ok("queue.py", "dispatch", "--id", "A")
        self.assertEqual(self.state()["tasks"][0]["allowed_actions"], [])
        self.ok("journal.py", "complete", "--task-id", "A", "--attempt-id", "A#1",
                "--status", "done", "--summary", "done")
        self.assertEqual(self.state()["tasks"][0]["allowed_actions"], ["verify", "accept"])
        self.ok("journal.py", "accept", "--task-id", "A", "--by", "owner")
        snapshot = self.state()
        self.assertEqual(snapshot["tasks"][0]["allowed_actions"], [])
        self.assertEqual(snapshot["tasks"][1]["allowed_actions"],
                         ["dispatch", "add_signal", "propose_amendment"])
        self.ok("journal.py", "checkpoint", "--workflow", "W", "--by", "owner",
                "--next-action", "wait", "--blocked-reason", "NEEDS_OWNER: decide")
        self.assertEqual(self.state()["needs_owner"][0]["reason"], "NEEDS_OWNER: decide")
        self.ok("journal.py", "checkpoint", "--workflow", "W", "--by", "owner",
                "--next-action", "resume")
        self.assertEqual(self.state()["needs_owner"], [])
        self.assertEqual(len(self.state("--workflow", "other")["tasks"]), 0)
        self.assertNotIn("done", self.state("--workflow", "other")["workflow_allowed"]["other"])
        # A separate workflow with one accepted task can close.
        self.ok("queue.py", "add", "--id", "C", "--title", "C", "--workflow", "X",
                "--worker", "w", "--brief", str(self.brief), "--signal", self.signal)
        self.ok("queue.py", "dispatch", "--id", "C")
        self.ok("journal.py", "complete", "--task-id", "C", "--attempt-id", "C#1",
                "--status", "done", "--summary", "done")
        self.ok("journal.py", "accept", "--task-id", "C", "--by", "owner")
        self.assertIn("done", self.state("--workflow", "X")["workflow_allowed"]["X"])

    def test_concurrent_dispatch(self):
        for _ in range(3):
            self.queue.unlink(missing_ok=True)
            self.journal.unlink(missing_ok=True)
            self.add()
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: self.cli("queue.py", "dispatch", "--id", "A"), range(8)))
            self.assertEqual(sum(result.returncode == 0 for result in results), 1)
            self.assertEqual(len(self.journal.read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
