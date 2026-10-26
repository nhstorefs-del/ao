import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import haiku_worker as worker


class FakeProcess:
    pid = 456
    def __init__(self, events):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(json.dumps(event) + "\n" for event in events))
        self.stderr = io.StringIO()
        self.returncode = None
        self.killed = False
    def poll(self):
        return self.returncode
    def wait(self, timeout=None):
        self.returncode = 9 if self.killed else 0
        return self.returncode
    def kill(self):
        self.killed = True
        self.returncode = 9


def event(mid, incoming, outgoing=10, cache_create=0, cache_read=0):
    return {"type": "assistant", "message": {"id": mid, "usage": {
        "input_tokens": incoming, "output_tokens": outgoing,
        "cache_creation_input_tokens": cache_create,
        "cache_read_input_tokens": cache_read}}}


class HaikuWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "usage.jsonl"
        self.config = Path(self.temp.name) / "config"
        self.patches = [patch.object(worker, "USAGE_FILE", self.log),
                        patch.object(worker, "CONFIG_DIR", self.config),
                        patch.object(worker, "load_credentials", return_value=("FAKE_KEY", "haiku-test"))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def invoke(self, brief, count, events=None):
        out, err = io.StringIO(), io.StringIO()
        process = FakeProcess(events or [])
        fake_stdin = SimpleNamespace(buffer=io.BytesIO(brief.encode("utf-8")))
        with patch.object(worker, "count_brief_tokens", return_value=count), \
             patch.object(worker.subprocess, "Popen", return_value=process) as popen, \
             patch.object(sys, "stdin", fake_stdin), patch.object(sys, "stdout", out), \
             patch.object(sys, "stderr", err), patch.object(worker, "kill_tree",
                  side_effect=lambda p: p.kill()):
            code = worker.main()
        return code, out.getvalue(), err.getvalue(), popen, process

    def test_large_brief_rejected_before_claude(self):
        code, _, err, popen, _ = self.invoke("large", 100_000)
        self.assertEqual(code, 3)
        self.assertIn("Brief ~100000 token", err)
        popen.assert_not_called()
        self.assertEqual(json.loads(self.log.read_text())["result"], "LOI")

    def test_count_failure_fallback_rejects_long_brief(self):
        with patch.object(worker, "urlopen", side_effect=OSError("offline")):
            estimate = worker.count_brief_tokens("x" * 180_000, "FAKE_KEY", "test")
        self.assertEqual(estimate, 95_000)
        code, _, _, popen, _ = self.invoke("x" * 180_000, estimate)
        self.assertEqual(code, 3)
        popen.assert_not_called()

    def test_turn_over_cap_kills(self):
        events = [event("m1", 50_000, cache_read=45_000),
                  {"type": "result", "result": "too late"}]
        code, _, err, _, process = self.invoke("brief", 5001, events)
        self.assertEqual(code, 4)
        self.assertTrue(process.killed)
        self.assertIn("CHAM_TRAN_TOKEN", err)
        self.assertEqual(json.loads(self.log.read_text())["result"], "CHAM_TRAN")

    def test_total_over_cap_kills(self):
        events = [event(f"m{i}", 80_000, 1000) for i in range(5)]
        code, _, _, _, process = self.invoke("brief", 5001, events)
        self.assertEqual(code, 4)
        self.assertTrue(process.killed)

    def test_normal_stream_one_usage_line(self):
        events = [event("m1", 20_000, 100), event("m1", 20_000, 100),
                  {"type": "result", "result": "done"}]
        code, out, _, popen, _ = self.invoke("brief", 5001, events)
        self.assertEqual(code, 0)
        self.assertEqual(out, "done")
        self.assertIn("--output-format", popen.call_args.args[0])
        rows = self.log.read_text().splitlines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0])["total_input"], 20_000)

    def test_env_cannot_change_cap(self):
        with patch.dict(os.environ, {"HAIKU_TRAN": "999999"}):
            self.assertEqual(worker.TRAN_MOI_LUOT, 90_000)
            self.assertNotIn("HAIKU_TRAN", worker.child_env("FAKE_KEY"))
            code, _, _, popen, _ = self.invoke("brief", 95_000)
        self.assertEqual(code, 3)
        popen.assert_not_called()

    def test_missing_usage_fails_closed(self):
        code, _, err, _, process = self.invoke("brief", 5001,
                                               [{"type": "result", "result": "done"}])
        self.assertEqual(code, 2)
        self.assertIn("usage", err)


if __name__ == "__main__":
    unittest.main()
