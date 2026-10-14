import json
import math
import unittest

from collector.attribution import attribute_events, build_routine_index, merge_routine_indexes, normalize_events, routine_rows, scope_grok_events


def raw_event(**changes):
    event = {
        "timestamp": "1791849600000", "model": "grok-bot-default", "conversationId": "bot-a",
        "tokenUsage": {"inputTokens": 10, "outputTokens": 3, "cacheReadTokens": 20, "cacheWriteTokens": 2, "totalCents": 1.25},
        "chargedCents": 1.5,
    }
    event.update(changes)
    return event


def routines(bot="bot-a", routine="routine-a", run="run-a", name="Morning check"):
    return {"agentId": bot, "automations": [{"recordJson": json.dumps({"id": routine, "name": name, "runs": [{"id": run}]})}]}


class NormalizeTests(unittest.TestCase):
    def test_separate_costs_tokens_and_metadata_allowlist(self):
        event = normalize_events([raw_event(content="private transcript", authorization="secret", requestId="request-only")], "account")[0]
        self.assertEqual(event["modelCostCents"], 1.25)
        self.assertEqual(event["chargedCents"], 1.5)
        self.assertEqual(event["totalTokens"], 35)
        self.assertEqual(event["timestampISO"], "2026-10-13T00:00:00.000Z")
        self.assertNotIn("private", json.dumps(event))
        self.assertNotIn("secret", json.dumps(event))
        self.assertEqual(event["identitySource"], "metadata-occurrence")

    def test_missing_costs_do_not_become_zero_or_model_cost(self):
        events = normalize_events([raw_event(chargedCents=None), {"usageBasedCosts": "$12.34", "kind": "Included"}], "account")
        self.assertIsNone(events[0]["chargedCents"])
        self.assertIsNone(events[1]["modelCostCents"])
        self.assertIsNone(events[1]["chargedCents"])
        self.assertIsNone(events[1]["totalTokens"])

    def test_invalid_values_are_unavailable_json_safe(self):
        events = normalize_events([raw_event(timestamp="bad", chargedCents=math.inf, tokenUsage={"inputTokens": True, "outputTokens": -1, "cacheReadTokens": 1.5, "cacheWriteTokens": "NaN", "totalCents": "nan"}), None], "account")
        self.assertTrue(events[0]["issues"])
        self.assertTrue(all(events[0][field] is None for field in ("timestamp", "inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "modelCostCents", "chargedCents")))
        self.assertIn("malformed-event", events[1]["issues"])
        json.dumps(events, allow_nan=False)

    def test_numeric_strings_and_zero_are_preserved(self):
        event = normalize_events([raw_event(chargedCents="0", tokenUsage={"inputTokens": "2", "outputTokens": 0, "cacheReadTokens": 0, "cacheWriteTokens": 0, "totalCents": "0.025"})], "account")[0]
        self.assertEqual(event["chargedCents"], 0)
        self.assertEqual(event["modelCostCents"], 0.025)
        self.assertEqual(event["totalTokens"], 2)

    def test_anonymous_identical_requests_survive_and_replays_are_stable(self):
        first = normalize_events([raw_event(), raw_event(), raw_event(model="different")], "account")
        replay = normalize_events([raw_event(model="different"), raw_event(), raw_event()], "account")
        self.assertEqual(len({event["eventId"] for event in first}), 3)
        self.assertEqual({event["eventId"] for event in first}, {event["eventId"] for event in replay})
        other = normalize_events([raw_event()], "another-account")
        self.assertNotEqual(first[0]["eventId"], other[0]["eventId"])

    def test_native_replay_deduplicates_but_conflicting_payloads_survive(self):
        same = raw_event(eventId="native")
        self.assertEqual(len(normalize_events([same, same], "account")), 1)
        conflict = raw_event(eventId="native", chargedCents=9)
        forward = normalize_events([same, conflict, same], "account")
        backward = normalize_events([conflict, same], "account")
        self.assertEqual(len(forward), 2)
        self.assertEqual({e["eventId"] for e in forward}, {e["eventId"] for e in backward})
        self.assertTrue(all("native-id-conflict" in e["issues"] for e in forward))

    def test_iso_timestamp_requires_timezone(self):
        events = normalize_events([raw_event(timestamp="2026-10-13T02:00:00+02:00"), raw_event(timestamp="2026-10-13T00:00:00")], "account")
        self.assertEqual(events[0]["timestamp"], 1791849600000)
        self.assertIsNone(events[1]["timestamp"])

    def test_account_scope_required(self):
        with self.assertRaises(ValueError):
            normalize_events([], "")


class RoutineIndexTests(unittest.TestCase):
    def test_server_id_does_not_replace_missing_local_record_id(self):
        response = {"agents": [{"agentId": "bot-a", "automations": [{"automationId": "routine-a", "recordJson": json.dumps({"name": "Routine", "runs": [{"id": "run-a"}]})}]}]}
        self.assertIsNone(build_routine_index(response)["sand-subagent-run-a"]["routineId"])

    def test_distinct_server_and_local_ids_use_the_native_local_identity(self):
        group = routines()
        group["automations"][0]["automationId"] = "server-routine-uuid"
        current = build_routine_index({"agents": [group]})
        entry = current["sand-subagent-run-a"]
        self.assertNotIn("ambiguous", entry)
        self.assertEqual(entry["routineId"], "routine-a")
        previous = build_routine_index({"agents": [routines()]})
        merged = merge_routine_indexes(previous, current)
        self.assertEqual(merged, previous)
        events = normalize_events([raw_event(conversationId="sand-subagent-run-a")], "account-a")
        result = attribute_events(events, {"bot-a": "Bot A"}, merged)
        self.assertEqual(len(result["unassigned"]), 0)

    def test_exact_run_join_and_no_arbitrary_payload(self):
        index = build_routine_index({"agents": [routines()]})
        self.assertEqual(index, {"sand-subagent-run-a": {"botId": "bot-a", "routineId": "routine-a", "name": "Morning check"}})

    def test_two_bots_or_routines_claiming_run_is_ambiguous(self):
        for second in (routines(bot="bot-b"), routines(routine="routine-b")):
            with self.subTest(second=second):
                index = build_routine_index({"agents": [routines(), second]})
                self.assertTrue(index["sand-subagent-run-a"]["ambiguous"])

    def test_duplicate_claims_are_not_ambiguous(self):
        index = build_routine_index({"agents": [routines(), routines()]})
        self.assertNotIn("ambiguous", index["sand-subagent-run-a"])

    def test_malformed_response_and_records(self):
        self.assertEqual(build_routine_index({"agents": "invalid"}), {})
        self.assertEqual(build_routine_index({"agents": [None, {"agentId": "a", "automations": [None, {"recordJson": "broken"}, {"recordJson": "[]"}, {"recordJson": "{\"runs\": null}"}]}]}), {})


class RoutineHistoryTests(unittest.TestCase):
    def test_rolling_twenty_run_window_keeps_older_exact_owners(self):
        owner = {"botId": "bot-a", "routineId": "routine-a", "name": "Routine"}
        old = {f"sand-subagent-run-{i}": dict(owner) for i in range(20)}
        recent = {f"sand-subagent-run-{i}": dict(owner) for i in range(1, 21)}
        merged = merge_routine_indexes(old, recent)
        self.assertEqual(len(merged), 21)
        event = normalize_events([raw_event(conversationId="sand-subagent-run-0")], "account")
        result = attribute_events(event, {"bot-a": "Bot A"}, merged)
        self.assertEqual(result["events"][0]["attribution"]["routineId"], "routine-a")
        self.assertEqual(merge_routine_indexes(merged, {}), merged)

    def test_competing_bot_or_routine_stays_ambiguous_after_later_refresh(self):
        old = build_routine_index({"agents": [routines()]})
        for change in (routines(bot="bot-b"), routines(routine="routine-b"), routines(routine=None)):
            current = build_routine_index({"agents": [change]})
            merged = merge_routine_indexes(old, current)
            self.assertTrue(merged["sand-subagent-run-a"]["ambiguous"])
            self.assertEqual(len(merged["sand-subagent-run-a"]["candidates"]), 2)
            self.assertEqual(merge_routine_indexes(merged, old), merged)
            event = normalize_events([raw_event(conversationId="sand-subagent-run-a")], "account")
            result = attribute_events(event, {"bot-a": "A", "bot-b": "B"}, merged)
            self.assertEqual(result["unassigned"][0]["attribution"]["unassignedReason"], "ambiguous-routine-parent")

    def test_existing_ambiguity_is_not_resolved_by_an_incomplete_observation(self):
        owner = {"botId": "bot-a", "routineId": "routine-a", "name": "Routine"}
        old = {"sand-subagent-run-a": {"ambiguous": True, "candidates": [owner]}}
        merged = merge_routine_indexes(old, {"sand-subagent-run-a": owner})
        self.assertTrue(merged["sand-subagent-run-a"]["ambiguous"])
        self.assertEqual(len(merged["sand-subagent-run-a"]["candidates"]), 1)

    def test_name_can_update_without_changing_identity_or_retaining_payload(self):
        old = build_routine_index({"agents": [routines()]})
        current = build_routine_index({"agents": [routines(name="Renamed")]})
        current["sand-subagent-run-a"]["prompt"] = "must not persist"
        before = json.dumps([old, current])
        merged = merge_routine_indexes(old, current)
        self.assertEqual(merged["sand-subagent-run-a"], {"botId": "bot-a", "routineId": "routine-a", "name": "Renamed"})
        self.assertEqual(json.dumps([old, current]), before)
        self.assertNotIn("must not persist", json.dumps(merged))
        self.assertEqual(merge_routine_indexes(merged, build_routine_index({"agents": [routines(name=None)]})), merged)

    def test_malformed_history_fails_without_silently_dropping_evidence(self):
        for invalid in (None, [], {"wrong-prefix": {}}, {"sand-subagent-run-a": None}, {"sand-subagent-run-a": {}}, {"sand-subagent-run-a": {"ambiguous": True}}, {"sand-subagent-run-a": {"ambiguous": True, "candidates": [None]}}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                merge_routine_indexes(invalid, {})


class AttributionTests(unittest.TestCase):
    def test_concurrent_events_join_by_id_not_timestamp_or_model(self):
        events = normalize_events([raw_event(), raw_event(conversationId="bot-b"), raw_event(conversationId="sand-subagent-run-a"), raw_event(conversationId="unknown", cloudAgentId="bot-a", automationId="routine-a")], "account")
        result = attribute_events(events, {"bot-a": "A", "bot-b": "B"}, build_routine_index({"agents": [routines()]}))
        bots = {bot["botId"]: bot for bot in result["bots"]}
        self.assertEqual(bots["bot-a"]["eventCount"], 2)
        self.assertEqual(bots["bot-b"]["eventCount"], 1)
        self.assertEqual(result["routines"][0]["eventCount"], 1)
        self.assertEqual(len(result["unassigned"]), 1)
        self.assertEqual(result["totals"]["eventCount"], 4)
        self.assertEqual(result["totals"]["chargedCents"], 6)
        self.assertEqual(result["totals"]["modelCostCents"], 5)

    def test_unknown_subagents_and_ambiguous_parents_stay_unassigned(self):
        index = build_routine_index({"agents": [routines(), routines(bot="bot-b")]})
        events = normalize_events([raw_event(conversationId="sand-subagent-run-a"), raw_event(conversationId="sand-subagent-unknown")], "account")
        result = attribute_events(events, {"bot-a": "A", "bot-b": "B"}, index)
        self.assertEqual(result["bots"], [])
        self.assertEqual([event["attribution"]["unassignedReason"] for event in result["unassigned"]], ["ambiguous-routine-parent", "unknown-subagent-parent"])

    def test_parent_outside_roster_and_mixed_accounts_fail_closed(self):
        event = normalize_events([raw_event(conversationId="sand-subagent-run-a")], "account")
        result = attribute_events(event, {}, build_routine_index({"agents": [routines()]}))
        self.assertEqual(result["unassigned"][0]["attribution"]["unassignedReason"], "routine-parent-not-in-account-roster")
        mixed = normalize_events([raw_event()], "a") + normalize_events([raw_event()], "b")
        result = attribute_events(mixed, {"bot-a": "A"}, {})
        self.assertEqual(len(result["unassigned"]), 2)
        self.assertEqual(result["bots"], [])

    def test_partial_totals_report_missing_coverage(self):
        events = normalize_events([raw_event(), raw_event(chargedCents=None, tokenUsage={})], "account")
        result = attribute_events(events, {"bot-a": "A"}, {})
        self.assertEqual(result["totals"]["chargedCents"], 1.5)
        self.assertEqual(result["totals"]["chargedCentsMissingCount"], 1)
        self.assertEqual(result["totals"]["totalTokensMissingCount"], 1)
        unavailable = attribute_events(normalize_events([{}], "account"), {}, {})
        self.assertIsNone(unavailable["totals"]["chargedCents"])
        self.assertIsNone(attribute_events([], {}, {})["totals"]["chargedCents"])

    def test_does_not_modify_input_and_preserves_unknown_requests(self):
        events = normalize_events([raw_event(conversationId=None), raw_event()], "account")
        before = json.dumps(events)
        result = attribute_events(events, {"bot-a": "A"}, {})
        self.assertEqual(before, json.dumps(events))
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(result["unassigned"][0]["attribution"]["unassignedReason"], "missing-conversation-id")


class GrokScopeTests(unittest.TestCase):
    def test_accepts_only_exact_metadata_not_model_names(self):
        raw = [
            raw_event(conversationId=None, clientType="grok-bot"),
            raw_event(conversationId=None, product="sand"),
            raw_event(conversationId=None, products=["sand"]),
            raw_event(),
            raw_event(conversationId="sand-subagent-unknown"),
            raw_event(conversationId="other-run"),
            raw_event(conversationId=None, model="grok-bot-default"),
            raw_event(conversationId=None, model="sand-4"),
        ]
        index = {"other-run": {"botId": "bot-a", "routineId": "routine-a", "name": "Routine"}}
        result = scope_grok_events(normalize_events(raw, "account"), {"bot-a": "A"}, index)
        self.assertEqual((result["includedCount"], result["excludedCount"], result["unknownCount"]), (6, 0, 2))
        self.assertEqual(sum(sum(group.values()) for group in result["reasons"].values()), 8)

    def test_explicit_other_product_wins_and_values_are_case_sensitive(self):
        raw = [
            raw_event(product="cursor"),
            raw_event(clientType="cursor", product="sand"),
            raw_event(products=["sand", "cursor"]),
            raw_event(conversationId=None, clientType="GROK-BOT"),
            raw_event(conversationId=None, product="SAND"),
        ]
        result = scope_grok_events(normalize_events(raw, "account"), {"bot-a": "A"}, {})
        self.assertEqual(result["events"], [])
        self.assertEqual(result["excludedCount"], 5)
        self.assertEqual(result["reasons"]["excluded"]["conflicting-product-evidence"], 3)

    def test_scoped_unknown_and_ambiguous_parents_are_preserved(self):
        index = build_routine_index({"agents": [routines(), routines(bot="bot-b")]})
        scoped = scope_grok_events(normalize_events([
            raw_event(conversationId="sand-subagent-run-a"),
            raw_event(conversationId="sand-subagent-unknown"),
            raw_event(conversationId=None, clientType="grok-bot"),
        ], "account"), {"bot-a": "A", "bot-b": "B"}, index)
        attributed = attribute_events(scoped["events"], {"bot-a": "A", "bot-b": "B"}, index)
        self.assertEqual(len(attributed["unassigned"]), 3)

    def test_mixed_accounts_and_empty_subagent_suffix_are_unknown(self):
        events = normalize_events([raw_event(clientType="grok-bot")], "a") + normalize_events([raw_event(clientType="grok-bot")], "b")
        result = scope_grok_events(events, {"bot-a": "A"}, {})
        self.assertEqual(result["unknownCount"], 2)
        self.assertEqual(result["reasons"]["unknown"], {"mixed-or-missing-account": 2})
        self.assertEqual(scope_grok_events(normalize_events([raw_event(conversationId="sand-subagent-")], "a"), {}, {})["unknownCount"], 1)

    def test_products_normalize_without_arbitrary_objects(self):
        events = normalize_events([raw_event(products=["sand", "sand", {"secret": "must not persist"}]), raw_event(products="sand")], "account")
        self.assertEqual(events[0]["products"], ["sand"])
        self.assertIn("malformed-products", events[0]["issues"])
        self.assertEqual(events[1]["products"], [])
        self.assertNotIn("secret", json.dumps(events))


class RoutineRowsTests(unittest.TestCase):
    def attributed(self, raw):
        index = build_routine_index({"agents": [routines(), routines(run="run-b")]})
        return attribute_events(normalize_events(raw, "account"), {"bot-a": "Bot A"}, index)

    def test_runs_count_unique_conversations_and_costs_do_not_mix(self):
        data = self.attributed([
            raw_event(conversationId="sand-subagent-run-a"),
            raw_event(conversationId="sand-subagent-run-a"),
            raw_event(conversationId="sand-subagent-run-b"),
        ])
        row = routine_rows(data)[0]
        self.assertEqual(row["runs"], 2)
        self.assertEqual(row["usageCents"], 3.75)
        self.assertEqual(row["modelCostCents"], 3.75)
        self.assertEqual(row["chargedCents"], 4.5)
        self.assertIsNone(row["includedCents"])
        self.assertIsNone(row["onDemandCents"])
        self.assertEqual(row["tokens"], 105)
        self.assertEqual(row["costBasis"], "model-cost")
        self.assertIn("Provider model cost", row["detail"])
        self.assertEqual(row["botId"], "bot-a")
        self.assertEqual(row["routineId"], "routine-a")
        self.assertEqual(row["coverage"]["modelCostCents"]["reportedEvents"], 3)

    def test_partial_costs_and_each_token_bucket_remain_unavailable(self):
        data = self.attributed([
            raw_event(conversationId="sand-subagent-run-a"),
            raw_event(conversationId="sand-subagent-run-a", chargedCents=None, tokenUsage={"inputTokens": 10, "outputTokens": 3, "cacheReadTokens": 20}),
        ])
        row = routine_rows(data)[0]
        self.assertIsNone(row["usageCents"])
        self.assertIsNone(row["modelCostCents"])
        self.assertIsNone(row["chargedCents"])
        self.assertIsNone(row["tokens"])
        self.assertEqual(row["inputTokens"], 20)
        self.assertIsNone(row["cacheWriteTokens"])
        self.assertEqual(row["coverage"]["cacheWriteTokens"], {"reportedEvents": 1, "missingEvents": 1, "complete": False})
        self.assertIn("unavailable for 1 of 2 events", row["detail"])

    def test_missing_routine_ids_do_not_merge_independent_runs(self):
        index = build_routine_index({"agents": [routines(routine=None), routines(routine=None, run="run-b")]})
        data = attribute_events(normalize_events([raw_event(conversationId="sand-subagent-run-a"), raw_event(conversationId="sand-subagent-run-b")], "account"), {"bot-a": "A"}, index)
        rows = routine_rows(data)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["routineId"] is None and row["runs"] == 1 for row in rows))
        self.assertEqual(len({row["id"] for row in rows}), 2)

    def test_unassigned_events_do_not_create_routine_rows(self):
        self.assertEqual(routine_rows(self.attributed([raw_event(conversationId="sand-subagent-unknown"), raw_event()])), [])


if __name__ == "__main__":
    unittest.main()
