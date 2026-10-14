import unittest
from unittest.mock import Mock

from collector.routine_provider import BASE_URL, RoutineSchemaError, read_account_automations


RECORD = {"automationId": "synthetic-routine", "recordJson": '{"id":"synthetic-routine","runs":[]}'}


class RoutineProviderTests(unittest.TestCase):
    def test_bulk_success_needs_no_individual_requests(self):
        request = Mock(return_value={"agents": [{"agentId": "bot-b", "automations": []}, {"agentId": "bot-a", "automations": [RECORD]}]})
        result = read_account_automations(request, ["bot-a", "bot-b"])
        request.assert_called_once_with(BASE_URL + "ListGrokBotAccountAutomations", {"agentIds": ["bot-a", "bot-b"]})
        self.assertEqual([g["agentId"] for g in result["agents"]], ["bot-a", "bot-b"])
        self.assertEqual(result["agents"][0]["automations"], [RECORD])

    def test_read_individually_uses_exact_bot_and_retains_other_groups(self):
        request = Mock(side_effect=[{"agents": [{"agentId": "bot-a", "readIndividually": True}, {"agentId": "bot-b", "automations": [RECORD]}]}, {"automations": [RECORD]}])
        result = read_account_automations(request, ["bot-a", "bot-b"], time_zone="UTC")
        self.assertEqual(request.call_count, 2)
        request.assert_called_with(BASE_URL + "ListGrokBotAgentAutomations", {"agentId": "bot-a", "timeZone": "UTC"})
        self.assertEqual(result["agents"], [{"agentId": "bot-a", "automations": [RECORD]}, {"agentId": "bot-b", "automations": [RECORD]}])

    def test_unavailable_does_not_become_synthetic_success(self):
        request = Mock(return_value={"agents": [{"agentId": "bot-a", "unavailable": True}]})
        self.assertEqual(read_account_automations(request, ["bot-a"]), {"agents": [{"agentId": "bot-a", "automations": [], "unavailable": True}]})
        self.assertEqual(request.call_count, 1)

    def test_auth_or_network_exceptions_propagate_unchanged(self):
        error = RuntimeError("synthetic provider read failure")
        request = Mock(side_effect=[{"agents": [{"agentId": "bot-a", "readIndividually": True}]}, error])
        with self.assertRaises(RuntimeError) as caught:
            read_account_automations(request, ["bot-a"])
        self.assertIs(caught.exception, error)

    def test_rejects_missing_duplicate_extra_and_conflicting_groups(self):
        variants = [[], [{"agentId": "other"}], [{"agentId": "bot-a"}, {"agentId": "bot-a"}], [{"agentId": "bot-a", "readIndividually": True, "unavailable": True}], [{"agentId": "bot-a", "readIndividually": True, "automations": [RECORD]}], [{"agentId": "bot-a", "unavailable": 1}]]
        for groups in variants:
            with self.subTest(groups=groups):
                with self.assertRaises(RoutineSchemaError):
                    read_account_automations(Mock(return_value={"agents": groups}), ["bot-a"])

    def test_rejects_malformed_individual_response(self):
        request = Mock(side_effect=[{"agents": [{"agentId": "bot-a", "readIndividually": True}]}, {"automations": "invalid"}])
        with self.assertRaises(RoutineSchemaError):
            read_account_automations(request, ["bot-a"])

    def test_empty_and_repeated_inputs_are_bounded_and_case_normalized(self):
        request = Mock()
        self.assertEqual(read_account_automations(request, []), {"agents": []})
        request.assert_not_called()
        request.return_value = {"agents": [{"agentId": "BOT-A"}]}
        result = read_account_automations(request, ["bot-a", "BOT-A"])
        self.assertEqual(len(result["agents"]), 1)
        self.assertEqual(request.call_args.args[1]["agentIds"], ["bot-a"])

    def test_large_bot_lists_use_observed_200_bot_batches(self):
        request = Mock(side_effect=lambda url, payload: {"agents": [{"agentId": identity} for identity in payload["agentIds"]]})
        result = read_account_automations(request, [f"synthetic-{n}" for n in range(201)])
        self.assertEqual(len(result["agents"]), 201)
        self.assertEqual([len(call.args[1]["agentIds"]) for call in request.call_args_list], [200, 1])


if __name__ == "__main__":
    unittest.main()
