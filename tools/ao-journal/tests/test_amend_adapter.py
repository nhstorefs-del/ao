"""AMEND adapter contracts with a fake coordinator and disposable state."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "adapters" / "ao_adapter.py"


class AmendAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.queue = self.root / "queue.json"
        self.journal = self.root / "journal.jsonl"
        self.brief = self.root / "brief.md"
        self.brief.write_text("brief", encoding="utf-8")
        self.signal = f"file:{self.root / 'output.txt'}"
        self.policy = self.root / "CLAUDE.md"
        self.policy.write_text("## Sổ việc bền (durable workflow)\nPolicy\n", encoding="utf-8")
        self.workers = self.root / "workers.json"
        self.workers.write_text(json.dumps({"W2": {"command": [sys.executable, "-c", "pass"],
                                                   "timeout_s": 5}}), encoding="utf-8")
        self.fake = self.root / "fake.json"
        self.core("add", "--id", "A", "--title", "A", "--workflow", "W", "--worker", "W1",
                  "--brief", str(self.brief), "--signal", self.signal, "--fallback-worker", "W2")

    def core(self, *args):
        result = subprocess.run([sys.executable, str(ROOT / "queue.py"), "--queue", str(self.queue),
                                 "--journal", str(self.journal), *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def turn(self, action):
        self.fake.write_text(json.dumps(action), encoding="utf-8")
        result = subprocess.run([sys.executable, str(ADAPTER), "turn", "--coordinator", "fake",
                                 "--workflow", "W", "--queue", str(self.queue), "--journal", str(self.journal),
                                 "--workers", str(self.workers), "--claude-md", str(self.policy),
                                 "--turn-log", str(self.root / "turn.jsonl"), "--fake-script", str(self.fake)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)["outcome"]

    def test_actions_and_core_authority(self):
        self.assertEqual(self.turn({"action": "switch_worker", "task_id": "A",
                                    "fallback_worker": "other", "reason": "x"}),
                         "CORE_REFUSED:WORKER_NOT_ALLOWED")
        self.assertEqual(self.turn({"action": "add_signal", "task_id": "A",
                                    "new_signal": "file:more", "reason": "more evidence"}), "APPLIED")
        task = json.loads(self.queue.read_text(encoding="utf-8"))[0]
        self.assertEqual(task["signals"], [self.signal, "file:more"])
        self.assertEqual(self.turn({"action": "propose_amendment", "task_id": "A", "reason": "change",
                                    "evidence": "review", "proposed_worker": "W3"}), "APPLIED")
        self.assertEqual(json.loads(self.queue.read_text(encoding="utf-8"))[0]["worker"], "W1")
        self.assertEqual(self.turn({"action": "switch_worker", "task_id": "A",
                                    "fallback_worker": "W2", "reason": "capacity"}), "APPLIED")
        records = [json.loads(line) for line in self.journal.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(row.get("worker") == "W2" and row.get("status") == "dispatched" for row in records))

    def test_rejected_keys_and_actions(self):
        for action in ("approve_amendment", "approve", "reject_amendment"):
            self.assertEqual(self.turn({"action": action, "task_id": "A"}),
                             "ADAPTER_REJECTED:ACTION_NOT_ALLOWED")
        for key in ("worker", "brief", "signal", "signals", "command"):
            self.assertEqual(self.turn({"action": "add_signal", "task_id": "A", "new_signal": "file:new",
                                        "reason": "x", key: None}), "ADAPTER_REJECTED:FORBIDDEN_FIELD")
        self.assertEqual(self.turn({"action": "add_signal", "task_id": "A", "new_signal": "file:new",
                                    "reason": "x", "fallback_worker": None}),
                         "ADAPTER_REJECTED:FORBIDDEN_FIELD")
        self.assertEqual(self.turn({"action": "verify", "task_id": "A"}),
                         "ADAPTER_REJECTED:ACTION_NOT_ALLOWED")
