"""Isolated CLI tests for one-turn Adapter behavior."""
import json
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "adapters" / "ao_adapter.py"


class AdapterTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.queue = self.root / "queue.json"
        self.journal = self.root / "journal.jsonl"
        self.log = self.root / "turn_log.jsonl"
        self.output_file = self.root / "response.txt"
        self.prompt = self.root / "prompt.txt"
        self.brief = self.root / "brief.txt"
        self.brief.write_text("brief", encoding="utf-8")
        self.policy = self.root / "CLAUDE.md"
        self.policy.write_text("# Other\nignored\n## Sổ việc bền (durable workflow)\nPolicy.\n## Later\nsecret transcript", encoding="utf-8")
        self.workers = self.root / "workers.json"
        self.worker = self.root / "worker.py"
        self.worker.write_text("import pathlib,sys\npathlib.Path(sys.argv[2]+'.out').write_text('ok')\n", encoding="utf-8")
        self.worker_command = [sys.executable, str(self.worker), "{brief}", "{task_id}", "{attempt_id}"]
        self.set_worker(self.worker_command)
        self.capabilities = ROOT / "adapters" / "capabilities"

    def set_worker(self, command, timeout=3, name="fixed"):
        self.workers.write_text(json.dumps({name: {"command": command, "timeout_s": timeout}}), encoding="utf-8")

    def core(self, script, *command):
        options = ["--journal", str(self.journal)]
        if script == "queue.py":
            options = ["--queue", str(self.queue), *options]
        proc = subprocess.run([sys.executable, str(ROOT / script), *options, *command],
                              capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout

    def add(self, task="A", depends=None, worker="fixed"):
        args = ["add", "--id", task, "--title", task, "--workflow", "W",
                "--worker", worker, "--brief", str(self.brief),
                "--signal", f"file:{task}.out"]
        if depends:
            args += ["--depends-on", depends]
        self.core("queue.py", *args)

    def turn(self, response, coordinator="fake", **overrides):
        self.output_file.write_text(response if isinstance(response, str) else json.dumps(response), encoding="utf-8")
        args = [sys.executable, str(ADAPTER), "turn", "--coordinator", coordinator,
                "--workflow", "W", "--queue", str(self.queue), "--journal", str(self.journal),
                "--workers", str(self.workers), "--turn-log", str(self.log),
                "--claude-md", str(self.policy), "--fake-output-file", str(self.output_file),
                "--prompt-out", str(self.prompt), "--capabilities-dir", str(self.capabilities)]
        for key, value in overrides.items():
            args += ["--" + key.replace("_", "-"), str(value)]
        proc = subprocess.run(args, cwd=self.root, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def qwen_setup(self, body, key="KEYMARKER_XYZ"):
        if not self.queue.exists():
            self.add()
        caps = self.root / "caps"
        caps.mkdir(exist_ok=True)
        source = json.loads((self.capabilities / "deepseek-qwen.json").read_text(encoding="utf-8"))
        env_file = self.root / "api.env"
        env_file.write_text("NVIDIA_API_KEY=" + key + "\n", encoding="utf-8")
        source["env_file"] = str(env_file)
        (caps / "qwen.json").write_text(json.dumps(source), encoding="utf-8")
        script = self.root / "fake_qwen.py"
        script.write_text("import sys\nsys.stdin.reconfigure(encoding='utf-8')\n" + body, encoding="utf-8")
        command = self.root / "fake_qwen.cmd"
        command.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return {"capabilities_dir": caps, "qwen_command": command}

    def test_qwen_legacy_backend_denied_before_key_or_process(self):
        marker=self.root/'backend_side_effect'
        options=self.qwen_setup("from pathlib import Path\n"+f"Path({str(marker)!r}).write_text('ran')\n")
        # Removing even the synthetic key file must not change the gate refusal.
        (self.root/'api.env').unlink()
        before=self.journal.read_bytes() if self.journal.exists() else None
        result=self.turn('',coordinator='qwen',**options)
        self.assertEqual(result['outcome'],'BACKEND_FAILED:DELEGATION_REQUIRED_UNBOUNDED_COORDINATOR_BACKEND')
        self.assertFalse(marker.exists())
        self.assertEqual(self.journal.read_bytes() if self.journal.exists() else None,before)
        self.assertNotIn('KEYMARKER_XYZ',json.dumps(result))

    def test_qwen_timeout_or_missing_command_cannot_bypass_gate(self):
        options=self.qwen_setup("raise AssertionError('must not run')\n")
        options['qwen_command']=self.root/'missing.cmd'
        for timeout in (.1,300):
            result=self.turn('',coordinator='qwen',backend_timeout=timeout,**options)
            self.assertEqual(result['outcome'],'BACKEND_FAILED:DELEGATION_REQUIRED_UNBOUNDED_COORDINATOR_BACKEND')

    def test_capability_timeout_metadata_remains_readable(self):
        options=self.qwen_setup('pass\n')
        spec=importlib.util.spec_from_file_location('ao_adapter',ADAPTER)
        adapter=importlib.util.module_from_spec(spec);spec.loader.exec_module(adapter)
        loaded=adapter.capability(options['capabilities_dir']/'qwen.json')
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.get('timeout_s',adapter.DEFAULT_BACKEND_TIMEOUT_S),300)

    def test_real_deepseek_answer_replay(self):
        self.add("A")
        self.add("B", "A")
        self.assertEqual(self.turn({"action": "dispatch", "task_id": "A"})["outcome"], "APPLIED")
        self.assertTrue((self.root / "A.out").exists())
        answer = (ROOT / "tests" / "fixtures" / "real_deepseek_answer_20261001.txt").read_text(encoding="utf-8")
        self.assertEqual(self.turn(answer)["outcome"], "APPLIED")
        rows = [json.loads(line) for line in self.journal.read_text(encoding="utf-8").splitlines()]
        verified = [row for row in rows if row.get("task_id") == "A" and row.get("status") == "verified"]
        self.assertEqual(len(verified), 1)
        self.assertTrue(verified[0]["passed"])
        self.assertNotIn("context_refs", verified[0])
        state = json.loads(self.core("queue.py", "state", "--json", "--workflow", "W"))
        self.assertEqual(next(row for row in state["tasks"] if row["id"] == "B")["status"], "waiting")

    def test_optional_nulls_and_field_guards(self):
        self.add("A")
        self.assertEqual(self.turn({"action": "dispatch", "task_id": "A"})["outcome"], "APPLIED")
        for field in ("reason", "next_action", "blocked_reason", "context_refs"):
            with self.subTest(optional_field=field):
                self.assertEqual(self.turn({"action": "verify", "task_id": "A", field: None})["outcome"], "APPLIED")
        self.assertEqual(self.turn({"action": "verify", "task_id": "A", "reason": None,
                                    "next_action": None, "blocked_reason": None,
                                    "context_refs": None})["outcome"], "APPLIED")
        self.assertEqual(self.turn({"action": "checkpoint", "task_id": None,
                                    "next_action": "wait"})["outcome"], "APPLIED")
        for field in ("brief", "worker", "signal", "command"):
            with self.subTest(forbidden_field=field):
                self.assertEqual(self.turn({"action": "verify", "task_id": "A", field: None})["outcome"],
                                 "ADAPTER_REJECTED:FORBIDDEN_FIELD")
        for field, value in (("task_id", 1), ("context_refs", "ref")):
            with self.subTest(invalid_field=field):
                self.assertEqual(self.turn({"action": "verify", "task_id": "A", field: value})["outcome"],
                                 "ADAPTER_REJECTED:INVALID_FIELD")
        self.assertEqual(self.turn({"action": None})["outcome"], "ADAPTER_REJECTED:ACTION_NOT_ALLOWED")
        self.assertEqual(self.turn({})["outcome"], "ADAPTER_REJECTED:ACTION_NOT_ALLOWED")
        self.assertEqual(self.turn({"action": "checkpoint", "next_action": None})["outcome"],
                         "ADAPTER_REJECTED:NEXT_ACTION_REQUIRED")
        self.assertEqual(self.turn({"action": "verify", "task_id": None})["outcome"],
                         "ADAPTER_REJECTED:UNKNOWN_TASK")

    def test_required_reasons_with_null(self):
        self.add("A")
        self.set_worker([sys.executable, "-c", "import sys;sys.exit(1)"])
        self.assertEqual(self.turn({"action": "dispatch", "task_id": "A"})["outcome"], "WORKER_FAILED:EXIT_1")
        self.assertEqual(self.turn({"action": "retry", "task_id": "A", "reason": None})["outcome"],
                         "ADAPTER_REJECTED:REASON_REQUIRED")
        self.assertEqual(self.turn({"action": "needs_owner", "blocked_reason": None})["outcome"],
                         "ADAPTER_REJECTED:REASON_REQUIRED")

    def test_happy_path_dependencies_and_done(self):
        for task, depends in (("A", None), ("B", "A"), ("C", "B")):
            self.add(task, depends)
        for task in ("A", "B", "C"):
            self.assertEqual(self.turn({"action": "dispatch", "task_id": task})["outcome"], "APPLIED")
            self.assertEqual(self.turn({"action": "verify", "task_id": task})["outcome"], "APPLIED")
            self.assertEqual(self.turn({"action": "accept", "task_id": task})["outcome"], "APPLIED")
        self.assertEqual(self.turn({"action": "done"})["outcome"], "APPLIED")
        rows = [json.loads(line) for line in self.journal.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sum(row["status"] == "accepted" for row in rows), 3)
        self.assertEqual(rows[-1]["next_action"], "WORKFLOW_DONE")

    def test_bad_output_and_preflight(self):
        self.add()
        for raw in ("bad", '{} {}', '[{"action":"dispatch"}]'):
            self.assertEqual(self.turn(raw)["outcome"], "MALFORMED_OUTPUT")
        self.assertEqual(self.turn('```json\n{"action":"dispatch","task_id":"A"}\n```')["outcome"], "APPLIED")
        before = self.journal.read_bytes()
        cases = [({"action": "dispatch", "task_id": "A"}, "ACTION_NOT_ALLOWED"),
                 ({"action": "retry", "task_id": "X"}, "UNKNOWN_TASK"),
                 ({"action": "done"}, "ACTION_NOT_ALLOWED"),
                 ({"action": "checkpoint"}, "NEXT_ACTION_REQUIRED")]
        for value, code in cases:
            self.assertEqual(self.turn(value)["outcome"], "ADAPTER_REJECTED:" + code)
        for field in ("brief", "worker", "signal", "signals", "command"):
            self.assertEqual(self.turn({"action": "verify", "task_id": "A", field: "x"})["outcome"],
                             "ADAPTER_REJECTED:FORBIDDEN_FIELD")
        self.assertEqual(self.journal.read_bytes(), before)

    def test_json_object_with_surrounding_text(self):
        self.add()
        obj = '{"action":"checkpoint","next_action":"x","context_refs":[]}'
        responses = (
            'Day la quyet dinh:\n```json\n' + obj + '\n```',
            'Quyet dinh cua toi: ' + obj,
            obj + '\nDa hoan tat quyet dinh.',
            '{"action":"checkpoint","next_action":"giu {dau ngoac}","context_refs":[]}',
        )
        for response in responses:
            with self.subTest(response=response):
                self.assertEqual(self.turn(response)["outcome"], "APPLIED")

    def test_json_object_count_and_top_level_array(self):
        self.add()
        obj = '{"action":"dispatch","task_id":"A"}'
        responses = (
            '```json\n' + obj + '\n```\n' + obj,
            obj + obj,
            'Chi co chu, khong co JSON object.',
            '[' + obj + ']',
        )
        for response in responses:
            with self.subTest(response=response):
                self.assertEqual(self.turn(response)["outcome"], "MALFORMED_OUTPUT")
                self.assertFalse(self.journal.exists())

    def test_retry_reason_and_worker_failures(self):
        self.add()
        self.set_worker([sys.executable, "-c", "import sys;sys.exit(127)"])
        self.assertEqual(self.turn({"action": "dispatch", "task_id": "A"})["outcome"], "WORKER_FAILED:EXIT_127")
        self.assertEqual(self.turn({"action": "retry", "task_id": "A"})["outcome"], "ADAPTER_REJECTED:REASON_REQUIRED")
        self.set_worker([sys.executable, "-c", "import sys;sys.exit(3)"])
        self.assertEqual(self.turn({"action": "retry", "task_id": "A", "reason": "again"})["outcome"], "WORKER_FAILED:EXIT_3")
        self.set_worker([sys.executable, "-c", "import time;time.sleep(2)"], timeout=.1)
        self.assertEqual(self.turn({"action": "retry", "task_id": "A", "reason": "again"})["outcome"], "WORKER_FAILED:WORKER_TIMEOUT")
        self.set_worker([], name="another")
        self.assertEqual(self.turn({"action": "retry", "task_id": "A", "reason": "again"})["outcome"], "WORKER_FAILED:WORKER_NOT_REGISTERED")
        self.set_worker([str(self.root / "missing-worker")])
        self.assertEqual(self.turn({"action": "retry", "task_id": "A", "reason": "again"})["outcome"], "WORKER_FAILED:WORKER_START_FAILED")
        self.assertEqual(len([r for r in self.journal.read_text(encoding="utf-8").splitlines() if '"status":"failed"' in r]), 5)

    def test_core_refusal_and_signals(self):
        self.add()
        queue = json.loads(self.queue.read_text(encoding="utf-8"))
        queue[0]["worker"] = "changed"
        self.queue.write_text(json.dumps(queue), encoding="utf-8")
        self.assertEqual(self.turn({"action": "dispatch", "task_id": "A"})["outcome"], "CORE_REFUSED:SPEC_CHANGED")
        queue[0]["worker"] = "fixed"
        self.queue.write_text(json.dumps(queue), encoding="utf-8")
        self.set_worker([sys.executable, "-c", "pass"])
        self.assertEqual(self.turn({"action": "dispatch", "task_id": "A"})["outcome"], "APPLIED")
        self.assertEqual(self.turn({"action": "accept", "task_id": "A"})["outcome"], "CORE_REFUSED:SIGNAL_FAILED")

    def test_capability_prompt_log_and_checkpoint(self):
        self.add()
        self.assertEqual(self.turn({"action": "done"}, coordinator="missing")["outcome"], "CAPABILITY_MISSING")
        self.assertFalse(self.prompt.exists())
        bad = self.root / "badcaps"
        bad.mkdir()
        (bad / "fake.json").write_text('{"invocation":"fake"}', encoding="utf-8")
        self.assertEqual(self.turn({"action": "done"}, capabilities_dir=bad)["outcome"], "CAPABILITY_MISSING")
        self.assertEqual(self.turn({"action": "needs_owner", "reason": "  "})["outcome"], "ADAPTER_REJECTED:REASON_REQUIRED")
        self.assertEqual(self.turn({"action": "done"})["outcome"], "ADAPTER_REJECTED:ACTION_NOT_ALLOWED")
        first = self.prompt.read_bytes()
        self.turn({"action": "done"})
        self.assertEqual(first, self.prompt.read_bytes())
        self.assertEqual(self.turn({"action": "checkpoint", "next_action": "wait"})["outcome"], "APPLIED")
        first = self.prompt.read_bytes()
        self.turn({"action": "checkpoint", "next_action": "wait"})
        self.assertNotEqual(first, self.prompt.read_bytes())  # Core state now contains the checkpoint.
        context = self.root / "context.txt"
        context.write_text("abcdefghij", encoding="utf-8")
        self.assertEqual(self.turn({"action": "checkpoint", "next_action": "read",
                                    "context_refs": [str(context)]})["outcome"], "APPLIED")
        self.turn({"action": "done"}, max_context_chars=4)
        prompt = json.loads(self.prompt.read_text(encoding="utf-8"))
        self.assertEqual(prompt["context_refs"][0]["content"], "abcd")
        self.assertNotIn(b"secret transcript", self.prompt.read_bytes())
        self.assertEqual(self.turn({"action": "needs_owner", "reason": "decision"})["outcome"], "APPLIED")
        self.assertIn("NEEDS_OWNER: decision", self.journal.read_text(encoding="utf-8"))
        self.assertNotIn("NVIDIA_API_KEY", self.log.read_text(encoding="utf-8"))

    def test_concurrent_dispatch(self):
        self.add()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.turn({"action": "dispatch", "task_id": "A"}), range(2)))
        self.assertEqual(sum(r["outcome"] == "APPLIED" for r in results), 1)
        self.assertFalse(list(self.root.rglob("*.lock")))


if __name__ == "__main__":
    unittest.main()
