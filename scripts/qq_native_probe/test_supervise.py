# SPDX-License-Identifier: MIT
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from supervise import read_updates, supervise


class SupervisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_child(self, body, **kwargs):
        file = self.root / "child.py"
        file.write_text(body, encoding="utf8")
        return supervise(
            [sys.executable, str(file)],
            directory=self.root,
            prefix="case",
            terminal_statuses={"done"},
            timeout=5,
            exit_grace=0.2,
            **kwargs,
        )

    def test_partial_appended_record_waits_for_its_newline(self):
        status = self.root / "progress.jsonl"
        status.write_bytes(b'{"status":')
        self.assertEqual(read_updates(status, 0), ([], 0))
        with status.open("ab") as writer:
            writer.write(b'"ready"}\n')
        updates, offset = read_updates(status, 0)
        self.assertEqual(updates, [{"status": "ready"}])
        self.assertEqual(read_updates(status, offset), ([], offset))

    def test_reader_does_not_block_append_and_keeps_record_order(self):
        status = self.root / "progress.jsonl"
        status.write_bytes(b'{"status":"old"}\n')
        with status.open("rb") as reader:
            with status.open("ab") as writer:
                writer.write(b'{"status":"new"}\n')
            self.assertEqual(json.loads(reader.readline()), {"status": "old"})
        updates, _ = read_updates(status, 0)
        self.assertEqual(updates, [{"status": "old"}, {"status": "new"}])

    def test_normal_exit_has_real_zero_exit_code(self):
        r = self.run_child(
            "import pathlib; pathlib.Path('case-status.jsonl').write_text('{\"status\":\"done\"}\\n')"
        )
        self.assertTrue(r["graceful_exit_verified"])
        self.assertTrue(r["owned_process_exited"])
        self.assertEqual(r["child_exit_code"], 0)

    def test_completed_but_hung_child_is_not_graceful_success(self):
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], creationflags=subprocess.CREATE_NO_WINDOW
        )
        try:
            r = self.run_child(
                "import pathlib,time; pathlib.Path('case-status.jsonl').write_text('{\"status\":\"done\"}\\n'); time.sleep(30)"
            )
            self.assertEqual(r["forced_cleanup_reason"], "exit_deadline")
            self.assertFalse(r["graceful_exit_verified"])
            self.assertTrue(r["owned_process_exited"])
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=3)

    def test_nonzero_exit_cannot_be_success(self):
        r = self.run_child("raise SystemExit(7)")
        self.assertEqual(r["child_exit_code"], 7)
        self.assertFalse(r["graceful_exit_verified"])

    def test_injected_interruption_is_recorded(self):
        r = self.run_child(
            "import pathlib,time; pathlib.Path('case-status.jsonl').write_text('{\"status\":\"ready\"}\\n'); time.sleep(30)",
            interrupt_at="ready",
        )
        self.assertEqual(r["forced_cleanup_reason"], "injected_interruption")
        self.assertTrue(r["owned_process_exited"])
        self.assertFalse(r["graceful_exit_verified"])

    def test_crash_mid_record_preserves_last_complete_status(self):
        r = self.run_child(
            "import pathlib; pathlib.Path('case-status.jsonl').write_bytes("
            'b\'{"status":"working"}\\n{"status":\'); raise SystemExit(9)'
        )
        self.assertEqual(r["probe_status"], "working")
        self.assertEqual(r["child_exit_code"], 9)
        self.assertFalse(r["graceful_exit_verified"])
        self.assertTrue(r["owned_process_exited"])

    def test_malformed_complete_record_reports_error_and_cleans_child(self):
        with self.assertRaises(json.JSONDecodeError):
            self.run_child(
                "import pathlib,time; pathlib.Path('case-status.jsonl').write_bytes(b'bad json\\n'); "
                "time.sleep(30)"
            )
        report = json.loads((self.root / "case-supervision.json").read_text())
        self.assertIn("JSONDecodeError", report["supervisor_error"])
        self.assertTrue(report["owned_process_exited"])
        self.assertFalse(report["graceful_exit_verified"])

    def test_incomplete_utf8_record_is_not_decoded_until_complete(self):
        status = self.root / "progress.jsonl"
        record = '{"status":"同步完成"}\n'.encode("utf8")
        split = record.index("步".encode("utf8")) + 1
        status.write_bytes(record[:split])
        self.assertEqual(read_updates(status, 0), ([], 0))
        with status.open("ab") as writer:
            writer.write(record[split:])
        self.assertEqual(read_updates(status, 0), ([{"status": "同步完成"}], len(record)))

    def test_no_progress_is_bounded_by_probe_deadline(self):
        r = supervise(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            directory=self.root,
            prefix="silent",
            terminal_statuses={"done"},
            timeout=0.2,
        )
        self.assertEqual(r["forced_cleanup_reason"], "probe_deadline")
        self.assertTrue(r["owned_process_exited"])
        self.assertFalse(r["graceful_exit_verified"])

    def test_prior_evidence_is_never_overwritten(self):
        original = b'{"status":"prior"}\n'
        evidence = self.root / "case-status.jsonl"
        evidence.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "fresh evidence prefix"):
            self.run_child("raise SystemExit(0)")
        self.assertEqual(evidence.read_bytes(), original)
        self.assertFalse((self.root / "case-supervision.json").exists())


if __name__ == "__main__":
    unittest.main()
