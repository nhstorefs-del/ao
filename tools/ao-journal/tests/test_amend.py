"""AMEND Core contracts on disposable queue and journal files."""
import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AmendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.queue = self.root / "queue.json"
        self.journal = self.root / "journal.jsonl"
        self.brief = self.root / "brief.md"
        self.brief.write_text("brief", encoding="utf-8")
        self.output = self.root / "out.txt"
        self.output.write_text("done", encoding="utf-8")

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "queue.py"), "--queue", str(self.queue),
                               "--journal", str(self.journal), *args], capture_output=True, text=True)

    def ok(self, *args):
        result = self.cli(*args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def reject(self, code, *args):
        before = (self.queue.read_bytes() if self.queue.exists() else b"",
                  self.journal.read_bytes() if self.journal.exists() else b"")
        result = self.cli(*args)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(code, result.stdout + result.stderr)
        self.assertEqual(before, (self.queue.read_bytes() if self.queue.exists() else b"",
                                  self.journal.read_bytes() if self.journal.exists() else b""))

    def add(self, fallback=True):
        args = ["add", "--id", "A", "--title", "A", "--worker", "W1",
                "--brief", str(self.brief), "--signal", f"file:{self.output}"]
        if fallback:
            args += ["--fallback-worker", "W2"]
        self.ok(*args)
        self.ok("sync")

    def state(self):
        return json.loads(self.ok("state", "--json").stdout)

    def test_hash_compatibility_and_legacy_dispatch(self):
        for fallback in (False, True):
            self.queue.unlink(missing_ok=True)
            self.journal.unlink(missing_ok=True)
            self.add(fallback=fallback)
            task = json.loads(self.queue.read_text(encoding="utf-8"))[0]
            fields = {key: task[key] for key in ("worker", "brief", "signals")}
            if fallback:
                fields["fallback_workers"] = ["W2"]
            canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            self.assertEqual(task["spec_hash"], hashlib.sha256(canonical.encode("utf-8")).hexdigest())
            if not fallback:
                for key in ("spec_version", "fallback_workers", "spec_history"):
                    task.pop(key)
                self.queue.write_text(json.dumps([task]), encoding="utf-8")
            self.ok("dispatch", "--id", "A")

    def test_fallback_and_version(self):
        self.add()
        self.reject("WORKER_NOT_ALLOWED", "dispatch", "--id", "A", "--worker", "W3", "--reason", "x")
        self.reject("REASON_REQUIRED", "dispatch", "--id", "A", "--worker", "W2")
        receipt = json.loads(self.ok("dispatch", "--id", "A", "--worker", "W2", "--reason", "capacity").stdout)
        self.assertEqual((receipt["worker"], receipt["worker_switched_from"], receipt["spec_version"]), ("W2", "W1", 1))
        self.assertEqual(json.loads(self.queue.read_text())[0]["spec_version"], 1)
        self.reject("TASK_BUSY", "add-signal", "--id", "A", "--signal", "file:new", "--reason", "x", "--by", "AO")

    def test_signal_amendment_and_approval(self):
        self.add()
        self.reject("DUPLICATE_SIGNAL", "add-signal", "--id", "A", "--signal", f"file:{self.output}", "--reason", "x", "--by", "AO")
        self.reject("SIGNAL_INVALID", "add-signal", "--id", "A", "--signal", "bad", "--reason", "x", "--by", "AO")
        self.ok("add-signal", "--id", "A", "--signal", "file:new", "--reason", "stronger", "--by", "AO")
        self.assertEqual(self.state()["tasks"][0]["spec_version"], 2)
        self.reject("NO_MACHINE_SIGNAL", "propose-amendment", "--id", "A", "--by", "AO", "--reason", "x", "--evidence", "e", "--signal", "bad")
        self.ok("propose-amendment", "--id", "A", "--by", "AO", "--reason", "x", "--evidence", "e", "--worker", "W3")
        self.assertEqual(self.state()["tasks"][0]["worker"], "W1")
        self.reject("APPROVAL_REF_INVALID", "approve-amendment", "--amendment-id", "1", "--approval-ref", "bad:1")
        log = self.root / "approvals.md"
        log.write_text("APPROVAL BY OWNER amendment A1\n", encoding="utf-8")
        self.ok("approve-amendment", "--amendment-id", "1", "--approval-ref", f"{log}:1", "--approvals-log", str(log))
        self.assertEqual(self.state()["tasks"][0]["worker"], "W3")
        self.reject("ALREADY_APPLIED", "approve-amendment", "--amendment-id", "1", "--approval-ref", f"{log}:1", "--approvals-log", str(log))
        self.assertEqual(self.state()["journal_verify"], "PASS")

    def test_concurrent_add_signal(self):
        self.add()
        for cycle in range(3):
            with ThreadPoolExecutor(max_workers=5) as pool:
                results = list(pool.map(lambda i: self.cli("add-signal", "--id", "A", "--signal", f"file:{cycle}-{i}", "--reason", "more", "--by", "AO"), range(5)))
            self.assertTrue(all(result.returncode == 0 for result in results))
        task = json.loads(self.queue.read_text())[0]
        self.assertEqual(task["spec_version"], 16)
        self.assertEqual(len({h["version"] for h in task["spec_history"]}), 16)
        self.assertEqual(self.state()["journal_verify"], "PASS")

    def test_stale_reject_and_legacy(self):
        self.add()
        self.ok("propose-amendment", "--id", "A", "--by", "AO", "--reason", "x", "--evidence", "e", "--worker", "W3")
        self.ok("add-signal", "--id", "A", "--signal", "file:extra", "--reason", "more", "--by", "AO")
        log = self.root / "approvals.md"
        log.write_text("APPROVAL BY OWNER amendment 1\n", encoding="utf-8")
        self.reject("STALE_AMENDMENT", "approve-amendment", "--amendment-id", "1", "--approval-ref", f"{log}:1", "--approvals-log", str(log))
        self.ok("reject-amendment", "--amendment-id", "1", "--reason", "stale")
        self.reject("ALREADY_REJECTED", "reject-amendment", "--amendment-id", "1", "--reason", "again")
        self.reject("AMENDMENT_NOT_FOUND", "reject-amendment", "--amendment-id", "9", "--reason", "x")
        self.ok("add", "--id", "OLD", "--title", "old")
        self.reject("NO_SPEC", "add-signal", "--id", "OLD", "--signal", "file:x", "--reason", "x", "--by", "AO")
        self.assertNotIn("propose_amendment", self.state()["tasks"][1]["allowed_actions"])

    def test_concurrent_proposals(self):
        self.add()
        for cycle in range(3):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda i: self.cli("propose-amendment", "--id", "A", "--by", "AO",
                    "--reason", "x", "--evidence", "e", "--worker", f"W{cycle}-{i}"), range(4)))
            self.assertTrue(all(result.returncode == 0 for result in results))
        ids = [row["amendment_id"] for row in self.state()["pending_amendments"]]
        self.assertEqual(sorted(ids), list(range(1, 13)))
        self.assertEqual(self.state()["journal_verify"], "PASS")


if __name__ == "__main__":
    unittest.main()
