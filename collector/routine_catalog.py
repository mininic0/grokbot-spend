"""Whitelist automation inventory metadata without retaining instructions.

Callers supply a response from the same authenticated account as their roster.
This module performs no I/O. Inventory completeness says whether requested bot
inventories were readable, not whether their rolling run histories are complete.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Iterable


def _text(value):
    # Preserve identifiers exactly; whitespace/case normalization could change
    # identity. Empty and whitespace-only strings carry no useful evidence.
    return value if isinstance(value, str) and value.strip() else None


def _scalar(value):
    if isinstance(value, str):
        return value
    if type(value) is int or type(value) is float and math.isfinite(value):
        return value
    return None


def _timestamp(value):
    """Preserve valid ISO strings or numeric timestamps, reject prose/objects."""
    if type(value) is int:
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None


def _time_key(value):
    if isinstance(value, str):
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    # The observed provider records use numeric milliseconds. Compare them
    # directly only within a homogeneous numeric set; mixed encodings stay
    # unknown rather than depending on an implicit unit conversion.
    if type(value) in (int, float):
        return value
    return None


def _runs(record):
    raw = record.get("runs")
    if not isinstance(raw, list):
        return [], None, None, ["run-history-unavailable"]
    runs, issues = {}, []
    for item in raw:
        if not isinstance(item, dict) or not (identity := _text(item.get("id"))):
            issues.append("invalid-run-record")
            continue
        normalized = {
            "id": identity, "requestId": _text(item.get("requestId")),
            "startedAt": _timestamp(item.get("startedAt")),
            "finishedAt": _timestamp(item.get("finishedAt")),
            "status": _text(item.get("status")),
        }
        previous = runs.get(identity)
        if previous is not None and previous != normalized:
            issues.append("conflicting-run-metadata")
            normalized = {key: previous[key] if previous[key] == normalized[key] else None for key in normalized}
        runs[identity] = normalized
    values = sorted(runs.values(), key=lambda run: run["id"])
    latest = None
    if values and not issues:
        if len(values) == 1:
            latest = values[0]["status"]
        else:
            starts = [run["startedAt"] for run in values]
            homogeneous = all(isinstance(value, str) for value in starts) or all(type(value) in (int, float) for value in starts)
            keys = [_time_key(value) for value in starts]
            if homogeneous and all(key is not None for key in keys):
                last = max(keys)
                statuses = {run["status"] for run, key in zip(values, keys) if key == last}
                latest = next(iter(statuses)) if len(statuses) == 1 else None
            else:
                issues.append("latest-run-order-unavailable")
    # A count of retained records, never the total number of runs over time.
    count = len(values) if "invalid-run-record" not in issues else None
    return values, count, latest, sorted(set(issues))


def _automation(raw, bot_id):
    issues = []
    wrapper_id = _text(raw.get("automationId"))
    record = raw.get("recordJson")
    if isinstance(record, str):
        try:
            record = json.loads(record)
        except (ValueError, TypeError):
            record = None
    if not isinstance(record, dict):
        record = {}
        issues.append("automation-record-unavailable")
    # These are different namespaces: record.id is the local automation ID
    # used by the native list consumer; wrapper.automationId is a provider ID
    # used by the native credential/webhook consumer. Never substitute it.
    routine_id = _text(record.get("id"))
    if routine_id is None:
        issues.append("routine-id-unavailable")
    enabled = record.get("isEnabled")
    if type(enabled) is not bool:
        enabled = None
    runs, count, latest, run_issues = _runs(record)
    return {
        "botId": bot_id, "routineId": routine_id, "providerAutomationId": wrapper_id,
        "name": _text(record.get("name")), "isEnabled": enabled,
        "schedule": _scalar(record.get("schedule")),
        "createdAt": _timestamp(record.get("createdAt")),
        "lastRunAt": _timestamp(record.get("lastRunAt")),
        "nextRunAt": _timestamp(record.get("nextRunAt")),
        "retainedRunCount": count, "latestRunStatus": latest,
        "runs": runs, "issues": sorted(set(issues + run_issues)),
    }


def normalize_catalog(response, expected_bot_ids: Iterable[str] | None = None) -> dict:
    """Return an automation catalog containing only whitelisted metadata.

    Result keys: automations, completeness (complete/unknown), groups, issues.
    Automations preserve exact botId/routineId/name and strict bool-or-null
    isEnabled. Run metadata is retained history, not billing attribution and
    not proof of lifetime run counts. Missing inventory never means disabled.
    None, malformed groups and unresolved individual reads produce unknown
    completeness; available observations are preserved where possible.
    """
    result = {"automations": [], "completeness": "unknown", "groups": [], "issues": []}
    expected = None
    if expected_bot_ids is not None:
        supplied = list(expected_bot_ids) if isinstance(expected_bot_ids, Iterable) and not isinstance(expected_bot_ids, (str, bytes)) else [None]
        expected = {_text(identity) for identity in supplied}
        if None in expected:
            result["issues"].append("invalid-expected-bot-id")
            expected.discard(None)
    agents = response.get("agents") if isinstance(response, dict) else None
    if not isinstance(agents, list):
        result["issues"].append("inventory-unavailable")
        result["groups"] = [{"botId": identity, "completeness": "unknown", "automationCount": None} for identity in sorted(expected or [])]
        return result
    seen, rows = set(), {}
    anonymous = []
    for group in agents:
        if not isinstance(group, dict) or not (bot_id := _text(group.get("agentId"))):
            result["issues"].append("invalid-bot-group")
            continue
        if expected is not None and bot_id not in expected:
            result["issues"].append("unexpected-bot-group")
            continue
        group_issues = []
        if bot_id in seen:
            group_issues.append("duplicate-bot-group")
        seen.add(bot_id)
        for flag in ("unavailable", "readIndividually"):
            if flag in group and type(group[flag]) is not bool:
                group_issues.append("invalid-group-status")
            elif group.get(flag) is True:
                group_issues.append("bot-inventory-unavailable" if flag == "unavailable" else "individual-read-required")
        # A missing repeated field is the protobuf empty-list representation,
        # but unavailable/readIndividually still prevent an empty success claim.
        automations = group.get("automations", [])
        if not isinstance(automations, list):
            automations = []
            group_issues.append("invalid-automation-list")
        observed = set()
        for raw in automations:
            if not isinstance(raw, dict):
                group_issues.append("invalid-automation-record")
                continue
            row = _automation(raw, bot_id)
            if "automation-record-unavailable" in row["issues"] or row["routineId"] is None:
                group_issues.append("incomplete-automation-record")
            if row["routineId"] is None:
                anonymous.append(row)
                continue
            identity = (bot_id, row["routineId"])
            observed.add(identity)
            if identity in rows and rows[identity] != row:
                # Conflicting state must not turn into a false enabled/disabled
                # recommendation. Keep only fields all duplicate records agree on.
                previous = rows[identity]
                for key in row:
                    if key in ("botId", "routineId", "issues"):
                        continue
                    if previous[key] != row[key]:
                        row[key] = [] if key == "runs" else None
                row["issues"] = sorted(set(previous["issues"] + row["issues"] + ["conflicting-automation-metadata"]))
                group_issues.append("conflicting-automation-metadata")
            rows[identity] = row
        result["groups"].append({"botId": bot_id, "completeness": "unknown" if group_issues else "complete", "automationCount": None if group_issues else len(observed)})
        result["issues"].extend(group_issues)
    for bot_id in sorted((expected or set()) - seen):
        result["groups"].append({"botId": bot_id, "completeness": "unknown", "automationCount": None})
        result["issues"].append("missing-bot-group")
    result["automations"] = [rows[key] for key in sorted(rows)] + anonymous
    result["issues"] = sorted(set(result["issues"]))
    unique_groups = {}
    for group in result["groups"]:
        if group["botId"] in unique_groups:
            group = {"botId": group["botId"], "completeness": "unknown", "automationCount": None}
        unique_groups[group["botId"]] = group
    result["groups"] = [unique_groups[key] for key in sorted(unique_groups)]
    result["completeness"] = "unknown" if result["issues"] else "complete"
    return result
