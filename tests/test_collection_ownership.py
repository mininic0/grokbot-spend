"""Collector-level regressions for rolling ownership and inventory failure."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from collector.grok_spend import AuthenticationError, CollectionError, collect


class CollectionOwnershipTests(unittest.TestCase):
    def test_session_guard_prevents_writes_after_account_change(self):
        for denied_call in (1, 2):
            with self.subTest(denied_call=denied_call), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "snapshot.json"
                output.write_text('{"previous": true}')
                provider = Mock()
                provider.claims = {"sub": "synthetic-user"}
                responses = {
                    "GetMe": {"userId": 1},
                    "GetMonthlyBillingCycle": {"startDateEpochMillis": 1790812800000, "endDateEpochMillis": 1793491200000},
                }
                provider.dashboard.side_effect = lambda method, *args: responses.get(method, {})
                provider.routines.return_value = {"agents": [{"agentId": "bot-1", "automations": []}]}
                provider.events.return_value = []
                calls = []

                def guard():
                    calls.append(True)
                    if len(calls) == denied_call:
                        raise AuthenticationError("The active session changed.")

                args = argparse.Namespace(auth_helper=None, no_prompt=True, team_id=None, skip_events=False, output=output)
                with patch("collector.grok_spend.Provider", return_value=provider), \
                        patch("collector.grok_spend.read_roster", return_value={"bot-1": "Synthetic bot"}), \
                        patch("collector.grok_spend.time.time", return_value=1791896521), \
                        patch("collector.grok_spend.store_snapshot") as store, \
                        self.assertRaises(AuthenticationError):
                    collect(args, token_override="synthetic-token", before_write=guard)
                store.assert_not_called()
                self.assertEqual(json.loads(output.read_text()), {"previous": True})
                if denied_call == 1:
                    self.assertFalse((output.parent / "ledger.sqlite3").exists())
                    provider.events.assert_not_called()

    def test_catalog_whitelist_and_retained_ownership_after_provider_failure(self):
        provider = Mock()
        provider.claims = {"sub": "synthetic-user"}

        def dashboard(method, *args):
            if method == "GetMe":
                return {"userId": 1, "authId": "synthetic-user"}
            if method == "GetMonthlyBillingCycle":
                return {"startDateEpochMillis": 1790812800000, "endDateEpochMillis": 1793491200000}
            return {}

        provider.dashboard.side_effect = dashboard
        provider.routines.return_value = {"agents": [{"agentId": "bot-1", "automations": [{
            "automationId": "routine-1", "recordJson": json.dumps({
                "id": "routine-1", "name": "Synthetic watcher", "isEnabled": False,
                "prompt": "synthetic-private-prompt", "filePath": "synthetic-private-path",
                "runs": [{"id": "run-1", "requestId": "request-1", "startedAt": 1791896400000, "status": "ok"}],
            }),
        }]}]}
        provider.events.return_value = [{"eventId": "event-1", "timestamp": 1791896400000,
                                        "conversationId": "sand-subagent-run-1", "chargedCents": 125}]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.json"
            args = argparse.Namespace(auth_helper=None, no_prompt=True, team_id=None, skip_events=False, output=output)
            with patch("collector.grok_spend.Provider", return_value=provider), \
                    patch("collector.grok_spend.read_roster", return_value={"bot-1": "Synthetic bot"}), \
                    patch("collector.grok_spend.time.time", return_value=1791896521), \
                    contextlib.redirect_stdout(io.StringIO()):
                collect(args, token_override="synthetic-token")
                first = json.loads(output.read_text())
                self.assertEqual(first["automationCatalog"]["completeness"], "complete")
                self.assertIs(first["automationCatalog"]["automations"][0]["isEnabled"], False)
                self.assertEqual(first["events"][0]["attribution"]["routineId"], "routine-1")
                self.assertNotIn("synthetic-private", output.read_text())
                self.assertNotIn("synthetic-token", output.read_text())
                provider.routines.side_effect = CollectionError("Metadata temporarily unavailable")
                collect(args, token_override="synthetic-token")
            second = json.loads(output.read_text())
            self.assertEqual(second["automationCatalog"]["completeness"], "unknown")
            self.assertEqual(second["automationCatalog"]["automations"], [])
            self.assertEqual(second["events"][0]["attribution"]["routineId"], "routine-1")
            self.assertEqual(second["automationCoverage"]["currentRunLinks"], 0)
            self.assertEqual(second["automationCoverage"]["retainedRunLinks"], 1)
            self.assertFalse(second["automationCoverage"]["historicalComplete"])


if __name__ == "__main__":
    unittest.main()
