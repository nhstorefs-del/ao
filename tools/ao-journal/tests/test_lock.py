"""Interprocess lock regressions using real CLI processes and temporary files."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = self.root / "journal.jsonl"
        self.brief = self.root / "brief.md"
        self.brief.write_text("brief", encoding="utf-8")
        self.output = self.root / "output.txt"
        self.output.write_text("result", encoding="utf-8")

    def command(self, *args):
        return [sys.executable, str(ROOT / "journal.py"), "--journal", str(self.journal), *args]

    def run_cli(self, *args):
        return subprocess.run(self.command(*args), capture_output=True, text=True, encoding="utf-8")

    def concurrent(self, count, *args):
        processes = [subprocess.Popen(self.command(*args), stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, encoding="utf-8")
                     for _ in range(count)]
        outcomes = []
        for process in processes:
            stdout, stderr = process.communicate()
            outcomes.append((process.returncode, stdout, stderr))
        return outcomes

    def rows(self):
        if not self.journal.exists():
            return []
        return [json.loads(line) for line in self.journal.read_text(encoding="utf-8").splitlines()]

    def dispatch_args(self):
        return ("append", "--task-id", "R", "--worker", "w", "--brief", str(self.brief),
                "--signal", f"file:{self.output}")

    def assert_clean(self):
        self.assertFalse(Path(str(self.journal) + ".lock").exists())

    def test_concurrent_dispatch_and_accept_fifteen_rounds(self):
        for round_number in range(15):
            with self.subTest(round=round_number):
                self.journal.unlink(missing_ok=True)
                outcomes = self.concurrent(8, *self.dispatch_args())
                self.assertEqual(sum(code == 0 for code, _, _ in outcomes), 1, outcomes)
                self.assertEqual(sum("ATTEMPT_IN_FLIGHT" in out + err
                                     for _, out, err in outcomes), 7, outcomes)
                self.assertEqual([row["status"] for row in self.rows()], ["dispatched"])
                self.assertEqual(self.run_cli("complete", "--task-id", "R", "--attempt-id", "R#1",
                                              "--status", "done", "--summary", "done").returncode, 0)
                outcomes = self.concurrent(6, "accept", "--task-id", "R", "--by", "owner")
                self.assertTrue(all(code == 0 for code, _, _ in outcomes), outcomes)
                self.assertEqual(sum("ALREADY_ACCEPTED" in out for _, out, _ in outcomes), 5, outcomes)
                rows = self.rows()
                self.assertEqual(sum(row["status"] == "accepted" for row in rows), 1)
                self.assertEqual([row["seq"] for row in rows], list(range(1, len(rows) + 1)))
                self.assertEqual(self.run_cli("verify").returncode, 0)
                self.assert_clean()

    def test_stale_lock_and_business_error_release(self):
        lock = Path(str(self.journal) + ".lock")
        lock.write_text("dead owner", encoding="ascii")
        old = time.time() - 65
        os.utime(lock, (old, old))
        self.assertEqual(self.run_cli(*self.dispatch_args()).returncode, 0)
        self.assert_clean()
        before = self.journal.read_bytes()
        result = self.run_cli("accept", "--task-id", "missing", "--by", "owner")
        self.assertIn("NOT_DONE", result.stdout)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.journal.read_bytes(), before)
        self.assert_clean()

    def test_fresh_lock_times_out_without_changing_journal(self):
        self.assertEqual(self.run_cli(*self.dispatch_args()).returncode, 0)
        before = self.journal.read_bytes()
        lock = Path(str(self.journal) + ".lock")
        holder_code = ("import sys, time; from file_lock import file_lock; "
                       "guard = file_lock(sys.argv[1]); guard.__enter__(); "
                       "print('LOCKED', flush=True); time.sleep(30); guard.__exit__(None, None, None)")
        holder = subprocess.Popen([sys.executable, "-c", holder_code, str(self.journal)],
                                  cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, encoding="utf-8")
        try:
            self.assertEqual(holder.stdout.readline().strip(), "LOCKED")
            owner = lock.read_text(encoding="ascii")
            result = self.run_cli("checkpoint", "--task-id", "R", "--by", "owner",
                                  "--next-action", "wait")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("LOCK_TIMEOUT", result.stdout)
            self.assertEqual(self.journal.read_bytes(), before)
            self.assertEqual(lock.read_text(encoding="ascii"), owner)
        finally:
            holder.terminate()
            holder.communicate(timeout=5)
            lock.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
