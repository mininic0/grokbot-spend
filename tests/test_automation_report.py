import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collector.automation_report import ReportError, build_report, render_text, with_inventory
from collector.grok_spend import main


def event(identity, moment="2026-10-13T10:00:00Z", cents=100, *, bot="b1", routine="r1", conversation="sand-subagent-run1", method="routine-run-id"):
    return {"eventId": identity, "accountKey": "synthetic-account", "timestampISO": moment,
            "chargedCents": cents, "modelCostCents": 999999, "conversationId": conversation,
            "attribution": {"botId": bot, "routineId": routine, "routineName": "Synthetic automation", "method": method}}


def snapshot(*events):
    return {"account": {"key": "synthetic-account"}, "generatedAt": "2026-10-13T12:02:00Z", "mode": "demo",
            "period": {"start": "2026-10-01T12:00:00Z", "end": "2026-11-01T12:00:00Z"},
            "capabilities": {"events": True}, "eventCoverage": {"through": "2026-10-13T12:00:00Z", "unknownCount": 3, "excludedCount": 2},
            "bots": [{"id": "b1", "name": "Synthetic bot"}, {"id": "b2", "name": "Second bot"}], "events": list(events)}


class AutomationReportTests(unittest.TestCase):
    def test_newer_inventory_changes_state_without_changing_billing(self):
        original = snapshot(event("one"))
        sidecar = {"accountKey": "synthetic-account", "observedAt": "2026-10-13T12:05:00Z", "catalog": {
            "completeness": "complete", "automations": [{"botId": "b1", "routineId": "r1", "isEnabled": False}],
        }}
        combined = with_inventory(original, sidecar)
        report = build_report(combined, "24h")
        self.assertEqual(report["automations"][0]["state"], "disabled")
        self.assertEqual(report["automations"][0]["stateObservedAt"], "2026-10-13T12:05:00.000Z")
        self.assertEqual(report["totals"]["recordedValueCents"], 100)
        self.assertIs(combined["events"], original["events"])
        self.assertNotIn("automationCatalog", original)

    def test_wrong_account_stale_or_invalid_inventory_is_rejected(self):
        good = {"accountKey": "synthetic-account", "observedAt": "2026-10-13T12:05:00Z", "catalog": {"automations": []}}
        for change in ({"accountKey": "another-account"}, {"observedAt": "2026-10-12T00:00:00Z"}, {"observedAt": "invalid"}, {"catalog": {}}):
            with self.subTest(change=change), self.assertRaises(ReportError):
                with_inventory(snapshot(event("one")), good | change)

    def test_window_bounds_are_inclusive_and_preserve_milliseconds(self):
        report = build_report(snapshot(
            event("before", "2026-10-12T11:59:59.999Z", 1000),
            event("start", "2026-10-12T12:00:00Z", 10),
            event("end", "2026-10-13T12:00:00Z", 20),
            event("after", "2026-10-13T12:00:00.001Z", 1000)), "24h")
        self.assertEqual(report["totals"]["recordedValueCents"], 30)
        self.assertEqual(report["outsideWindowEventCount"], 2)
        self.assertEqual(report["window"]["start"], "2026-10-12T12:00:00.000Z")

    def test_recent_window_clamps_to_cycle_start(self):
        sample = snapshot(event("a"))
        sample["period"]["start"] = "2026-10-12T00:00:00Z"
        self.assertEqual(build_report(sample, "7d")["window"]["start"], "2026-10-12T00:00:00.000Z")
        self.assertEqual(build_report(snapshot(event("a")), "7d")["window"]["start"], "2026-10-06T12:00:00.000Z")

    def test_cycle_end_caps_window_and_excludes_next_period(self):
        sample = snapshot(event("a", "2026-10-14T00:00:00Z"))
        sample["period"]["end"] = "2026-10-13T12:00:00Z"
        sample["eventCoverage"]["through"] = "2026-10-14T12:00:00Z"
        report = build_report(sample)
        self.assertFalse(report["window"]["endsAtSavedEventCutoff"])
        self.assertEqual(report["totals"]["eventCount"], 0)

    def test_distinct_matched_runs_and_recorded_cost_not_model_cost(self):
        report = build_report(snapshot(event("a", cents="0.1"), event("b", cents="0.2"),
                                       event("c", cents=30, conversation="sand-subagent-run2")))
        row = report["automations"][0]
        self.assertEqual(row["matchedRuns"], 2)
        self.assertEqual(row["recordedValueCents"], 30.3)
        self.assertEqual(row["averageRecordedValueCentsPerMatchedRun"], 15.15)

    def test_missing_values_remain_null_with_reported_subtotals(self):
        report = build_report(snapshot(event("a", cents=200), event("b", cents=None)))
        for item in (report["totals"], report["coverage"]["routineLinked"], report["automations"][0]):
            self.assertIsNone(item["recordedValueCents"])
            self.assertEqual(item["reportedValueCents"], 200)
            self.assertEqual(item["missingValueEventCount"], 1)
        self.assertIsNone(report["automations"][0]["averageRecordedValueCentsPerMatchedRun"])
        self.assertIsNone(report["coverage"]["routineLinked"]["valuePercent"])
        self.assertIn("$2.00 reported; 1 events missing value", render_text(report))

    def test_all_missing_invalid_and_zero_have_distinct_meanings(self):
        for invalid in (None, True, "NaN", "infinity", "bad"):
            with self.subTest(value=invalid):
                report = build_report(snapshot(event("a", cents=invalid)))
                self.assertIsNone(report["totals"]["reportedValueCents"])
                self.assertIsNone(report["totals"]["recordedValueCents"])
        self.assertEqual(build_report(snapshot(event("a", cents=0)))["totals"]["recordedValueCents"], 0)

    def test_coverage_is_disjoint_and_never_guesses_routine_from_name(self):
        report = build_report(snapshot(
            event("routine", cents=10),
            event("bot", cents=20, conversation="b1", routine=None, method="bot-conversation-id"),
            event("child", cents=30, bot=None, routine=None, method=None),
            event("unknown", cents=40, conversation="unknown", bot=None, routine=None, method=None)))
        self.assertEqual(report["totals"]["recordedValueCents"], 100)
        self.assertEqual([item["recordedValueCents"] for item in report["coverage"].values()], [10, 20, 30, 40])
        self.assertEqual(report["coverage"]["routineLinked"]["valuePercent"], 10)
        self.assertEqual(len(report["automations"]), 1)

    def test_unproven_parent_and_same_routine_id_different_owners(self):
        report = build_report(snapshot(event("a"), event("b", bot="b2", cents=200), event("c", bot="unknown", cents=900)))
        self.assertEqual([row["botId"] for row in report["automations"]], ["b2", "b1"])
        self.assertEqual(report["coverage"]["unassignedChild"]["recordedValueCents"], 900)
        wrong_direct = event("d", conversation="b2", bot="b1", method="bot-conversation-id")
        self.assertEqual(build_report(snapshot(wrong_direct))["coverage"]["unknown"]["eventCount"], 1)

    def test_missing_timestamp_is_visible_and_not_window_attributed(self):
        report = build_report(snapshot(event("a"), event("b", None, cents=200)), "24h")
        self.assertEqual(report["totals"]["recordedValueCents"], 100)
        self.assertEqual(report["undatedEvents"]["recordedValueCents"], 200)
        self.assertIsNone(report["coverage"]["routineLinked"]["valuePercent"])
        self.assertIn("Undated events excluded", render_text(report))

    def test_cross_account_duplicate_and_conflicting_identity_fail_closed(self):
        other = event("b")
        other["accountKey"] = "different-account"
        conflict = event("c")
        conflict["issues"] = ["native-id-conflict"]
        for events in ((event("a"), other), (event("a"), event("a")), (conflict,)):
            with self.subTest(events=len(events)), self.assertRaises(ReportError):
                build_report(snapshot(*events))

    def test_missing_event_capability_or_cutoff_never_reports_zero(self):
        for mutation in ("capability", "cutoff", "period"):
            sample = snapshot()
            if mutation == "capability": sample["capabilities"]["events"] = False
            elif mutation == "cutoff": sample["eventCoverage"].pop("through")
            else: sample["period"]["end"] = sample["period"]["start"]
            with self.subTest(mutation=mutation), self.assertRaises(ReportError):
                build_report(sample)
        self.assertEqual(build_report(snapshot())["totals"]["recordedValueCents"], 0)

    def test_text_sanitizes_names_and_json_retains_only_report_metadata(self):
        value = event("a")
        value["attribution"]["routineName"] = "A\x1b[2J\nB"
        value["secret"] = "synthetic-do-not-export"
        report = build_report(snapshot(value))
        self.assertNotIn("\x1b", render_text(report))
        self.assertNotIn("synthetic-do-not-export", json.dumps(report))

    def test_catalog_state_joins_exact_owner_and_absence_stays_unknown(self):
        sample = snapshot(event("a"), event("b", bot="b2"), event("c", routine="r2"))
        sample["automationCatalog"] = {"completeness": "unknown", "automations": [
            {"botId": "b1", "routineId": "r1", "isEnabled": False, "name": "Renamed automation"},
            {"botId": "b2", "routineId": "r1", "providerAutomationId": "different-server-id", "isEnabled": True},
            {"botId": "b1", "routineId": None, "providerAutomationId": "r2", "isEnabled": False}]}
        report = build_report(sample)
        rows = {(row["botId"], row["routineId"]): row for row in report["automations"]}
        self.assertIs(rows[("b1", "r1")]["isEnabled"], False)
        self.assertEqual(rows[("b1", "r1")]["name"], "Renamed automation")
        self.assertEqual(rows[("b2", "r1")]["stateObservedAt"], sample["generatedAt"])
        self.assertIsNone(rows[("b1", "r2")]["isEnabled"])
        self.assertIsNone(rows[("b1", "r2")]["stateObservedAt"])
        self.assertIn("[disabled]", render_text(report))
        sample["automationCatalog"]["observedAt"] = "2026-10-13T12:03:00Z"
        self.assertEqual(build_report(sample)["automations"][0]["stateObservedAt"], "2026-10-13T12:03:00Z")

    def test_conflicting_or_non_boolean_catalog_state_is_unknown(self):
        for states in ([True, False], ["false"], [0], [None]):
            sample = snapshot(event("a"))
            sample["automationCatalog"] = {"automations": [{"botId": "b1", "routineId": "r1", "isEnabled": state} for state in states]}
            with self.subTest(states=states):
                self.assertEqual(build_report(sample)["automations"][0]["state"], "unknown")

    def test_enabled_filter_keeps_full_coverage_and_discloses_unknown_exclusions(self):
        sample = snapshot(event("a"), event("b", routine="r2"), event("c", routine="r3"))
        sample["automationCatalog"] = {"automations": [{"botId": "b1", "routineId": "r1", "isEnabled": True},
                                                       {"botId": "b1", "routineId": "r2", "isEnabled": False}]}
        report = build_report(sample, enabled_only=True)
        self.assertEqual(len(report["automations"]), 1)
        self.assertEqual(report["totals"]["recordedValueCents"], 300)
        self.assertEqual(report["rankingFilter"]["excludedUnknownStateCount"], 1)
        self.assertIn("excludes 1 disabled and 1 unknown-state", render_text(report))
        unknown = build_report(snapshot(event("a")), enabled_only=True)
        self.assertIn("No confirmed enabled automation", render_text(unknown))
        self.assertNotIn("No exact automation-linked events", render_text(unknown))

    def test_cli_reports_offline_and_writes_private_json_and_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(json.dumps(snapshot(event("a"))))
            for output_format in ("text", "json"):
                output = Path(directory) / ("report." + output_format)
                output.write_text("old")
                output.chmod(0o644)
                with patch("sys.argv", ["grok_spend", "automations", "--snapshot", str(path), "--window", "24h", "--format", output_format, "--output", str(output)]), patch("collector.grok_spend.Provider", side_effect=AssertionError("Network forbidden")), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(), 0)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                if output_format == "json": self.assertEqual(json.loads(output.read_text())["totals"]["recordedValueCents"], 100)
                else: self.assertIn("Synthetic automation", output.read_text())


if __name__ == "__main__":
    unittest.main()
