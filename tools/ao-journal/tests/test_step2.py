"""Step 2 gate behavior through CLI calls against temporary state."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Step2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = self.root / "journal.jsonl"
        self.queue = self.root / "queue.json"
        self.brief = self.root / "brief.md"
        self.brief.write_text("brief", encoding="utf-8")
        self.output = self.root / "out.txt"
        self.guard = self.root / "guard.txt"
        self.cli("queue.py", "add", "--id", "A", "--title", "Parent")
        self.cli("queue.py", "add", "--id", "B", "--title", "Child", "--depends-on", "A")

    def cli(self, script, *args):
        options = ["--journal", str(self.journal)]
        if script == "queue.py":
            options = ["--queue", str(self.queue)] + options
        return subprocess.run([sys.executable, str(ROOT / script), *options, *args],
                              capture_output=True, text=True, encoding="utf-8")

    def rows(self):
        return [json.loads(line) for line in self.journal.read_text(encoding="utf-8").splitlines()]

    def setup_done(self, signal=True):
        args = ["append", "--task-id", "A", "--worker", "w", "--brief", str(self.brief)]
        if signal:
            args += ["--signal", f"file:{self.output}"]
        self.assertEqual(self.cli("journal.py", *args).returncode, 0)
        self.assertEqual(self.cli("journal.py", "complete", "--task-id", "A", "--attempt-id", "A#1", "--status", "done", "--summary", "done").returncode, 0)

    def rejected(self, code):
        result = self.cli("journal.py", "accept", "--task-id", "A", "--by", "owner")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(code, result.stdout)
        self.assertNotIn("accepted", [row["status"] for row in self.rows()])
        self.assertEqual(self.cli("queue.py", "sync").returncode, 0)
        tasks = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual(tasks[1]["status"], "waiting")

    def test_not_done(self):
        self.assertEqual(self.cli("journal.py", "append", "--task-id", "A", "--worker", "w", "--brief", str(self.brief)).returncode, 0)
        self.rejected("NOT_DONE")

    def test_no_signals(self):
        self.setup_done(signal=False)
        self.rejected("NO_SIGNALS")

    def test_signal_failed(self):
        self.setup_done()
        self.rejected("SIGNAL_FAILED")

    def test_brief_changed(self):
        self.output.write_text("ok", encoding="utf-8")
        self.setup_done()
        self.brief.write_text("changed", encoding="utf-8")
        self.rejected("BRIEF_CHANGED")

    def test_output_changed(self):
        self.output.write_text("one", encoding="utf-8")
        self.setup_done()
        self.assertEqual(self.cli("journal.py", "verify-task", "--task-id", "A").returncode, 0)
        self.output.write_text("two", encoding="utf-8")
        self.rejected("OUTPUT_CHANGED")

    def test_output_deleted(self):
        self.output.write_text("one", encoding="utf-8")
        self.setup_done()
        self.assertEqual(self.cli("journal.py", "verify-task", "--task-id", "A").returncode, 0)
        self.output.unlink()
        self.rejected("OUTPUT_CHANGED")

    def test_chain_broken(self):
        self.output.write_text("ok", encoding="utf-8")
        self.setup_done()
        rows = self.rows()
        rows[0]["worker"] = "tampered"
        self.journal.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        self.rejected("CHAIN_BROKEN")

    def test_guard_not_pass(self):
        self.output.write_text("ok", encoding="utf-8")
        self.setup_done()
        result = self.cli("journal.py", "accept", "--task-id", "A", "--by", "owner", "--guard-report", str(self.guard))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GUARD_NOT_PASS", result.stdout)
        self.assertNotIn("accepted", [row["status"] for row in self.rows()])
        self.assertEqual(self.cli("queue.py", "sync").returncode, 0)
        self.assertEqual(json.loads(self.queue.read_text(encoding="utf-8"))[1]["status"], "waiting")

    def test_guard_happy_path_auto_verify(self):
        self.output.write_text("ok", encoding="utf-8")
        self.setup_done()
        self.guard.write_text("GUARD_RESULT: PASS\n", encoding="utf-8")
        result = self.cli("journal.py", "accept", "--task-id", "A", "--by", "owner", "--guard-report", str(self.guard))
        self.assertEqual(result.returncode, 0, result.stdout)
        rows = self.rows()
        self.assertEqual([r["status"] for r in rows], ["dispatched", "done", "verified", "accepted"])
        self.assertEqual(rows[-1]["verified_seq"], rows[-2]["seq"])
        self.assertEqual(rows[-1]["output_hashes"][str(self.output)], hashlib.sha256(b"ok").hexdigest())
        self.assertIn("GUARD", rows[-1]["gate"])

    def test_guard_pass_exit_code_zero(self):
        self.output.write_text("ok", encoding="utf-8")
        self.setup_done()
        self.guard.write_text("GUARD_RESULT: PASS exit_code=0\n", encoding="utf-8")
        result = self.cli("journal.py", "accept", "--task-id", "A", "--by", "owner", "--guard-report", str(self.guard))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.rows()[-1]["status"], "accepted")

    def test_guard_pass_exit_code_one(self):
        self.output.write_text("ok", encoding="utf-8")
        self.setup_done()
        self.guard.write_text("GUARD_RESULT: PASS exit_code=1\n", encoding="utf-8")
        result = self.cli("journal.py", "accept", "--task-id", "A", "--by", "owner", "--guard-report", str(self.guard))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.rows()[-1]["status"], "accepted")

    def test_guard_warn_rejected(self):
        self.output.write_text("ok", encoding="utf-8")
        self.setup_done()
        self.guard.write_text("GUARD_RESULT: WARN N_OUT_OF_SCOPE=2 exit_code=0\n", encoding="utf-8")
        self.assert_guard_rejected()

    def test_guard_prefixed_pass_rejected(self):
        self.output.write_text("ok", encoding="utf-8")
        self.setup_done()
        self.guard.write_text("... GUARD_RESULT: PASS\n", encoding="utf-8")
        self.assert_guard_rejected()

    def test_guard_pass_then_warn_rejected(self):
        self.output.write_text("ok", encoding="utf-8")
        self.setup_done()
        self.guard.write_text("GUARD_RESULT: PASS exit_code=0\nGUARD_RESULT: WARN N_OUT_OF_SCOPE=2 exit_code=0\n", encoding="utf-8")
        self.assert_guard_rejected()

    def assert_guard_rejected(self):
        result = self.cli("journal.py", "accept", "--task-id", "A", "--by", "owner", "--guard-report", str(self.guard))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GUARD_NOT_PASS", result.stdout)
        self.assertNotIn("accepted", [row["status"] for row in self.rows()])

    def test_explicit_verify_then_accept(self):
        self.output.write_text("ok", encoding="utf-8")
        self.setup_done()
        self.assertEqual(self.cli("journal.py", "verify-task", "--task-id", "A").returncode, 0)
        before = self.rows()[-1]
        state = json.loads(self.cli("queue.py", "state", "--json").stdout)
        self.assertEqual(state["tasks"][0]["status"], "done")
        self.assertEqual(state["tasks"][0]["verified"], {"seq": before["seq"], "passed": True})
        self.assertEqual(self.cli("journal.py", "accept", "--task-id", "A", "--by", "owner").returncode, 0)
        self.assertEqual(self.rows()[-1]["verified_seq"], before["seq"])

    def test_failed_verify_stays_unaccepted(self):
        self.setup_done()
        self.assertEqual(self.cli("journal.py", "verify-task", "--task-id", "A").returncode, 0)
        self.assertFalse(self.rows()[-1]["passed"])
        self.rejected("SIGNAL_FAILED")


if __name__ == "__main__":
    unittest.main()
