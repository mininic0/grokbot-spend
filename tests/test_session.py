import argparse
import base64
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from collector import grok_spend, session


class SessionWorkerTests(unittest.TestCase):
    def make_worker(self):
        args = argparse.Namespace(auth_helper=Path("synthetic-helper"), output=Path("synthetic-output"), team_id=None, no_prompt=True, skip_events=False)
        fingerprint = Mock(return_value=b"synthetic-session")
        reader = Mock(return_value="synthetic-token")
        collector = Mock()
        worker = session.SessionWorker(args, fingerprint=fingerprint, token_reader=reader, collector=collector)
        return worker, fingerprint, reader, collector

    def test_cached_refresh_reads_helper_once_and_suppresses_cli_output(self):
        worker, _, reader, collector = self.make_worker()
        collector.side_effect = lambda *a, **k: print("collector-metadata")
        output = io.StringIO()
        with patch("sys.stdout", output):
            self.assertEqual(worker.refresh(True), {"status": 0, "detail": None})
            self.assertEqual(worker.refresh(False)["status"], 0)
        reader.assert_called_once_with(worker.args.auth_helper, no_prompt=False)
        self.assertEqual(collector.call_count, 2)
        collector.assert_called_with(worker.args, token_override="synthetic-token")
        self.assertEqual(output.getvalue(), "")

    def test_account_change_invalidates_and_uses_only_noninteractive_helper(self):
        worker, fingerprint, reader, _ = self.make_worker()
        worker.refresh(True)
        fingerprint.return_value = b"other-session"
        reader.side_effect = grok_spend.AuthenticationError("Keychain permission unavailable.")
        self.assertEqual(worker.refresh(False)["status"], 2)
        self.assertIsNone(worker._token)
        reader.assert_called_with(worker.args.auth_helper, no_prompt=True)

    def test_account_switch_during_prompt_rejects_token_before_collection(self):
        worker, fingerprint, _, collector = self.make_worker()
        fingerprint.side_effect = [b"before", b"after"]
        self.assertEqual(worker.refresh(True)["status"], 2)
        collector.assert_not_called()
        self.assertIsNone(worker._token)

    def test_auth_failure_invalidates_cached_token(self):
        worker, _, reader, collector = self.make_worker()
        worker.refresh(True)
        collector.side_effect = grok_spend.AuthenticationError("Provider rejected the saved sign-in.")
        self.assertEqual(worker.refresh(False)["status"], 2)
        self.assertIsNone(worker._token)
        collector.side_effect = None
        worker.refresh(False)
        self.assertEqual(reader.call_count, 2)

    def test_network_failure_retains_cached_token(self):
        worker, _, reader, collector = self.make_worker()
        worker.refresh(True)
        collector.side_effect = grok_spend.CollectionError("Billing service unavailable.")
        self.assertEqual(worker.refresh(False)["status"], 1)
        self.assertEqual(worker._token, "synthetic-token")
        collector.side_effect = None
        self.assertEqual(worker.refresh(False)["status"], 0)
        self.assertEqual(reader.call_count, 1)

    def test_changed_store_unavailable_clears_cache(self):
        worker, fingerprint, _, _ = self.make_worker()
        worker.refresh(True)
        fingerprint.side_effect = grok_spend.AuthenticationError("Saved session unavailable.")
        self.assertEqual(worker.refresh(False)["status"], 2)
        self.assertIsNone(worker._token)

    def test_disconnect_and_quit_clear_cached_token(self):
        worker, _, _, _ = self.make_worker()
        worker.refresh(True)
        self.assertEqual(worker.command({"command": "disconnect"}), ({"status": 0, "detail": None}, False))
        self.assertIsNone(worker._token)
        worker.refresh(True)
        self.assertEqual(worker.command({"command": "quit"}), ({"status": 0, "detail": None}, True))
        self.assertIsNone(worker._token)

    def test_malformed_bounded_commands_do_not_invoke_helper(self):
        worker, _, reader, _ = self.make_worker()
        invalid = [b"not-json\n", b"[]\n", b'{"command":"refresh","allowPrompt":"true"}\n', b'{"command":"unknown"}\n', b"\xff\n", b"x" * (session.MAX_REQUEST_BYTES + 100) + b"\n"]
        output = io.StringIO()
        session.serve(worker, io.BytesIO(b"".join(invalid) + b'{"command":"quit"}\n'), output)
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([r["status"] for r in responses], [1] * len(invalid) + [0])
        reader.assert_not_called()

    def test_eof_clears_token_and_protocol_never_contains_secret(self):
        worker, _, _, collector = self.make_worker()
        collector.side_effect = grok_spend.CollectionError("unexpected synthetic-token")
        output = io.StringIO()
        session.serve(worker, io.BytesIO(b'{"command":"refresh","allowPrompt":true}\n'), output)
        self.assertNotIn("synthetic-token", output.getvalue())
        self.assertIsNone(worker._token)

    def test_worker_termination_also_terminates_inflight_child(self):
        code = """
from collector.session import install_shutdown
import subprocess, sys, time
class SyntheticWorker:
    def clear(self): pass
install_shutdown(SyntheticWorker())
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
print('ready', flush=True)
time.sleep(60)
"""
        process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            process.terminate()
            # The child inherits stdout: communicate cannot reach EOF until it
            # also exits. This exercises cancellation without reading Keychain.
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, "")
            self.assertEqual(process.returncode, -signal.SIGTERM)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)


class SessionFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "store.json"

    def write_store(self, active="synthetic-a", cipher=b"v10" + b"a" * 16, version=1):
        accounts = {"active": active, "accounts": {active: {"cursor-access-token": base64.b64encode(cipher).decode()}}}
        self.path.write_text(json.dumps({"version": version, "cursor-accounts": json.dumps(accounts)}))

    def test_fingerprint_changes_for_active_slot_or_encrypted_token(self):
        self.write_store()
        first = session.session_fingerprint(self.path)
        self.assertEqual(first, session.session_fingerprint(self.path))
        self.write_store(active="synthetic-b")
        second = session.session_fingerprint(self.path)
        self.write_store(active="synthetic-b", cipher=b"v10" + b"b" * 16)
        self.assertEqual(len(first), 32)
        self.assertNotEqual(first, second)
        self.assertNotEqual(second, session.session_fingerprint(self.path))

    def test_symlink_and_nonregular_files_rejected(self):
        self.write_store()
        link = Path(self.temp.name) / "link.json"
        link.symlink_to(self.path)
        fifo = Path(self.temp.name) / "fifo"
        os.mkfifo(fifo)
        for path in [link, Path(self.temp.name), fifo]:
            with self.assertRaises(grok_spend.AuthenticationError):
                session.session_fingerprint(path)

    def test_invalid_versions_ciphers_and_missing_active_are_rejected(self):
        for version in [True, 2, "1"]:
            self.write_store(version=version)
            with self.assertRaises(grok_spend.AuthenticationError):
                session.session_fingerprint(self.path)
        for cipher in [b"v20" + b"a" * 16, b"v10", b"v10invalid"]:
            self.write_store(cipher=cipher)
            with self.assertRaises(grok_spend.AuthenticationError):
                session.session_fingerprint(self.path)
        self.path.write_text('{"version":1,"cursor-accounts":"{}"}')
        with self.assertRaises(grok_spend.AuthenticationError):
            session.session_fingerprint(self.path)

    def test_wrong_owner_and_oversized_store_rejected(self):
        self.write_store()
        with patch.object(session.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(grok_spend.AuthenticationError):
                session.session_fingerprint(self.path)
        with self.path.open("wb") as stream:
            stream.truncate(session.MAX_STORE_BYTES + 1)
        with self.assertRaises(grok_spend.AuthenticationError):
            session.session_fingerprint(self.path)


if __name__ == "__main__":
    unittest.main()
