import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cost_report


class CostReportTests(unittest.TestCase):
    def make_job(self, root, job_id, worker, state, events, log=None, retries=0):
        folder = root / job_id
        folder.mkdir()
        (folder / "job.json").write_text(json.dumps({"id": job_id, "worker": worker, "attempt_id": f"{job_id}#{retries + 1}"}), encoding="utf-8")
        (folder / "status.json").write_text(json.dumps({"id": job_id, "state": state, "worker": worker, "retries": retries}), encoding="utf-8")
        (folder / "events.jsonl").write_text("\n".join(json.dumps(x) for x in events), encoding="utf-8")
        if log is not None:
            (folder / "worker.log").write_text(log, encoding="utf-8")

    def test_codex_total_missing_measurement_and_failed_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_job(root, "CODEX", "codex-2", "DONE_NEEDS_REVIEW", [
                {"ts": "2026-10-04T23:00:00+00:00", "type": "START"},
                {"ts": "2026-10-04T23:02:00+00:00", "type": "DONE_NEEDS_REVIEW"}],
                "work complete\ntokens used\n1,234\n")
            self.make_job(root, "FAILED", "deepseek-paid", "GAVE_UP", [
                {"ts": "2026-10-04T23:03:00+00:00", "type": "START"},
                {"ts": "2026-10-04T23:03:12+00:00", "type": "FAILED"},
                {"ts": "2026-10-04T23:03:13+00:00", "type": "GAVE_UP"}],
                "No usage information\n")
            rows = {x["id"]: x for x in cost_report.collect(root)}
            self.assertEqual(rows["CODEX"]["total_tokens"], 1234)
            self.assertIsNone(rows["CODEX"]["input_tokens"])
            self.assertEqual(rows["CODEX"]["seconds"], 120)
            self.assertEqual(rows["CODEX"]["day"], "2026-10-05")
            self.assertIsNone(rows["FAILED"]["total_tokens"])
            self.assertIn("lỗi/bỏ", rows["FAILED"]["flags"])
            self.assertIn("không có số đo", cost_report.make_markdown(list(rows.values())))

    def test_retry_and_active_log_is_not_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            events = [
                {"ts": "2026-10-05T00:00:00+00:00", "type": "START"},
                {"ts": "2026-10-05T00:11:00+00:00", "type": "FAILED"},
                {"ts": "2026-10-05T00:12:00+00:00", "type": "RETRY"},
                {"ts": "2026-10-05T00:13:00+00:00", "type": "START"},
                {"ts": "2026-10-05T00:14:00+00:00", "type": "DONE_NEEDS_REVIEW"}]
            self.make_job(root, "RETRY", "codex-main", "DONE_NEEDS_REVIEW", events,
                          "tokens used\n2,000\ntokens used\n300\n", retries=1)
            self.make_job(root, "ACTIVE", "codex-main", "RUNNING", events,
                          "tokens used\n999,999\n")
            rows = {x["id"]: x for x in cost_report.collect(root)}
            self.assertEqual(rows["RETRY"]["seconds"], 720)
            self.assertEqual(rows["RETRY"]["total_tokens"], 2300)
            self.assertEqual(rows["RETRY"]["attempts"], 2)
            self.assertIn("retry", rows["RETRY"]["flags"])
            self.assertIn(">10 phút", rows["RETRY"]["flags"])
            self.assertIsNone(rows["ACTIVE"]["total_tokens"])
            self.assertIsNone(rows["ACTIVE"]["seconds"])

    def test_copied_footer_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.make_job(root, "COPY", "codex-2", "DONE_NEEDS_REVIEW", [
                {"ts": "2026-10-05T00:00:00+00:00", "type": "START", "detail": "COPY#1 worker=codex-2"},
                {"ts": "2026-10-05T00:01:00+00:00", "type": "DONE_NEEDS_REVIEW"}],
                "=== COPY#1 codex-2 ===\nexec output:\ntokens used\n999,999\n"
                "codex final\ntokens used\n84,931\nDone\n")
            self.assertEqual(cost_report.collect(root)[0]["total_tokens"], 84931)

    def test_two_attempts_and_duplicate_footer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            events = [
                {"ts": "2026-10-05T00:00:00+00:00", "type": "START", "detail": "RETRY#1 worker=codex-2"},
                {"ts": "2026-10-05T00:01:00+00:00", "type": "FAILED"},
                {"ts": "2026-10-05T00:02:00+00:00", "type": "START", "detail": "RETRY#2 worker=codex-2"},
                {"ts": "2026-10-05T00:03:00+00:00", "type": "DONE_NEEDS_REVIEW"}]
            self.make_job(root, "RETRY", "codex-2", "DONE_NEEDS_REVIEW", events,
                          "=== RETRY#1 codex-2 ===\ntokens used\n1,000\n"
                          "=== RETRY#2 codex-2 ===\ntokens used\n2,000\n"
                          "tokens used\n2,000\n", retries=1)
            self.assertEqual(cost_report.collect(root)[0]["total_tokens"], 3000)


if __name__ == "__main__":
    unittest.main()
