import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from collector.grok_spend import (
    CollectionError, Provider, build_snapshot, daily_rows, private_write,
    read_roster, reconciliation, store_snapshot, summarize_group, integrate_events, iso_ms,
)
from collector.attribution import normalize_events


def rows(*values):
    return {"dailySpend": [{"day": "1791849600000", "category": category,
                            "spendCents": amount, "totalTokens": tokens}
                           for category, amount, tokens in values]}


class DailyBillingTests(unittest.TestCase):
    def test_overlapping_views_are_not_added_and_products_are_scoped(self):
        grouped = {key: {"usageCents": rows(("sand" if key == "products" else "b1", 125, 1000)),
                         "includedCents": rows(("sand" if key == "products" else "b1", 100, 800)),
                         "onDemandCents": rows(("sand" if key == "products" else "b1", 25, 200))}
                   for key in ("products", "bots", "models", "sources")}
        grouped["products"]["usageCents"]["dailySpend"].extend(rows(("cursor", 99999, 500))["dailySpend"])
        snap = build_snapshot("a", "Account", {"start": "2026-10-01", "end": "2026-11-01"}, {}, grouped, {"b1": "Bot"}, [])
        self.assertEqual(snap["totals"]["usageCents"], 125)
        self.assertEqual(snap["totals"]["onDemandCents"], 25)
        self.assertEqual(snap["reconciliation"]["status"], "matched")

    def test_failed_metric_does_not_become_zero(self):
        result = summarize_group({"usageCents": rows(("a", 250, 2)), "includedCents": None, "onDemandCents": None})
        self.assertIsNone(result[0]["includedCents"])
        self.assertIsNone(result[0]["onDemandCents"])

    def test_explicit_missing_token_count_is_not_zero(self):
        result = summarize_group({"usageCents": rows(("a", 250, None))})
        self.assertIsNone(result[0]["tokens"])

    def test_omitted_token_count_is_unknown_and_explicit_zero_is_preserved(self):
        response = rows(("a", 25, 0))
        self.assertEqual(daily_rows(response)[0]["tokens"], 0)
        del response["dailySpend"][0]["totalTokens"]
        self.assertIsNone(daily_rows(response)[0]["tokens"])

    def test_live_shape_omitted_cents_never_claims_free_or_reconciled(self):
        response = {"dailySpend": [{"day": "1791849600000", "category": "sand", "totalTokens": "1200000"}]}
        grouped = {key: {"usageCents": response} for key in ("bots", "products", "models", "sources")}
        snap = build_snapshot("a", "A", {"start": "2026-10-01", "end": "2026-11-01"}, {}, grouped, {}, [])
        self.assertIsNone(snap["totals"]["usageCents"])
        self.assertIsNone(snap["bots"][0]["usageCents"])
        self.assertEqual(snap["bots"][0]["tokens"], 1200000)
        self.assertIsNone(snap["daily"][0]["usageCents"])
        self.assertEqual(snap["reconciliation"]["status"], "unavailable")

    def test_mixed_known_and_missing_amount_is_not_a_complete_sum(self):
        response = rows(("a", 20, 10))
        response["dailySpend"].append({"day": "1791849600000", "category": "a", "totalTokens": "30"})
        self.assertIsNone(summarize_group({"usageCents": response})[0]["usageCents"])

    def test_explicit_zero_preserved_without_claiming_cost_coverage(self):
        self.assertEqual(daily_rows(rows(("a", 0, 20)))[0]["cents"], 0)
        self.assertIsNone(reconciliation([{"id": "a", "usageCents": 0}], 0)["coveragePercent"])

    def test_successful_empty_metric_has_zero_for_known_category(self):
        result = summarize_group({"usageCents": rows(("a", 250, 2)), "onDemandCents": {}})
        self.assertEqual(result[0]["onDemandCents"], 0)

    def test_unknown_source_stays_unknown(self):
        self.assertEqual(summarize_group({"usageCents": rows(("new-flow", 10, 2))})[0]["id"], "new-flow")

    def test_invalid_cost_is_a_failed_view(self):
        for value in (None, True, "nan", "infinity", "bad"):
            with self.subTest(value=value), self.assertRaises(CollectionError):
                daily_rows(rows(("a", value, 2)))

    def test_unattributed_spend_reduces_coverage_without_hiding_money(self):
        report = reconciliation([{"id": "a", "usageCents": 75}, {"id": "unattributed", "usageCents": 25}], 100)
        self.assertEqual(report["status"], "matched")
        self.assertEqual(report["coveragePercent"], 75)
        self.assertEqual(report["attributedCents"], 100)

    def test_mismatch_visible_and_decimal_sum_exact(self):
        self.assertEqual(reconciliation([{"id": "a", "usageCents": .1}, {"id": "b", "usageCents": .2}], .3)["differenceCents"], 0)
        self.assertEqual(reconciliation([{"id": "a", "usageCents": 90}], 100)["status"], "mismatch")

    def test_missing_bot_view_cannot_claim_matched_zero(self):
        snap = build_snapshot("a", "A", {"start": "2026-10-01", "end": "2026-11-01"}, {}, {"products": {"usageCents": {}}}, {}, [])
        self.assertEqual(snap["reconciliation"]["status"], "unavailable")
        self.assertFalse(snap["capabilities"]["bots"])


class StorageAndTransportTests(unittest.TestCase):
    def test_event_cutoff_preserves_milliseconds_for_recent_window_filtering(self):
        self.assertEqual(iso_ms(1791921792957), "2026-10-13T20:03:12.957Z")

    def test_rosters_cannot_cross_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            for subject, name in (("one", "One"), ("two", "Two")):
                key = "sand.client.slice.account." + subject + ".roster.last-roster"
                filename = base64.b32encode(key.encode()).decode().lower().rstrip("=") + ".blob"
                Path(directory, filename).write_text(json.dumps({"value": {"rows": [{"id": "same", "name": name}]}}))
            self.assertEqual(read_roster(["one"], Path(directory)), {"same": "One"})
            self.assertEqual(read_roster(["missing"], Path(directory)), {})

    def test_display_name_absence_does_not_discard_bot_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            key = "sand.client.slice.account.one.roster.last-roster"
            filename = base64.b32encode(key.encode()).decode().lower().rstrip("=") + ".blob"
            Path(directory, filename).write_text(json.dumps({"value": {"rows": [{"id": "a", "title": "Title"}, {"id": "b"}]}}))
            self.assertEqual(read_roster(["one"], Path(directory)), {"a": "Title", "b": "b"})

    def test_atomic_private_snapshot_and_account_scoped_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "snapshot.json")
            base = {"period": {"start": "2026-10-01"}, "generatedAt": "2026-10-13T00:00:00Z", "events": [{"eventId": "same"}]}
            for account in ("a", "b"):
                store_snapshot(base | {"account": {"key": account}}, output)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual((output.parent / "ledger.sqlite3").stat().st_mode & 0o777, 0o600)
            import sqlite3
            with sqlite3.connect(output.parent / "ledger.sqlite3") as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)

    def test_failed_write_preserves_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "snapshot.json")
            private_write(output, {"old": True})
            with self.assertRaises(ValueError):
                private_write(output, {"invalid": float("nan")})
            self.assertEqual(json.loads(output.read_text()), {"old": True})

    def test_corrected_anonymous_event_replaces_complete_period(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "snapshot.json")
            base = {"account": {"key": "a"}, "period": {"start": "2026-10-01"}, "generatedAt": "2026-10-13T00:00:00Z", "capabilities": {"events": True}}
            for value in (None, 20):
                events = normalize_events([{"timestamp": "1791849600000", "conversationId": "b", "chargedCents": value}], "a")
                store_snapshot(base | {"events": events}, output)
            with sqlite3.connect(output.parent / "ledger.sqlite3") as db:
                rows = db.execute("SELECT data FROM events").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(json.loads(rows[0][0])["chargedCents"], 20)
            store_snapshot(base | {"capabilities": {"events": False}, "events": []}, output)
            with sqlite3.connect(output.parent / "ledger.sqlite3") as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def provider(self):
        payload = base64.urlsafe_b64encode(json.dumps({"sub": "synthetic"}).encode()).decode().rstrip("=")
        return Provider("unused." + payload + ".unused")

    def test_write_methods_are_rejected_before_network(self):
        with self.assertRaises(CollectionError):
            self.provider().dashboard("SetHardLimit")

    def test_rest_schema_change_is_not_empty_success(self):
        provider = self.provider()
        with patch.object(provider, "_request", return_value={"unexpected": []}):
            with self.assertRaises(CollectionError):
                provider.events(1, 2)

    def test_pagination_cannot_silently_truncate(self):
        provider = self.provider()
        pages = [{"usageEventsDisplay": [{"id": "a"}], "totalUsageEventsCount": 2}, {"usageEventsDisplay": [], "totalUsageEventsCount": 2}]
        with patch.object(provider, "_request", side_effect=pages), self.assertRaises(CollectionError):
            provider.events(1, 2)

    def test_conflicting_native_id_cannot_be_added_twice(self):
        provider = self.provider()
        page = {"usageEventsDisplay": [{"id": "same", "chargedCents": 10}, {"id": "same", "chargedCents": 20}], "totalUsageEventsCount": 2}
        with patch.object(provider, "_request", return_value=page), self.assertRaises(CollectionError):
            provider.events(1, 2)

    def test_unknown_total_count_cannot_claim_complete_page(self):
        provider = self.provider()
        for total in (None, -1, 1.5, "bad"):
            with self.subTest(total=total), patch.object(provider, "_request", return_value={"usageEventsDisplay": [], "totalUsageEventsCount": total}), self.assertRaises(CollectionError):
                provider.events(1, 2)


class EventIntegrationTests(unittest.TestCase):
    def test_recorded_value_does_not_become_cash_or_overlapping_daily_cost(self):
        snap = build_snapshot("a", "A", {"start": "2026-10-01", "end": "2026-11-01"}, {},
                              {"bots": {"usageCents": rows(("b1", 100, 500))}, "models": {}, "products": {}}, {"b1": "Bot"}, [])
        raw = [{"conversationId": "b1", "model": "grok-bot-default", "chargedCents": 25,
                "kind": "INCLUDED_IN_ULTRA", "tokenUsage": {"totalCents": 25, "inputTokens": 10}},
               {"conversationId": "sand-subagent-unknown", "model": "grok-bot-default", "chargedCents": 75,
                "tokenUsage": {"totalCents": 75}},
               {"conversationId": "other-product", "model": "grok-bot-default", "chargedCents": 9999}]
        integrate_events(snap, normalize_events(raw, "a"), {"b1": "Bot"}, {}, "2026-10-13T00:00:00Z", True)
        self.assertEqual(snap["bots"][0]["usageCents"], 100)
        self.assertEqual(snap["bots"][0]["eventValueCents"], 25)
        self.assertIsNone(snap["bots"][0]["onDemandCents"])
        self.assertEqual(snap["bots"][-1]["eventValueCents"], 75)
        self.assertEqual(snap["models"][0]["eventValueCents"], 100)
        self.assertEqual(snap["eventCoverage"]["unknownCount"], 1)
        self.assertEqual(snap["eventCoverage"]["unassignedCount"], 1)
        self.assertEqual(snap["eventTotals"]["chargedCents"], 100)
        self.assertIsNone(snap["totals"]["usageCents"])

    def test_mixed_account_events_fail_closed(self):
        snapshot = {"account": {"key": "a"}}
        with self.assertRaises(CollectionError):
            integrate_events(snapshot, normalize_events([{}], "b"), {}, {}, "now", False)


if __name__ == "__main__":
    unittest.main()
