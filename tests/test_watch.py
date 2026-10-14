"""Synthetic foreground capture: retained ownership, auth scope and failures."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

from collector import grok_spend, watch


class Clock:
    def __init__(self):
        self.elapsed = 0.0
        self.wall = 1_000.0
        self.sleeps = []

    def advance(self, seconds):
        self.elapsed += seconds
        self.wall += seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


def routine_response(run_id):
    return {"agents": [{"agentId": "bot-1", "automations": [{
        "automationId": "provider-automation-1",
        "recordJson": json.dumps({
            "id": "routine-1", "name": "Synthetic watcher", "isEnabled": True,
            "prompt": "synthetic-private-prompt", "filePath": "synthetic-private-path",
            "runs": [{"id": run_id, "requestId": "request-" + run_id,
                      "startedAt": 1791896400000, "status": "ok"}],
        }),
    }]}]}


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "snapshot.json"
        self.args = argparse.Namespace(
            output=self.output, auth_helper=Path("synthetic-helper"), team_id=None,
            no_prompt=False, ownership_interval=60, billing_interval=900, cycles=2,
        )
        self.clock = Clock()
        self.fingerprint = Mock(return_value=b"synthetic-session")
        self.reader = Mock(return_value="synthetic-private-token")
        self.provider = Mock()
        self.provider.claims = {"sub": "synthetic-account", "exp": 10_000}
        self.provider.dashboard.return_value = {"userId": 1, "authId": "synthetic-account"}
        self.provider.routines.return_value = routine_response("run-1")
        self.factory = Mock(return_value=self.provider)
        self.roster = Mock(return_value={"bot-1": "Synthetic bot"})
        self.billing = Mock(side_effect=lambda args, **kw: kw["before_write"]())
        self.destination = io.StringIO()

    def runner(self, **overrides):
        dependencies = dict(
            fingerprint=self.fingerprint, token_reader=self.reader,
            provider_factory=self.factory, roster_reader=self.roster,
            billing_collector=self.billing,
            monotonic=lambda: self.clock.elapsed, wall_time=lambda: self.clock.wall,
            sleep=self.clock.sleep, destination=self.destination,
        )
        dependencies.update(overrides)
        return watch.WatchRunner(self.args, **dependencies)

    def messages(self):
        return [json.loads(line) for line in self.destination.getvalue().splitlines()]

    def inventory(self):
        return json.loads(watch.inventory_path(self.output).read_text())

    def remembered(self):
        with sqlite3.connect(self.output.parent / "ledger.sqlite3") as db:
            return [(account, conversation, json.loads(data)) for account, conversation, data in db.execute(
                "SELECT account,conversation_id,data FROM ownership_runs ORDER BY conversation_id"
            )]

    def assert_cleared(self, runner):
        self.assertIsNone(runner.worker._token)
        self.assertIsNone(runner.worker._session)

    def test_rolling_runs_survive_and_only_whitelisted_metadata_is_saved(self):
        self.provider.routines.side_effect = [routine_response("run-1"), routine_response("run-2")]
        runner = self.runner()
        self.assertEqual(runner.run(), 0)
        inventory = self.inventory()
        self.assertEqual((inventory["currentRunLinks"], inventory["retainedRunLinks"]), (1, 2))
        self.assertEqual([row[1] for row in self.remembered()], ["sand-subagent-run-1", "sand-subagent-run-2"])
        self.assertEqual({row[0] for row in self.remembered()}, {inventory["accountKey"]})
        self.assertEqual(inventory["catalog"]["automations"][0]["routineId"], "routine-1")
        self.assertTrue(inventory["catalog"]["automations"][0]["isEnabled"])
        self.assertEqual(watch.inventory_path(self.output).stat().st_mode & 0o777, 0o600)
        for path in self.output.parent.iterdir():
            self.assertNotIn(b"synthetic-private", path.read_bytes())
        self.assertNotIn("Synthetic watcher", self.destination.getvalue())
        self.assertNotIn("synthetic-private", self.destination.getvalue())
        self.reader.assert_called_once_with(self.args.auth_helper, no_prompt=False)
        self.assertEqual(self.provider.dashboard.call_count, 3)  # Each capture validates GetMe.
        self.assertEqual(self.billing.call_count, 1)
        self.assert_cleared(runner)

    def test_later_routine_and_billing_failures_cannot_erase_observed_links(self):
        self.provider.routines.side_effect = [routine_response("run-1"), grok_spend.CollectionError("synthetic-private-error")]
        self.billing.side_effect = grok_spend.CollectionError("synthetic-private-token")
        runner = self.runner()
        self.assertEqual(runner.run(), 1)
        self.assertEqual(self.inventory()["retainedRunLinks"], 1)
        self.assertEqual(len(self.remembered()), 1)
        self.assertNotIn("synthetic-private", self.destination.getvalue())
        self.reader.assert_called_once()
        self.assert_cleared(runner)

    def test_later_ownership_success_does_not_hide_unresolved_billing_failure(self):
        self.billing.side_effect = grok_spend.CollectionError("offline billing")
        runner = self.runner()
        result = runner.run()
        self.assertEqual([row["status"] for row in self.messages()], [0, 1, 0])
        self.assertEqual(self.billing.call_count, 1)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.inventory()["retainedRunLinks"], 1)
        self.assertEqual(result, 1)
        self.assert_cleared(runner)

    def test_later_billing_success_clears_its_previous_failure(self):
        self.args.billing_interval = 60
        attempts = 0
        def billing(args, **kw):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise grok_spend.CollectionError("offline billing")
            kw["before_write"]()
            grok_spend.private_write(args.output, {"synthetic": True})
        self.billing.side_effect = billing
        runner = self.runner()
        self.assertEqual(runner.run(), 0)
        self.assertEqual([row["status"] for row in self.messages()], [0, 1, 0, 0])
        self.assertEqual(json.loads(self.output.read_text()), {"synthetic": True})
        self.assert_cleared(runner)

    def test_all_unavailable_inventory_is_incomplete_and_retains_known_links(self):
        self.provider.routines.side_effect = [routine_response("run-1"), {
            "agents": [{"agentId": "bot-1", "automations": [], "unavailable": True}],
        }]
        runner = self.runner()
        self.assertEqual(runner.run(), 1)
        inventory = self.inventory()
        self.assertEqual(inventory["catalog"]["completeness"], "unknown")
        self.assertEqual((inventory["currentRunLinks"], inventory["retainedRunLinks"]), (0, 1))
        self.assertEqual(len(self.remembered()), 1)
        status = self.messages()[-1]
        self.assertEqual(status["status"], 1)
        self.assertEqual(status["catalogCompleteness"], "unknown")
        self.assertEqual(status["unavailableBotCount"], 1)
        self.assertEqual(status["retainedRunLinks"], 1)
        self.assertIn("incomplete", status["detail"])
        self.assert_cleared(runner)

    def test_partial_inventory_saves_available_links_but_reports_incomplete(self):
        self.roster.return_value = {"bot-1": "Synthetic bot", "bot-2": "Second synthetic bot"}
        def group2(run):
            group = routine_response(run)["agents"][0]
            group["agentId"] = "bot-2"
            return group
        self.provider.routines.side_effect = [
            {"agents": [routine_response("run-1")["agents"][0], group2("run-2")]},
            {"agents": [{"agentId": "bot-1", "unavailable": True, "automations": []}, group2("run-3")]},
        ]
        runner = self.runner()
        self.assertEqual(runner.run(), 1)
        inventory = self.inventory()
        self.assertEqual((inventory["currentRunLinks"], inventory["retainedRunLinks"]), (1, 3))
        self.assertEqual(len(self.remembered()), 3)
        self.assertEqual(inventory["catalog"]["automations"][0]["botId"], "bot-2")
        status = self.messages()[-1]
        self.assertEqual(status["status"], 1)
        self.assertEqual(status["catalogCompleteness"], "unknown")
        self.assertEqual(status["unavailableBotCount"], 1)
        self.assertEqual(status["currentRunLinks"], 1)
        self.assertEqual(status["retainedRunLinks"], 3)
        self.reader.assert_called_once()
        self.assert_cleared(runner)

    def test_changed_local_session_stops_without_a_second_helper_read(self):
        def switch(seconds):
            self.clock.sleep(seconds)
            self.fingerprint.return_value = b"another-session"
        runner = self.runner(sleep=switch)
        self.assertEqual(runner.run(), 2)
        self.assertEqual(self.provider.routines.call_count, 1)
        self.assertEqual(self.inventory()["retainedRunLinks"], 1)
        self.reader.assert_called_once()
        self.assert_cleared(runner)

    def test_account_switch_during_keychain_read_never_reads_provider(self):
        self.fingerprint.side_effect = [b"before", b"after"]
        runner = self.runner()
        self.assertEqual(runner.run(), 2)
        self.factory.assert_not_called()
        self.reader.assert_called_once()
        self.assertFalse(watch.inventory_path(self.output).exists())
        self.assert_cleared(runner)

    def test_account_switch_during_routine_request_is_rejected_before_writes(self):
        def response(_):
            self.fingerprint.return_value = b"another-session"
            return routine_response("run-1")
        self.provider.routines.side_effect = response
        runner = self.runner()
        self.assertEqual(runner.run(), 2)
        self.assertFalse(watch.inventory_path(self.output).exists())
        self.assertFalse((self.output.parent / "ledger.sqlite3").exists())
        self.billing.assert_not_called()
        self.assert_cleared(runner)

    def test_account_switch_during_billing_is_rejected_by_write_guard(self):
        def billing(args, **kw):
            self.fingerprint.return_value = b"another-session"
            kw["before_write"]()
            args.output.write_text("should not be written")
        self.billing.side_effect = billing
        runner = self.runner()
        self.assertEqual(runner.run(), 2)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.inventory()["retainedRunLinks"], 1)
        self.assert_cleared(runner)

    def test_changed_getme_identity_stops_before_billing(self):
        self.provider.dashboard.side_effect = [
            {"userId": 1, "authId": "synthetic-account"},
            {"userId": 2, "authId": "other-account"},
        ]
        runner = self.runner()
        self.assertEqual(runner.run(), 2)
        self.billing.assert_not_called()
        self.reader.assert_called_once()
        self.assertEqual(self.inventory()["retainedRunLinks"], 1)
        self.assert_cleared(runner)

    def test_changed_team_scope_stops_before_any_new_write(self):
        runner = self.runner()
        def change_team(seconds):
            self.clock.sleep(seconds)
            runner.args.team_id = 42
        runner._sleep = change_team
        self.assertEqual(runner.run(), 2)
        self.provider.routines.assert_called_once()
        self.reader.assert_called_once()
        self.assert_cleared(runner)

    def test_expiry_while_waiting_stops_without_refreshing_credentials(self):
        self.provider.claims["exp"] = 1_030
        runner = self.runner()
        self.assertEqual(runner.run(), 2)
        self.reader.assert_called_once()
        self.provider.routines.assert_called_once()
        self.assert_cleared(runner)

    def test_401_from_billing_stops_and_keeps_prior_ownership(self):
        self.billing.side_effect = grok_spend.AuthenticationError("synthetic-private-token")
        runner = self.runner()
        self.assertEqual(runner.run(), 2)
        self.assertEqual(self.inventory()["retainedRunLinks"], 1)
        self.reader.assert_called_once()
        self.assertNotIn("synthetic-private", self.destination.getvalue())
        self.assert_cleared(runner)

    def test_initial_auth_failure_does_not_retry_or_expose_helper_detail(self):
        self.reader.side_effect = grok_spend.AuthenticationError("synthetic-private-detail")
        runner = self.runner()
        self.assertEqual(runner.run(), 2)
        self.reader.assert_called_once()
        self.factory.assert_not_called()
        self.assertNotIn("synthetic-private", self.destination.getvalue())
        self.assert_cleared(runner)

    def test_no_prompt_is_respected_on_the_only_helper_read(self):
        self.args.no_prompt = True
        self.assertEqual(self.runner().run(), 0)
        self.reader.assert_called_once_with(self.args.auth_helper, no_prompt=True)

    def test_offline_capture_recovers_using_the_original_cached_token(self):
        self.provider.routines.side_effect = [grok_spend.CollectionError("offline"), routine_response("run-2")]
        runner = self.runner()
        self.assertEqual(runner.run(), 0)
        self.assertEqual(self.inventory()["currentRunLinks"], 1)
        self.reader.assert_called_once()
        self.assertEqual([row["status"] for row in self.messages()], [1, 0, 0])
        self.assert_cleared(runner)

    def test_slow_billing_triggers_immediate_ownership_capture_before_wait(self):
        def slow_billing(args, **kw):
            self.clock.advance(95)
            kw["before_write"]()
        self.billing.side_effect = slow_billing
        self.args.cycles = 1
        self.provider.routines.side_effect = [routine_response("run-1"), routine_response("run-2")]
        self.assertEqual(self.runner().run(), 0)
        self.assertEqual([row["phase"] for row in self.messages()], ["ownership", "billing", "ownership"])
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.inventory()["retainedRunLinks"], 2)

    def test_billing_cadence_uses_monotonic_and_does_not_poll_every_minute(self):
        self.args.cycles = 17
        self.assertEqual(self.runner().run(), 0)
        self.assertEqual(self.billing.call_count, 2)
        self.assertEqual(self.provider.routines.call_count, 17)
        self.reader.assert_called_once()

    def test_cancellation_clears_token_and_does_not_print_collector_output(self):
        def billing(*args, **kw):
            print("synthetic-private-output")
            kw["before_write"]()
        self.billing.side_effect = billing
        runner = self.runner(sleep=Mock(side_effect=KeyboardInterrupt))
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(runner.run(), 130)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("synthetic-private", self.destination.getvalue())
        self.assert_cleared(runner)

    def test_failed_clock_drops_token_without_exception_text(self):
        runner = self.runner(monotonic=Mock(side_effect=RuntimeError("synthetic-private-clock")))
        self.assertEqual(runner.run(), 1)
        self.assertNotIn("synthetic-private", self.destination.getvalue())
        self.reader.assert_not_called()
        self.assert_cleared(runner)

    def test_empty_roster_is_unavailable_not_a_false_empty_inventory(self):
        self.roster.return_value = {}
        self.args.cycles = 1
        runner = self.runner()
        self.assertEqual(runner.run(), 1)
        self.assertFalse(watch.inventory_path(self.output).exists())
        self.provider.routines.assert_not_called()
        self.assert_cleared(runner)


if __name__ == "__main__":
    unittest.main()
