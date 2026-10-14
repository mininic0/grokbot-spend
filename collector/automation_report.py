"""Offline automation rankings from already attributed, account-local events."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation


class ReportError(ValueError):
    pass


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except ValueError:
        return None


def _iso(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _amount(value):
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def _json_number(value):
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


def _text(value):
    return value if isinstance(value, str) and value.strip() else None


def with_inventory(snapshot, sidecar):
    """Apply explicitly selected newer state without changing billing events."""
    account = snapshot.get("account", {}).get("key") if isinstance(snapshot, dict) else None
    if not account or not isinstance(sidecar, dict) or sidecar.get("accountKey") != account:
        raise ReportError("The selected inventory belongs to a different or unknown account.")
    observed = _time(sidecar.get("observedAt"))
    catalog = sidecar.get("catalog")
    if observed is None or not isinstance(catalog, dict) or not isinstance(catalog.get("automations"), list):
        raise ReportError("The selected inventory has an unsupported format or observation time.")
    previous = snapshot.get("automationCatalog") or {}
    previous_time = _time(previous.get("observedAt")) or _time(snapshot.get("generatedAt"))
    if previous_time is not None and observed < previous_time:
        raise ReportError("The selected inventory is older than the snapshot's automation state.")
    return {**snapshot, "automationCatalog": {**catalog, "observedAt": _iso(observed)}}


def _summary(events):
    values = [_amount(event.get("chargedCents")) for event in events]
    known = [value for value in values if value is not None]
    reported = sum(known, Decimal(0)) if known or not events else None
    missing = len(events) - len(known)
    return {
        "eventCount": len(events),
        "conversationCount": len({_text(e.get("conversationId")) for e in events} - {None}),
        "recordedValueCents": _json_number(reported) if not missing else None,
        "reportedValueCents": _json_number(reported),
        "missingValueEventCount": missing,
    }


def build_report(snapshot, window="cycle", enabled_only=False):
    """Disjoint coverage buckets; amounts are observed usage, never savings.

    Both bounds are inclusive, matching a saved collection's cutoff. Recent
    windows are clamped to the collected billing period. Run counts are distinct
    matched conversations with events in the window, not all scheduled runs.
    """
    if window not in ("cycle", "7d", "24h"):
        raise ReportError("Choose cycle, 7d, or 24h.")
    if not isinstance(snapshot, dict) or not snapshot.get("capabilities", {}).get("events"):
        raise ReportError("Detailed events are unavailable in this snapshot; collect them before ranking automations.")
    account = snapshot.get("account", {}).get("key")
    if not _text(account):
        raise ReportError("The snapshot has no account scope.")
    period = snapshot.get("period", {})
    cycle_start, cycle_end = _time(period.get("start")), _time(period.get("end"))
    cutoff = _time(snapshot.get("eventCoverage", {}).get("through"))
    if cycle_start is None or cycle_end is None or cutoff is None or cycle_end <= cycle_start:
        raise ReportError("The snapshot is missing a valid billing period or event cutoff.")
    end = min(cutoff, cycle_end)
    if end < cycle_start:
        raise ReportError("The saved event cutoff precedes this billing period.")
    start = cycle_start if window == "cycle" else max(cycle_start, end - timedelta(days=7 if window == "7d" else 1))
    events = snapshot.get("events")
    if not isinstance(events, list):
        raise ReportError("The snapshot has no detailed event list.")
    bots = {}
    for row in snapshot.get("bots", []):
        if isinstance(row, dict) and row.get("attribution") != "unassigned" and _text(row.get("id")):
            bots[row["id"]] = _text(row.get("name")) or row["id"]
    catalog = snapshot.get("automationCatalog")
    catalog = catalog if isinstance(catalog, dict) else {}
    state_observed_at = catalog.get("observedAt") if _time(catalog.get("observedAt")) else snapshot.get("generatedAt")
    if _time(state_observed_at) is None:
        state_observed_at = None
    inventory = {}
    catalog_rows = catalog.get("automations", [])
    for row in catalog_rows if isinstance(catalog_rows, list) else []:
        if isinstance(row, dict) and _text(row.get("botId")) and _text(row.get("routineId")):
            inventory.setdefault((row["botId"], row["routineId"]), []).append(row)
    buckets = {key: [] for key in ("routineLinked", "namedBotWithoutRoutine", "unassignedChild", "unknown")}
    groups, undated, outside = {}, [], 0
    seen = set()
    for event in events:
        if not isinstance(event, dict) or event.get("accountKey") != account:
            raise ReportError("The snapshot contains events outside its account scope.")
        event_id = _text(event.get("eventId"))
        if event_id is None or event_id in seen or "native-id-conflict" in event.get("issues", []):
            raise ReportError("Event identities are missing, duplicated, or conflicting; totals cannot be ranked safely.")
        seen.add(event_id)
        moment = _time(event.get("timestampISO"))
        if moment is None:
            undated.append(event)
            continue
        if not start <= moment <= end:
            outside += 1
            continue
        match = event.get("attribution")
        match = match if isinstance(match, dict) else {}
        bot, routine = _text(match.get("botId")), _text(match.get("routineId"))
        conversation = _text(event.get("conversationId"))
        if (match.get("method") == "routine-run-id" and bot in bots and routine
                and conversation and conversation.startswith("sand-subagent-")):
            bucket = "routineLinked"
            groups.setdefault((bot, routine), []).append(event)
        elif match.get("method") == "bot-conversation-id" and bot in bots and conversation == bot:
            bucket = "namedBotWithoutRoutine"
        elif conversation and conversation.startswith("sand-subagent-"):
            bucket = "unassignedChild"
        else:
            bucket = "unknown"
        buckets[bucket].append(event)
    selected = [event for values in buckets.values() for event in values]
    totals = _summary(selected)
    coverage = {key: _summary(values) for key, values in buckets.items()}
    for summary in coverage.values():
        complete_value, total_value = summary["recordedValueCents"], totals["recordedValueCents"]
        summary["valuePercent"] = (float(Decimal(str(complete_value)) / Decimal(str(total_value)) * 100)
                                   if not undated and complete_value is not None and total_value is not None and total_value > 0 else None)
    rows = []
    for (bot, routine), values in groups.items():
        summary = _summary(values)
        names = {_text(event["attribution"].get("routineName")) for event in values} - {None}
        name = next(iter(names)) if len(names) == 1 else routine
        inventory_rows = inventory.get((bot, routine), [])
        current_names = {_text(row.get("name")) for row in inventory_rows} - {None}
        if len(current_names) == 1:
            name = next(iter(current_names))
        states = {row.get("isEnabled") if isinstance(row.get("isEnabled"), bool) else None for row in inventory_rows}
        enabled = next(iter(states)) if len(states) == 1 else None
        state = "enabled" if enabled is True else "disabled" if enabled is False else "unknown"
        runs = summary["conversationCount"]
        value = summary["recordedValueCents"]
        rows.append({"botId": bot, "botName": bots[bot], "routineId": routine, "name": name,
                     "isEnabled": enabled, "state": state,
                     "stateObservedAt": state_observed_at if enabled is not None else None,
                     **summary, "matchedRuns": runs,
                     "averageRecordedValueCentsPerMatchedRun": _json_number(Decimal(str(value)) / runs) if value is not None and runs else None,
                     "lastActivity": _iso(max(_time(event["timestampISO"]) for event in values))})
    rows.sort(key=lambda row: (row["reportedValueCents"] is None, -(row["reportedValueCents"] or 0), row["botId"], row["routineId"]))
    state_counts = {state: sum(row["state"] == state for row in rows) for state in ("enabled", "disabled", "unknown")}
    if enabled_only:
        rows = [row for row in rows if row["isEnabled"] is True]
    event_coverage = snapshot.get("eventCoverage", {})
    return {
        "schemaVersion": 1, "snapshotGeneratedAt": snapshot.get("generatedAt"), "mode": snapshot.get("mode"),
        "window": {"name": window, "start": _iso(start), "end": _iso(end), "bounds": "inclusive", "endsAtSavedEventCutoff": end == cutoff},
        "costBasis": "recorded-usage-value", "rankingBasis": "reportedValueCents; incomplete rows are marked",
        "inventoryCompleteness": "complete" if catalog.get("completeness") == "complete" else "unknown",
        "matchedAutomationStateCounts": state_counts,
        "rankingFilter": {"enabledOnly": enabled_only,
                          "excludedDisabledCount": state_counts["disabled"] if enabled_only else 0,
                          "excludedUnknownStateCount": state_counts["unknown"] if enabled_only else 0},
        "totals": totals, "coverage": coverage, "automations": rows,
        "outsideWindowEventCount": outside, "undatedEvents": _summary(undated),
        "productScopeOmissions": {"scope": "entire saved collection; cannot filter omitted events by date",
                                  "unknownProductEventCount": event_coverage.get("unknownCount"),
                                  "excludedOtherProductEventCount": event_coverage.get("excludedCount")},
        "limitations": [
            "Recorded usage value can include plan consumption; it is not an invoice or guaranteed savings from disabling an automation.",
            "Names and ownership come only from exact saved account-local attribution. Historical ownership may be incomplete.",
            "Enabled state is observed at inventory collection time (snapshot time for older files), not the event's historical state; absent inventory means unknown.",
            "Matched runs count distinct conversations with events inside this window; a spanning run contributes only its in-window events.",
            "Missing amounts and timestamps remain unavailable. Ranking uses only reported amounts, with missing counts shown.",
        ],
    }


def _safe(value):
    # Bot/routine names are user metadata, not terminal control sequences.
    return "".join(c if c.isprintable() else " " for c in str(value))


def _money(cents):
    return "unavailable" if cents is None else f"${Decimal(str(cents)) / 100:,.2f}"


def _value(summary):
    if summary["missingValueEventCount"]:
        return f"{_money(summary['reportedValueCents'])} reported; {summary['missingValueEventCount']} events missing value"
    return _money(summary["recordedValueCents"])


def render_text(report, limit=20):
    window = report["window"]
    lines = [f"Automations · {window['name']} · {_safe(report.get('mode') or 'saved snapshot')}",
             f"{window['start']} through {window['end']} (inclusive UTC)",
             f"Snapshot: {_safe(report.get('snapshotGeneratedAt'))}",
             "Recorded usage value; not an invoice or guaranteed savings.", "",
             f"Identified Grok Bot events: {_value(report['totals'])} ({report['totals']['eventCount']:,} events)"]
    labels = {"routineLinked": "Linked to named automations", "namedBotWithoutRoutine": "Named bot, no proven automation",
              "unassignedChild": "Unassigned child runs", "unknown": "Other unknown ownership"}
    for key, label in labels.items():
        summary = report["coverage"][key]
        percent = "" if summary["valuePercent"] is None else f"; {summary['valuePercent']:.1f}% of value"
        lines.append(f"  {label}: {_value(summary)} ({summary['eventCount']:,} events{percent})")
    if report["undatedEvents"]["eventCount"]:
        lines.append(f"Undated events excluded from window: {_value(report['undatedEvents'])} ({report['undatedEvents']['eventCount']:,} events)")
    omitted = report["productScopeOmissions"]
    if omitted["unknownProductEventCount"]:
        lines.append(f"Product unidentified: {omitted['unknownProductEventCount']:,} additional events in the saved collection; amounts/window unavailable.")
    rows = report["automations"]
    if report["rankingFilter"]["enabledOnly"]:
        filtered = report["rankingFilter"]
        lines += ["", f"Enabled-only ranking excludes {filtered['excludedDisabledCount']} disabled and {filtered['excludedUnknownStateCount']} unknown-state automations; coverage above includes all states."]
    lines += ["", f"Ranked automations ({min(limit, len(rows))} of {len(rows)}; observed subsets)"]
    for position, row in enumerate(rows[:limit], 1):
        lines.append(f"{position}. {_safe(row['name'])} — {_safe(row['botName'])} [{row['state']}]")
        lines.append(f"   {_value(row)} · {row['matchedRuns']:,} matched runs · {_money(row['averageRecordedValueCentsPerMatchedRun'])}/matched run")
        lines.append(f"   Last activity {row['lastActivity']} · automation {_safe(row['routineId'])}")
    if not rows:
        lines.append("No confirmed enabled automation with matched events in this window." if report["rankingFilter"]["enabledOnly"]
                     else "No exact automation-linked events in this window.")
    if len(rows) > limit:
        lines.append("Use --limit to show more; JSON includes all matching automations.")
    lines += ["", *report["limitations"]]
    return "\n".join(lines) + "\n"
