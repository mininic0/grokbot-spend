"""Metadata-only normalization and exact, account-local billing attribution.

Provider model cost and provider charged cost are separate measurements. Missing
amounts stay unavailable. A model name, timestamp, or cloud-agent ID never proves
which Grok Bot owns an event. Callers must supply the roster and routine response
for the same account as the events; mixed-account batches fail closed.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


TOKEN_FIELDS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")
COST_FIELDS = ("modelCostCents", "chargedCents")
METRIC_FIELDS = TOKEN_FIELDS + COST_FIELDS


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _number(value: Any, *, counter: bool = False) -> int | float | None:
    """Accept JSON numbers/numeric strings, excluding bool, NaN and infinity."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    if counter and (number < 0 or number != number.to_integral_value()):
        return None
    if number == number.to_integral_value():
        return int(number)
    result = float(number)
    return result if math.isfinite(result) else None


def _timestamp(value: Any) -> tuple[int | None, str | None]:
    """Dashboard epoch milliseconds; explicit ISO timestamps are also accepted."""
    milliseconds = _number(value, counter=True)
    try:
        if milliseconds is not None:
            moment = datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
        elif isinstance(value, str):
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if moment.tzinfo is None:
                return None, None
            moment = moment.astimezone(timezone.utc)
            milliseconds = int(moment.timestamp() * 1000)
            if milliseconds < 0:
                return None, None
        else:
            return None, None
        return milliseconds, moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None, None


def _digest(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_events(raw_events: list[dict], account_key: str) -> list[dict]:
    """Normalize a complete pull without conflating identical anonymous events.

    Native event IDs are account scoped. Otherwise, the identity hashes only the
    retained billing metadata, with an ordinal for identical occurrences. This
    is stable across repeated complete pulls (including event order changes),
    not an authorization to deduplicate separate pagination pages by content.
    No arbitrary source fields, credentials or conversation bodies survive.
    """
    if not isinstance(account_key, str) or not account_key.strip():
        raise ValueError("A nonempty account_key is required")
    if not isinstance(raw_events, list):
        raise TypeError("raw_events must be a list")
    scope = _digest(account_key)[:24]
    occurrences: Counter[str] = Counter()
    events = []
    native_seen: dict[str, dict[str, dict]] = {}
    for raw in raw_events:
        issues = []
        if not isinstance(raw, dict):
            raw = {}
            issues.append("malformed-event")
        usage = raw.get("tokenUsage")
        if not isinstance(usage, dict):
            if usage is not None:
                issues.append("malformed-tokenUsage")
            usage = {}
        timestamp, iso = _timestamp(raw.get("timestamp"))
        if raw.get("timestamp") is not None and timestamp is None:
            issues.append("invalid-timestamp")
        event = {
            "accountKey": account_key,
            "timestamp": timestamp,
            "timestampISO": iso,
            "model": _text(raw.get("model")),
            "conversationId": _text(raw.get("conversationId")),
            "cloudAgentId": _text(raw.get("cloudAgentId")),
            "automationId": _text(raw.get("automationId")),
            "clientType": _text(raw.get("clientType")),
            "product": _text(raw.get("product")),
            "kind": _text(raw.get("kind")),
            "isChargeable": raw.get("isChargeable") if isinstance(raw.get("isChargeable"), bool) else None,
        }
        products = raw.get("products")
        event["products"] = sorted({_text(value) for value in products if _text(value)}) if isinstance(products, list) else []
        if products is not None and (not isinstance(products, list) or any(not isinstance(value, str) for value in products)):
            issues.append("malformed-products")
        for field in TOKEN_FIELDS:
            event[field] = _number(usage.get(field), counter=True)
            if usage.get(field) is not None and event[field] is None:
                issues.append("invalid-" + field)
        for destination, value in (("modelCostCents", usage.get("totalCents")), ("chargedCents", raw.get("chargedCents"))):
            event[destination] = _number(value)
            if value is not None and event[destination] is None:
                issues.append("invalid-" + destination)
        # These four disjoint provider buckets are only summed when all exist.
        event["totalTokens"] = sum(event[field] for field in TOKEN_FIELDS) if all(event[field] is not None for field in TOKEN_FIELDS) else None
        event["issues"] = issues
        fingerprint = _digest(event)
        native_id = next((_text(raw.get(key)) for key in ("eventId", "usageEventId", "id") if _text(raw.get(key))), None)
        if native_id:
            base = "native:" + scope + ":" + native_id
            # Exact native repeats are replayed observations, not new requests.
            variants = native_seen.setdefault(base, {})
            if fingerprint in variants:
                continue
            event["eventId"] = base
            event["identitySource"] = "native"
            if variants:
                # A conflicting native identity is retained, never overwritten.
                for previous_fingerprint, previous in variants.items():
                    previous["eventId"] = base + ":conflict:" + previous_fingerprint
                    if "native-id-conflict" not in previous["issues"]:
                        previous["issues"].append("native-id-conflict")
                event["eventId"] += ":conflict:" + fingerprint
                event["issues"].append("native-id-conflict")
            variants[fingerprint] = event
        else:
            ordinal = occurrences[fingerprint]
            occurrences[fingerprint] += 1
            event["eventId"] = "derived:" + scope + ":" + fingerprint + ":" + str(ordinal)
            event["identitySource"] = "metadata-occurrence"
        events.append(event)
    return events


def build_routine_index(response: dict) -> dict:
    """Index exact run IDs; competing owner/routine claims remain ambiguous.

    Input is ListGrokBotAccountAutomations' agents[].automations[].recordJson.
    Historical runs absent from this response cannot be inferred from timing.
    """
    candidates: dict[str, dict[tuple[str, str | None], dict]] = {}
    agents = response.get("agents") if isinstance(response, dict) else None
    if not isinstance(agents, list):
        return {}
    for agent in agents:
        if not isinstance(agent, dict) or not (bot_id := _text(agent.get("agentId"))):
            continue
        automations = agent.get("automations")
        if not isinstance(automations, list):
            continue
        for automation in automations:
            if not isinstance(automation, dict):
                continue
            record = automation.get("recordJson")
            if isinstance(record, str):
                try:
                    record = json.loads(record)
                except (ValueError, TypeError):
                    continue
            if not isinstance(record, dict) or not isinstance(record.get("runs"), list):
                continue
            # These are distinct namespaces in the native client: record.id
            # identifies the local routine; wrapper automationId is a server ID
            # used for webhook APIs. Do not replace one with the other.
            routine_id = _text(record.get("id"))
            name = _text(record.get("name"))
            for run in record["runs"]:
                if not isinstance(run, dict) or not (run_id := _text(run.get("id"))):
                    continue
                key = "sand-subagent-" + run_id
                candidate = {"botId": bot_id, "routineId": routine_id, "name": name}
                owners = candidates.setdefault(key, {})
                identity = (bot_id, routine_id)
                if identity in owners and owners[identity]["name"] != name:
                    candidate["name"] = None  # A title conflict must not change ownership.
                owners[identity] = candidate
    index = {}
    for key, owners in sorted(candidates.items()):
        choices = sorted(owners.values(), key=lambda item: (item["botId"], item["routineId"] or ""))
        index[key] = choices[0] if len(choices) == 1 else {"ambiguous": True, "candidates": choices}
    return index


def merge_routine_indexes(existing: dict, current: dict) -> dict:
    """Retain historical exact run ownership without overwriting conflicts.

    Both indexes MUST belong to the same account; their storage/query owner is
    responsible for that boundary because this index has no account field.
    An absent run in a newer rolling history does not retract earlier evidence.
    Once ambiguous, a run stays ambiguous even if a later response has only one
    candidate. A newer display name may update an unchanged exact owner; names
    never establish ownership. Only the three ownership metadata fields survive.
    Malformed indexes raise rather than silently discard historical evidence.
    """
    candidates: dict[str, dict[tuple[str, str | None], dict]] = {}
    ambiguous = set()
    for index in (existing, current):
        if not isinstance(index, dict):
            raise ValueError("Routine ownership index must be an object")
        for run_id, entry in index.items():
            if not isinstance(run_id, str) or not run_id.startswith("sand-subagent-") or len(run_id) <= len("sand-subagent-") or not isinstance(entry, dict):
                raise ValueError("Invalid routine ownership entry")
            owners = candidates.setdefault(run_id, {})
            if entry.get("ambiguous") is True:
                ambiguous.add(run_id)
                entries = entry.get("candidates")
                if not isinstance(entries, list):
                    raise ValueError("Ambiguous routine ownership requires candidates")
            else:
                entries = [entry]
            for owner in entries:
                if not isinstance(owner, dict) or not (bot_id := _text(owner.get("botId"))):
                    raise ValueError("Routine ownership requires a bot ID")
                routine_id = _text(owner.get("routineId"))
                identity = (bot_id, routine_id)
                name = _text(owner.get("name"))
                if name is None and identity in owners:
                    name = owners[identity]["name"]
                owners[identity] = {"botId": bot_id, "routineId": routine_id, "name": name}
    merged = {}
    for run_id, owners in sorted(candidates.items()):
        choices = sorted(owners.values(), key=lambda item: (item["botId"], item["routineId"] or ""))
        merged[run_id] = choices[0] if len(choices) == 1 and run_id not in ambiguous else {"ambiguous": True, "candidates": choices}
    return merged


def scope_grok_events(events: list[dict], roster: dict[str, str], routine_index: dict) -> dict:
    """Select Grok Bot events using explicit product/client or exact IDs.

    This selects the product, not the owning bot. For example, a known Grok
    subagent remains in the result even when its parent cannot be attributed.
    Explicit other-product/client markers win over ID evidence. Model names
    never identify a product. Counts/reasons cover all records without returning
    excluded third-party activity to a Grok-only UI.
    """
    accounts = {event.get("accountKey") for event in events}
    invalid_scope = len(accounts) > 1 or (bool(events) and (None in accounts or "" in accounts))
    included = []
    reasons = {"included": Counter(), "excluded": Counter(), "unknown": Counter()}
    for event in events:
        if invalid_scope:
            reasons["unknown"]["mixed-or-missing-account"] += 1
            continue
        evidence = []
        contradictions = []
        client = _text(event.get("clientType"))
        if client == "grok-bot":
            evidence.append("clientType:grok-bot")
        elif client:
            contradictions.append("other-clientType")
        products = list(event.get("products") or [])
        if product := _text(event.get("product")):
            products.append(product)
        if "sand" in products:
            evidence.append("product:sand")
        if any(product != "sand" for product in products):
            contradictions.append("other-product")
        conversation = _text(event.get("conversationId"))
        if conversation and conversation in roster:
            evidence.append("account-roster-id")
        if conversation and isinstance(routine_index.get(conversation), dict):
            evidence.append("routine-run-id")
        if conversation and conversation.startswith("sand-subagent-") and len(conversation) > len("sand-subagent-"):
            evidence.append("sand-subagent-namespace")
        if contradictions:
            reason = "conflicting-product-evidence" if evidence else "+".join(contradictions)
            reasons["excluded"][reason] += 1
        elif evidence:
            # One reason per input row, even if several exact signals agree.
            reason = "+".join(evidence)
            reasons["included"][reason] += 1
            included.append({**event, "scopeEvidence": evidence})
        else:
            reasons["unknown"]["no-exact-grok-evidence"] += 1
    return {
        "events": included,
        "includedCount": len(included),
        "excludedCount": sum(reasons["excluded"].values()),
        "unknownCount": sum(reasons["unknown"].values()),
        "reasons": {kind: dict(sorted(counts.items())) for kind, counts in reasons.items()},
    }


def _summary(events: list[dict]) -> dict:
    result = {"eventCount": len(events)}
    for field in METRIC_FIELDS + ("totalTokens",):
        values = [event[field] for event in events if event.get(field) is not None]
        total = sum((Decimal(str(value)) for value in values), Decimal(0))
        result[field] = _number(str(total)) if values else None
        result[field + "MissingCount"] = len(events) - len(values)
    return result


def attribute_events(events: list, roster: dict[str, str], routine_index: dict) -> dict:
    """Attribute once by exact identifiers; grouped views intentionally overlap.

    `bots` includes directly billed bot work and its proven routine runs.
    `routines` is a subset, never an extra amount to add to `bots`. `totals`
    covers all input events, including unassigned records. A reported-only sum
    always includes a MissingCount so consumers can distinguish partial totals.
    """
    accounts = {event.get("accountKey") for event in events}
    mixed_accounts = len(accounts) > 1 or (bool(events) and (None in accounts or "" in accounts))
    annotated = []
    bot_events: dict[str, list] = {}
    routine_events: dict[tuple[str, str], list] = {}
    routine_metadata: dict[tuple[str, str], dict] = {}
    unassigned = []
    for original in events:
        event = dict(original)
        conversation = event.get("conversationId")
        match = routine_index.get(conversation) if conversation else None
        bot_id = routine_id = routine_name = None
        method = None
        reason = "unknown-conversation" if conversation else "missing-conversation-id"
        if mixed_accounts:
            reason = "mixed-or-missing-account"
        elif isinstance(match, dict) and match.get("ambiguous"):
            reason = "ambiguous-routine-parent"
        elif isinstance(match, dict) and _text(match.get("botId")):
            owner = match["botId"]
            if owner not in roster:
                reason = "routine-parent-not-in-account-roster"
            else:
                bot_id, routine_id, routine_name = owner, _text(match.get("routineId")), _text(match.get("name"))
                method = "routine-run-id"
        elif conversation in roster:
            bot_id, method = conversation, "bot-conversation-id"
        elif isinstance(conversation, str) and conversation.startswith("sand-subagent-"):
            reason = "unknown-subagent-parent"
        event["attribution"] = {
            "botId": bot_id, "routineId": routine_id, "routineName": routine_name,
            "method": method, "unassignedReason": None if method else reason,
        }
        annotated.append(event)
        if bot_id is None:
            unassigned.append(event)
            continue
        bot_events.setdefault(bot_id, []).append(event)
        if method == "routine-run-id":
            key = (bot_id, routine_id or conversation)
            routine_events.setdefault(key, []).append(event)
            routine_metadata[key] = {"botId": bot_id, "routineId": routine_id, "name": routine_name}
    bots = [{"botId": key, "name": roster[key], **_summary(values)} for key, values in sorted(bot_events.items())]
    routines = [{**routine_metadata[key], **_summary(values)} for key, values in sorted(routine_events.items())]
    return {
        "events": annotated, "bots": bots, "routines": routines, "unassigned": unassigned,
        "totals": _summary(annotated), "unassignedTotals": _summary(unassigned),
    }


def routine_rows(attribution: dict) -> list[dict]:
    """Map proven routine runs to UI rows with complete-only cost/token totals.

    `usageCents` has the explicit `model-cost` basis. `chargedCents` is retained
    separately and is never substituted for on-demand or included usage. `runs`
    counts distinct matched conversation IDs, not API requests or all scheduled
    runs. Unknown routine IDs remain null and each known run stays separate.
    """
    groups: dict[tuple, list[dict]] = {}
    bot_names = {bot.get("botId"): bot.get("name") for bot in attribution.get("bots", [])}
    for event in attribution.get("events", []):
        match = event.get("attribution") or {}
        bot_id = _text(match.get("botId"))
        conversation = _text(event.get("conversationId"))
        if match.get("method") != "routine-run-id" or not bot_id or not conversation:
            continue
        routine_id = _text(match.get("routineId"))
        key = (event.get("accountKey"), bot_id, routine_id, None if routine_id else conversation)
        groups.setdefault(key, []).append(event)
    rows = []
    for key, events in groups.items():
        _, bot_id, routine_id, _ = key
        totals = _summary(events)
        metrics = METRIC_FIELDS + ("totalTokens",)
        complete = {field: totals[field] if totals[field + "MissingCount"] == 0 else None for field in metrics}
        coverage = {
            "eventCount": len(events),
            **{field: {
                "reportedEvents": len(events) - totals[field + "MissingCount"],
                "missingEvents": totals[field + "MissingCount"],
                "complete": totals[field + "MissingCount"] == 0,
            } for field in metrics},
        }
        names = {_text(event["attribution"].get("routineName")) for event in events}
        name = next(iter(names)) if len(names) == 1 and None not in names else None
        runs = len({event["conversationId"] for event in events})
        bot_name = _text(bot_names.get(bot_id)) or bot_id
        detail = f"{bot_name} · {runs} matched run{'s' if runs != 1 else ''} · Provider model cost"
        missing = totals["modelCostCentsMissingCount"]
        if missing:
            detail += f" unavailable for {missing} of {len(events)} events"
        rows.append({
            "id": "routine:" + _digest(key)[:24],
            "botId": bot_id,
            "routineId": routine_id,
            "name": name or routine_id or "Unnamed routine run",
            "botName": bot_name,
            "runs": runs,
            "usageCents": complete["modelCostCents"],
            "includedCents": None,
            "onDemandCents": None,
            "tokens": complete["totalTokens"],
            "attribution": "exact-routine-run-id",
            "detail": detail,
            "costBasis": "model-cost",
            "coverage": coverage,
            **complete,
        })
    return sorted(rows, key=lambda row: (row["usageCents"] is None, -(row["usageCents"] or 0), row["id"]))
