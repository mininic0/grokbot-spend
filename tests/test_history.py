import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from collector.history import remember_ownership
from collector.grok_spend import store_snapshot
from collector.attribution import attribute_events, normalize_events


class OwnershipHistoryTests(unittest.TestCase):
    def test_rolling_history_and_removed_bot_do_not_erase_proven_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "snapshot.json")
            old = {"sand-subagent-old": {"botId": "b", "routineId": "r", "name": "Routine"}}
            new = {"sand-subagent-new": {"botId": "b", "routineId": "r", "name": "Routine"}}
            remember_ownership(output, "a", {"b": "Bot"}, old, "first")
            roster, index = remember_ownership(output, "a", {}, new, "second")
            self.assertEqual(roster, {"b": "Bot"})
            self.assertEqual(set(index), set(old) | set(new))
            self.assertEqual((output.parent / "ledger.sqlite3").stat().st_mode & 0o777, 0o600)
            with sqlite3.connect(output.parent / "ledger.sqlite3") as db:
                self.assertEqual(db.execute("SELECT first_seen,last_seen FROM ownership_runs WHERE conversation_id=?", ("sand-subagent-old",)).fetchone(), ("first", "first"))

    def test_account_switch_cannot_import_another_accounts_owner_or_name(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "snapshot.json")
            old = {"sand-subagent-same": {"botId": "b", "routineId": "r", "name": "Private"}}
            remember_ownership(output, "a", {"b": "Account A"}, old, "first")
            roster, index = remember_ownership(output, "b", {}, {}, "second")
            self.assertEqual((roster, index), ({}, {}))
            roster, index = remember_ownership(output, "a", {}, {}, "third")
            self.assertEqual(roster, {"b": "Account A"})
            self.assertEqual(index, old)

    def test_conflicting_owner_remains_ambiguous_on_future_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "snapshot.json")
            for owner in ("b1", "b2", "b1"):
                _, index = remember_ownership(output, "a", {owner: owner}, {"sand-subagent-run": {"botId": owner, "routineId": "r", "name": "Routine"}}, "now")
            self.assertTrue(index["sand-subagent-run"]["ambiguous"])
            self.assertEqual(len(index["sand-subagent-run"]["candidates"]), 2)

    def test_upgrade_imports_only_exact_same_account_routine_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "snapshot.json")
            def event(identifier, account="a", method="routine-run-id"):
                return {"eventId": identifier, "accountKey": account, "conversationId": "sand-subagent-" + identifier,
                        "attribution": {"method": method, "botId": "b", "routineId": "r", "routineName": "Routine"}}
            store_snapshot({"account": {"key": "a"}, "period": {"start": "start"}, "generatedAt": "now", "events": [event("valid"), event("wrong-account", "other"), event("unknown", method=None)]}, output)
            _, index = remember_ownership(output, "a", {"b": "Bot"}, {}, "now")
            self.assertEqual(set(index), {"sand-subagent-valid"})

    def test_upgrade_preserves_proven_owner_after_bot_disappears_from_roster(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "snapshot.json")
            event = {"eventId": "prior", "accountKey": "a", "conversationId": "sand-subagent-old",
                     "attribution": {"method": "routine-run-id", "botId": "removed-bot", "routineId": "r", "routineName": "Routine"}}
            store_snapshot({"account": {"key": "a"}, "period": {"start": "start"}, "generatedAt": "now", "events": [event]}, output)
            roster, index = remember_ownership(output, "a", {}, {}, "first")
            current = normalize_events([{"conversationId": "sand-subagent-old", "chargedCents": 20}], "a")
            result = attribute_events(current, roster, index)
            self.assertEqual(result["events"][0]["attribution"]["botId"], "removed-bot")
            # The bootstrap evidence must also survive the next empty refresh.
            self.assertEqual(remember_ownership(output, "a", {}, {}, "second"), (roster, index))


if __name__ == "__main__":
    unittest.main()
