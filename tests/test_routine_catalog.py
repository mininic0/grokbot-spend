import copy
import json
import unittest

from collector.routine_catalog import normalize_catalog


def record(**changes):
    result = {
        'id': 'Routine-A', 'name': 'Synthetic automation', 'isEnabled': True,
        'schedule': '*/15 * * * *', 'createdAt': '2026-10-01T00:00:00Z',
        'lastRunAt': '2026-10-13T12:00:00Z', 'nextRunAt': '2026-10-13T12:15:00Z',
        'runs': [
            {'id': 'run-new', 'requestId': 'request-new', 'startedAt': '2026-10-13T12:00:00Z', 'finishedAt': '2026-10-13T12:01:00Z', 'status': 'completed'},
            {'id': 'run-old', 'requestId': 'request-old', 'startedAt': '2026-10-13T11:45:00Z', 'finishedAt': '2026-10-13T11:46:00Z', 'status': 'failed'},
        ],
    }
    result.update(changes)
    return result


def response(*records, bot='Bot-A'):
    return {'agents': [{'agentId': bot, 'automations': [{'automationId': 'Routine-A', 'recordJson': json.dumps(value)} for value in records]}]}


class RoutineCatalogTests(unittest.TestCase):
    def test_enabled_and_disabled_exact_metadata(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                value = normalize_catalog(response(record(isEnabled=enabled)), ['Bot-A'])
                row = value['automations'][0]
                self.assertEqual(value['completeness'], 'complete')
                self.assertEqual((row['botId'], row['routineId']), ('Bot-A', 'Routine-A'))
                self.assertIs(row['isEnabled'], enabled)
                self.assertEqual(row['schedule'], '*/15 * * * *')
                self.assertEqual(row['retainedRunCount'], 2)
                self.assertEqual(row['latestRunStatus'], 'completed')
                self.assertEqual(row['runs'][0]['id'], 'run-new')
                self.assertEqual(row['createdAt'], '2026-10-01T00:00:00Z')

    def test_only_boolean_enabled_is_accepted(self):
        for enabled in (None, 0, 1, 'true', 'false', {}, []):
            with self.subTest(enabled=enabled):
                value = normalize_catalog(response(record(isEnabled=enabled)))
                self.assertIsNone(value['automations'][0]['isEnabled'])

    def test_strict_whitelist_drops_instruction_and_unknown_fields(self):
        raw = record(prompt='PRIVATE-PROMPT', trigger='PRIVATE-TRIGGER', triggerDescription='PRIVATE-DESCRIPTION', provenance={'secret': 'PRIVATE-PROVENANCE'}, filePath='PRIVATE-PATH', unknown='PRIVATE-UNKNOWN')
        raw['runs'][0].update(trigger='PRIVATE-RUN-TRIGGER', prompt='PRIVATE-RUN-PROMPT', arbitrary={'secret': 'PRIVATE-NESTED'})
        normalized = normalize_catalog(response(raw))
        self.assertNotIn('PRIVATE-', json.dumps(normalized))
        self.assertEqual(set(normalized['automations'][0]), {'botId', 'routineId', 'providerAutomationId', 'name', 'isEnabled', 'schedule', 'createdAt', 'lastRunAt', 'nextRunAt', 'retainedRunCount', 'latestRunStatus', 'runs', 'issues'})
        self.assertEqual(set(normalized['automations'][0]['runs'][0]), {'id', 'requestId', 'startedAt', 'finishedAt', 'status'})

    def test_unavailable_and_individual_fallback_are_not_empty_success(self):
        for flag in ('unavailable', 'readIndividually'):
            value = normalize_catalog({'agents': [{'agentId': 'Bot-A', flag: True}]}, ['Bot-A'])
            self.assertEqual(value['completeness'], 'unknown')
            self.assertEqual(value['groups'][0]['completeness'], 'unknown')
            self.assertIsNone(value['groups'][0]['automationCount'])

    def test_absent_response_and_missing_bot_are_unknown(self):
        for raw in (None, {}, {'agents': None}, {'agents': 'bad'}):
            value = normalize_catalog(raw, ['Bot-A'])
            self.assertEqual(value['completeness'], 'unknown')
            self.assertEqual(value['groups'], [{'botId': 'Bot-A', 'completeness': 'unknown', 'automationCount': None}])
        value = normalize_catalog(response(record()), ['Bot-A', 'Bot-B'])
        self.assertIn('missing-bot-group', value['issues'])
        self.assertEqual(len(value['automations']), 1)

    def test_valid_empty_proto_list_is_zero_inventory_not_disabled(self):
        value = normalize_catalog({'agents': [{'agentId': 'Bot-A'}]}, ['Bot-A'])
        self.assertEqual(value['completeness'], 'complete')
        self.assertEqual(value['groups'][0]['automationCount'], 0)
        self.assertEqual(value['automations'], [])

    def test_malformed_record_retains_wrapper_identity_only(self):
        value = normalize_catalog({'agents': [{'agentId': 'Bot-A', 'automations': [{'automationId': 'Routine-A', 'recordJson': 'PRIVATE-MALFORMED'}]}]})
        row = value['automations'][0]
        self.assertIsNone(row['routineId'])
        self.assertEqual(row['providerAutomationId'], 'Routine-A')
        self.assertIsNone(row['isEnabled'])
        self.assertIsNone(row['retainedRunCount'])
        self.assertEqual(value['completeness'], 'unknown')
        self.assertNotIn('PRIVATE-MALFORMED', json.dumps(value))

    def test_distinct_id_namespaces_and_no_wrapper_fallback(self):
        raw = record(); raw.pop('id')
        value = normalize_catalog(response(raw))
        self.assertIsNone(value['automations'][0]['routineId'])
        self.assertEqual(value['automations'][0]['providerAutomationId'], 'Routine-A')
        self.assertEqual(value['completeness'], 'unknown')
        value = normalize_catalog(response(record(id='Local-ID')))
        self.assertEqual(value['automations'][0]['routineId'], 'Local-ID')
        self.assertEqual(value['automations'][0]['providerAutomationId'], 'Routine-A')
        self.assertEqual(value['completeness'], 'complete')

    def test_duplicates_do_not_pick_a_false_current_state(self):
        same = normalize_catalog(response(record(), record()))
        self.assertEqual(len(same['automations']), 1)
        self.assertEqual(same['groups'][0]['automationCount'], 1)
        conflicting = normalize_catalog(response(record(), record(isEnabled=False)))
        self.assertEqual(conflicting['completeness'], 'unknown')
        self.assertIsNone(conflicting['automations'][0]['isEnabled'])
        self.assertEqual(conflicting['automations'][0]['name'], 'Synthetic automation')

    def test_run_duplicates_bad_entries_and_missing_history(self):
        raw = record(); raw['runs'].append(copy.deepcopy(raw['runs'][0]))
        self.assertEqual(normalize_catalog(response(raw))['automations'][0]['retainedRunCount'], 2)
        raw['runs'].append({'id': 'run-new', 'status': 'failed'})
        row = normalize_catalog(response(raw))['automations'][0]
        self.assertIsNone(row['latestRunStatus'])
        self.assertIn('conflicting-run-metadata', row['issues'])
        raw['runs'].append(None)
        self.assertIsNone(normalize_catalog(response(raw))['automations'][0]['retainedRunCount'])
        self.assertIsNone(normalize_catalog(response(record(runs=None)))['automations'][0]['retainedRunCount'])
        self.assertEqual(normalize_catalog(response(record(runs=[])))['automations'][0]['retainedRunCount'], 0)

    def test_schedule_objects_and_prose_timestamps_are_not_retained(self):
        row = normalize_catalog(response(record(schedule={'prompt': 'PRIVATE-SCHEDULE'}, createdAt='PRIVATE-TIMESTAMP', lastRunAt=True, nextRunAt=1720000000000)))['automations'][0]
        self.assertIsNone(row['schedule'])
        self.assertIsNone(row['createdAt'])
        self.assertIsNone(row['lastRunAt'])
        self.assertEqual(row['nextRunAt'], 1720000000000)
        self.assertNotIn('PRIVATE-', json.dumps(row))

    def test_numeric_time_units_are_not_guessed_for_latest_run(self):
        raw = record()
        raw['runs'][0]['startedAt'] = 1720000000000
        row = normalize_catalog(response(raw))['automations'][0]
        self.assertIsNone(row['latestRunStatus'])
        self.assertIn('latest-run-order-unavailable', row['issues'])

    def test_homogeneous_numeric_milliseconds_order_latest_run(self):
        raw = record()
        raw['runs'][0]['startedAt'] = 1791892800000
        raw['runs'][1]['startedAt'] = 1791891900000
        row = normalize_catalog(response(raw))['automations'][0]
        self.assertEqual(row['latestRunStatus'], 'completed')
        self.assertNotIn('latest-run-order-unavailable', row['issues'])

    def test_exact_scope_and_unknown_flags(self):
        value = normalize_catalog(response(record(), bot='bot-a'), ['Bot-A'])
        self.assertEqual(value['automations'], [])
        self.assertIn('unexpected-bot-group', value['issues'])
        raw = response(record()); raw['agents'][0]['unavailable'] = 1
        self.assertEqual(normalize_catalog(raw)['completeness'], 'unknown')
        raw = response(record()); raw['agents'].append(copy.deepcopy(raw['agents'][0]))
        value = normalize_catalog(raw)
        self.assertEqual(len(value['groups']), 1)
        self.assertEqual(value['groups'][0]['completeness'], 'unknown')


if __name__ == '__main__':
    unittest.main()
