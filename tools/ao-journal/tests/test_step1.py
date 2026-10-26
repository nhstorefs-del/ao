"""Step 1 behavior through the public CLI, using only temporary files."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Step1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = self.root / "journal.jsonl"
        self.queue = self.root / "queue.json"
        self.brief = self.root / "brief.md"
        self.brief.write_text("test brief", encoding="utf-8")
        self.output = self.root / "output.txt"

    def cli(self, script, *args, ok=True):
        flags = (["--queue", str(self.queue)] if script == "queue.py" else [])
        flags += ["--journal", str(self.journal)]
        result = subprocess.run(
            [sys.executable, str(ROOT / script), *flags, *args],
            capture_output=True, text=True, encoding="utf-8",
        )
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def add_pair(self):
        self.cli("queue.py", "add", "--id", "A", "--title", "Parent")
        self.cli("queue.py", "add", "--id", "B", "--title", "Child", "--depends-on", "A")

    def dispatch_done(self, signal=None):
        args = ["append", "--task-id", "A", "--worker", "worker", "--brief", str(self.brief)]
        if signal is not None:
            args += ["--signal", signal]
        self.cli("journal.py", *args)
        self.cli("journal.py", "complete", "--task-id", "A", "--attempt-id", "A#1", "--status", "done", "--summary", "finished")

    def receipts(self):
        return [json.loads(line) for line in self.journal.read_text(encoding="utf-8").splitlines()]

    def test_happy_path_and_hash_chain(self):
        self.add_pair()
        self.output.write_text("result", encoding="utf-8")
        self.dispatch_done(f"file:{self.output} exists")
        self.cli("queue.py", "sync")
        before = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual(before[1]["status"], "waiting")
        self.cli("journal.py", "accept", "--task-id", "A", "--by", "coordinator", "--note", "checked")
        self.cli("queue.py", "sync")
        after = json.loads(self.queue.read_text(encoding="utf-8"))
        self.assertEqual([task["status"] for task in after], ["accepted", "ready"])
        receipt = self.receipts()[-1]
        self.assertEqual(receipt["accepted_by"], "coordinator")
        self.assertEqual(receipt["note"], "checked")
        self.assertEqual(receipt["evidence"], [{"signal": f"file:{self.output} exists", "passed": True}])
        self.assertEqual(receipt["output_hashes"][str(self.output)], hashlib.sha256(b"result").hexdigest())
        self.assertIn("VERIFY PASS", self.cli("journal.py", "verify").stdout)

    def test_accept_is_idempotent(self):
        self.output.write_text("result", encoding="utf-8")
        self.dispatch_done(f"file:{self.output}")
        self.cli("journal.py", "accept", "--task-id", "A", "--by", "owner")
        before = self.journal.read_bytes()
        result = self.cli("journal.py", "accept", "--task-id", "A", "--by", "owner")
        self.assertIn("ALREADY_ACCEPTED task=A", result.stdout)
        self.assertEqual(self.journal.read_bytes(), before)

    def test_accept_before_done_and_missing_task(self):
        self.cli("journal.py", "append", "--task-id", "A", "--worker", "w", "--brief", str(self.brief), "--signal", f"file:{self.output}")
        before = self.journal.read_bytes()
        self.assertIn("NOT_DONE", self.cli("journal.py", "accept", "--task-id", "A", "--by", "owner", ok=False).stdout)
        self.assertIn("NOT_DONE", self.cli("journal.py", "accept", "--task-id", "missing", "--by", "owner", ok=False).stdout)
        self.assertEqual(self.journal.read_bytes(), before)

    def test_failed_signal(self):
        self.dispatch_done(f"file:{self.output}")
        before = self.journal.read_bytes()
        self.assertIn("SIGNAL_FAILED", self.cli("journal.py", "accept", "--task-id", "A", "--by", "owner", ok=False).stdout)
        self.assertEqual(self.journal.read_bytes(), before)

    def test_no_signal(self):
        self.dispatch_done()
        before = self.journal.read_bytes()
        self.assertIn("NO_SIGNALS", self.cli("journal.py", "accept", "--task-id", "A", "--by", "owner", ok=False).stdout)
        self.assertEqual(self.journal.read_bytes(), before)

    def test_command_exit_evidence(self):
        self.dispatch_done(f'cmd:"{sys.executable}" -c "import sys; sys.exit(3)" exit=3')
        before=self.journal.read_bytes()
        refused=self.cli("journal.py", "accept", "--task-id", "A", "--by", "owner",ok=False)
        self.assertIn("SIGNAL_FAILED",refused.stdout)
        self.assertEqual(self.journal.read_bytes(),before)

    def test_state_json_is_read_only(self):
        self.add_pair()
        self.output.write_text("result", encoding="utf-8")
        self.dispatch_done(f"file:{self.output}")
        self.cli("journal.py", "accept", "--task-id", "A", "--by", "owner")
        old_queue = self.queue.read_bytes()
        old_journal = self.journal.read_bytes()
        snapshot = json.loads(self.cli("queue.py", "state", "--json").stdout)
        self.assertEqual(snapshot["journal_verify"], "PASS")
        self.assertEqual(snapshot["ready"], ["B"])
        self.assertEqual(snapshot["accepted"], ["A"])
        parent = snapshot["tasks"][0]
        # verified là seq 3 trước khi accepted được ghi ở seq 4.
        self.assertEqual(parent["latest_receipt_seq"], 4)
        self.assertEqual(parent["accepted_by"], "owner")
        self.assertTrue(parent["output_hashes"])
        self.assertEqual(parent["depends_on"], [])
        self.assertEqual(self.queue.read_bytes(), old_queue)
        self.assertEqual(self.journal.read_bytes(), old_journal)

    def test_old_receipts_remain_readable(self):
        self.add_pair()
        self.output.write_text("result", encoding="utf-8")
        self.dispatch_done(f"file:{self.output}")
        self.assertIn("VERIFY PASS", self.cli("journal.py", "verify").stdout)
        snapshot = json.loads(self.cli("queue.py", "state", "--json").stdout)
        self.assertEqual(snapshot["tasks"][0]["status"], "done")
        self.assertFalse(snapshot["tasks"][0]["accepted"])
        self.assertEqual(snapshot["tasks"][0]["output_hashes"], {})

    def test_state_reports_broken_chain(self):
        self.add_pair()
        self.dispatch_done()
        records = self.receipts()
        records[0]["worker"] = "tampered"
        self.journal.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
        snapshot = json.loads(self.cli("queue.py", "state", "--json").stdout)
        self.assertEqual(snapshot["journal_verify"], "FAIL")


if __name__ == "__main__":
    unittest.main()
