#!/usr/bin/env python3
"""Read-only Grok Bot billing collector. Python standard library only."""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import csv
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_DIR = Path.home() / "Library/Application Support/Grok Spend"
DEFAULT_OUTPUT = DEFAULT_DIR / "snapshot.json"
GROUPS = {"bots": 4, "models": 1, "sources": 3, "products": 5}
SPEND_TYPES = {"usageCents": 3, "includedCents": 2, "onDemandCents": 1}
FLOW_NAMES = {"user": "Your requests", "automation": "Automations", "agent": "Other bots", "group": "Group activity"}
READ_METHODS = {"GetMe", "GetTeams", "GetSandUsageStatus", "GetMonthlyBillingCycle", "GetDailySpendByCategory", "GetAggregatedUsageEvents", "GetCurrentPeriodUsage"}


class CollectionError(Exception):
    pass


class AuthenticationError(CollectionError):
    pass


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not n.is_finite():
        return None
    return int(n) if n == n.to_integral_value() else float(n)


def precise_sum(values):
    return number(sum((Decimal(str(n)) for v in values if (n := number(v)) is not None), Decimal(0)))


def complete_sum(values):
    """An omitted measurement must not masquerade as free usage."""
    values = list(values)
    return precise_sum(values) if all(value is not None for value in values) else None


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_ms(value):
    n = number(value)
    if n is None:
        return None
    try:
        return dt.datetime.fromtimestamp(n / 1000, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def jwt_claims(token):
    try:
        part = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        if not isinstance(value, dict) or not isinstance(value.get("sub"), str):
            raise ValueError()
        return value
    except (IndexError, ValueError, UnicodeError):
        raise CollectionError("The existing Grok Bot session is not a supported access token. Sign in to Grok Bot again.") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Provider:
    """Only named read-only operations; credentials never cross a redirect."""
    def __init__(self, token, team_id=None):
        self.token = token
        self.claims = jwt_claims(token)
        self.team_id = team_id
        self.tls = ssl.create_default_context()
        # python.org macOS installs may not ship a configured root store. Use
        # the OS-maintained CA bundle, while retaining hostname verification.
        if not os.environ.get("SSL_CERT_FILE") and Path("/etc/ssl/cert.pem").is_file():
            self.tls.load_verify_locations(cafile="/etc/ssl/cert.pem")

    def _request(self, url, payload, dashboard=False):
        headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "GrokSpend/0.1"}
        if dashboard:
            subject = self.claims["sub"].rsplit("|", 1)[-1]
            headers.update({"Origin": "https://cursor.com", "Cookie": "WorkosCursorSessionToken=" + urllib.parse.quote(subject + "::" + self.token, safe="")})
        else:
            headers.update({"Authorization": "Bearer " + self.token, "Connect-Protocol-Version": "1", "x-ghost-mode": "true"})
            if self.team_id is not None:
                headers["x-cursor-team-id"] = str(self.team_id)
        request = urllib.request.Request(url, json.dumps(payload).encode(), headers, method="POST")
        try:
            with urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=self.tls)).open(request, timeout=25) as response:
                raw = response.read(32 * 1024 * 1024 + 1)
                if len(raw) > 32 * 1024 * 1024:
                    raise CollectionError("Provider response exceeded the safety size limit.")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise CollectionError("Provider returned an unsupported response shape.")
                return result
        except urllib.error.HTTPError as error:
            # Do not emit response bodies; they can contain account data.
            if error.code == 401:
                raise AuthenticationError("The provider rejected the saved sign-in. Open Grok Bot, then refresh here.") from None
            if error.code == 403:
                raise CollectionError(f"Provider denied this read (HTTP {error.code}). Check Grok Bot sign-in and account permissions.") from None
            raise CollectionError(f"Provider read failed (HTTP {error.code}).") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                raise CollectionError("TLS certificate validation failed. Configure a trusted Python CA bundle; certificate verification remains enabled.") from None
            raise CollectionError("The billing service could not be reached. Check your connection and retry.") from None
        except (TimeoutError, OSError):
            raise CollectionError("The billing service could not be reached. Check your connection and retry.") from None
        except (json.JSONDecodeError, UnicodeError):
            raise CollectionError("Provider returned invalid JSON.") from None

    def dashboard(self, method, payload=None):
        if method not in READ_METHODS:
            raise CollectionError("This operation is not in the read-only billing allowlist.")
        return self._request("https://api2.cursor.sh/aiserver.v1.DashboardService/" + method, payload or {})

    def routines(self, agent_ids):
        try:
            from routine_provider import read_account_automations, RoutineSchemaError
        except ImportError:
            from collector.routine_provider import read_account_automations, RoutineSchemaError
        try:
            return read_account_automations(self._request, agent_ids)
        except RoutineSchemaError:
            raise CollectionError("Automation ownership records have an unsupported or incomplete shape.") from None

    def events(self, start, end):
        events, expected, page_fingerprints = [], None, set()
        started = time.monotonic()
        for page in range(1, 101):
            if time.monotonic() - started > 120:
                raise CollectionError("Detailed-event collection exceeded two minutes; daily totals are still available.")
            payload = {"page": page, "pageSize": 1000, "startDate": str(start), "endDate": str(end)}
            if self.team_id is not None:
                payload["teamId"] = self.team_id
            result = self._request("https://cursor.com/api/dashboard/get-filtered-usage-events", payload, dashboard=True)
            rows = result.get("usageEventsDisplay")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise CollectionError("Detailed billing events have an unsupported schema.")
            total = number(result.get("totalUsageEventsCount"))
            if not isinstance(total, int) or total < 0:
                raise CollectionError("Detailed billing events did not provide a valid total count; completeness is unavailable.")
            if expected is not None and total != expected:
                raise CollectionError("Billing changed during pagination. Retry for a consistent event snapshot.")
            expected = total
            fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
            if rows and fingerprint in page_fingerprints:
                raise CollectionError("The billing service repeated a page; complete event coverage is unavailable.")
            page_fingerprints.add(fingerprint)
            events.extend(rows)
            if not rows or (expected is not None and len(events) >= expected) or (expected is None and len(rows) < 1000):
                if expected is not None and len(events) < expected:
                    raise CollectionError("The provider returned an incomplete billing page sequence; event totals are unavailable.")
                if expected is not None:
                    try:
                        from attribution import normalize_events
                    except ImportError:
                        from collector.attribution import normalize_events
                    normalized = normalize_events(events, "pagination-check")
                    if any("native-id-conflict" in event.get("issues", []) for event in normalized):
                        raise CollectionError("A billing event ID has conflicting amounts or metadata; event totals are unavailable.")
                    if len(normalized) != expected:
                        raise CollectionError("Billing event identities overlap across pages; complete event coverage is unavailable.")
                return events
        raise CollectionError("The billing event page limit was reached; event totals are unavailable.")


def access_token(helper, no_prompt=False):
    command = [str(helper), "access-token"]
    if no_prompt:
        command.append("--no-prompt")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15 if no_prompt else 190)
    except (OSError, subprocess.TimeoutExpired):
        raise AuthenticationError("The local session helper could not run. Build the app or check Keychain access.") from None
    if result.returncode:
        # Helper emits deliberately non-sensitive diagnostic strings only.
        safe = result.stderr.strip()
        if len(safe) > 500 or any(word in safe.lower() for word in ("eyj", "bearer", "refresh-token")):
            safe = "The session helper could not read the current Grok Bot sign-in."
        raise AuthenticationError(safe or "The session helper could not read the current Grok Bot sign-in.")
    token = result.stdout.strip()
    claims = jwt_claims(token)
    expiry = number(claims.get("exp"))
    if expiry is not None and expiry <= dt.datetime.now().timestamp():
        raise AuthenticationError("The saved Grok Bot access token expired. Open Grok Bot to renew its normal session, then refresh here.")
    return token


def read_roster(subjects, directory=None):
    directory = directory or Path.home() / "Library/Application Support/Grok Bot/sand-client-persistence"
    expected = {"sand.client.slice.account." + urllib.parse.quote(s, safe="") + ".roster.last-roster" for s in subjects if s}
    roster = {}
    for path in directory.glob("*.blob"):
        try:
            key = base64.b32decode(path.stem.upper() + "=" * (-len(path.stem) % 8)).decode()
            if key not in expected:
                continue
            envelope = json.loads(path.read_text())
            data = envelope.get("value")
            if isinstance(data, str):
                data = json.loads(data)
            for row in data.get("rows", []):
                if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]:
                    label = next((row[key] for key in ("title", "name") if isinstance(row.get(key), str) and row[key].strip()), row["id"])
                    roster[row["id"]] = label
        except (OSError, ValueError, UnicodeError, AttributeError):
            continue
    return roster


def daily_rows(response):
    rows = response.get("dailySpend", [])
    if not isinstance(rows, list):
        raise CollectionError("Daily billing data has an unsupported shape.")
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            raise CollectionError("A daily billing row is invalid.")
        day = iso_ms(row.get("day"))
        # The live service returns token-bearing rows with NO spendCents at all.
        # A protobuf decoder defaults that to zero, but that does not establish
        # a measured dollar amount. Retain absence for an honest cost display.
        cents = number(row.get("spendCents"))
        category = row.get("category", "")
        if day is None or ("spendCents" in row and cents is None) or not isinstance(category, str):
            raise CollectionError("A daily billing row has an invalid date, category, or amount.")
        normalized.append({"date": day[:10], "category": category or "unattributed", "cents": cents, "tokens": number(row.get("totalTokens"))})
    return normalized


def summarize_group(responses, names=None):
    names = names or {}
    categories = set()
    indexed = {}
    for metric, response in responses.items():
        if response is None:
            indexed[metric] = None
            continue
        rows = daily_rows(response)
        by_category = {}
        for row in rows:
            categories.add(row["category"])
            by_category.setdefault(row["category"], []).append(row)
        indexed[metric] = by_category
    output = []
    for category in categories:
        record = {"id": category, "name": names.get(category, "Unattributed" if category in ("Other", "unattributed") else category), "runs": None, "attribution": "provider", "detail": "Provider daily billing totals"}
        for metric in SPEND_TYPES:
            grouped = indexed.get(metric)
            record[metric] = complete_sum(row["cents"] for row in grouped.get(category, [])) if grouped is not None else None
        usage = indexed.get("usageCents")
        token_values = [row["tokens"] for row in usage.get(category, [])] if usage is not None else None
        record["tokens"] = precise_sum(token_values) if token_values is not None and all(v is not None for v in token_values) else None
        if record["usageCents"] is None:
            record["detail"] = "Provider token totals; dollar amounts unavailable"
        output.append(record)
    return sorted(output, key=lambda row: (row["usageCents"] is not None, row["usageCents"] or 0, row["tokens"] or 0, row["id"]), reverse=True)


def reconciliation(bots, reference):
    if reference is None:
        return {"status": "unavailable", "referenceCents": None, "attributedCents": None, "differenceCents": None, "coveragePercent": None, "label": "Provider product total unavailable"}
    if any(row.get("usageCents") is None for row in bots):
        return {"status": "unavailable", "referenceCents": reference, "attributedCents": None, "differenceCents": None, "coveragePercent": None, "label": "Bot billing totals unavailable"}
    bot_total = precise_sum(row["usageCents"] for row in bots)
    named = precise_sum(row["usageCents"] for row in bots if row["id"] not in ("Other", "unattributed", "other-bots", ""))
    difference = number(Decimal(str(reference)) - Decimal(str(bot_total)))
    return {"status": "matched" if difference == 0 else "mismatch", "referenceCents": reference, "attributedCents": bot_total, "differenceCents": difference, "coveragePercent": round(named / reference * 100, 2) if reference > 0 else None, "label": "Bot totals match provider product totals" if difference == 0 else "Bot and product totals differ; billing may be delayed"}


def build_snapshot(account_key, label, period, weekly, grouped, roster, warnings):
    views = {group: summarize_group(grouped.get(group, {}), roster if group == "bots" else FLOW_NAMES if group == "sources" else {}) for group in GROUPS}
    totals = {}
    product_response = grouped.get("products", {})
    for metric in SPEND_TYPES:
        value = product_response.get(metric)
        totals[metric] = complete_sum(row["cents"] for row in daily_rows(value) if row["category"] == "sand") if value is not None else None
    days = {}
    for metric in ("usageCents", "onDemandCents"):
        response = product_response.get(metric)
        if response is not None:
            for row in daily_rows(response):
                if row["category"] == "sand":
                    record = days.setdefault(row["date"], {"date": row["date"], "usageCents": 0 if product_response.get("usageCents") is not None else None, "onDemandCents": 0 if product_response.get("onDemandCents") is not None else None})
                    record[metric] = complete_sum([record[metric], row["cents"]])
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    totals["todayCents"] = days.get(today, {}).get("usageCents", 0) if product_response.get("usageCents") is not None else None
    if totals["usageCents"] is None and any(row.get("tokens") for row in views["bots"]):
        warnings.append("The provider reports token usage but omits dollar amounts. Unavailable amounts are not zero-dollar usage.")
    rec = reconciliation(views["bots"], totals["usageCents"])
    if grouped.get("bots", {}).get("usageCents") is None:
        rec = {"status": "unavailable", "referenceCents": totals["usageCents"], "attributedCents": None, "differenceCents": None, "coveragePercent": None, "label": "Bot billing totals unavailable"}
    return {"schemaVersion": 1, "generatedAt": utc_now(), "mode": "live", "account": {"key": account_key, "label": label}, "period": period, "weekly": {"percentUsed": number(weekly.get("usagePercent")), "resetsAt": weekly.get("nextResetTimestampUtc")}, "totals": totals, "reconciliation": rec, "bots": views["bots"], "models": views["models"], "sources": views["sources"], "routines": [], "daily": sorted(days.values(), key=lambda row: row["date"]), "tokenBreakdown": {"input": None, "output": None, "cacheRead": None, "cacheWrite": None, "scope": "unavailable"}, "events": [], "warnings": warnings, "capabilities": {group: grouped.get(group, {}).get("usageCents") is not None for group in ("bots", "models", "sources")} | {"routines": False, "events": False}, "lastError": None}


def integrate_events(snapshot, normalized, roster, routine_index, through, routines_available):
    """Keep event model costs separate from provider daily billing amounts."""
    try:
        from attribution import scope_grok_events, attribute_events, routine_rows
    except ImportError:
        from collector.attribution import scope_grok_events, attribute_events, routine_rows
    if any(event.get("accountKey") != snapshot["account"]["key"] for event in normalized):
        raise CollectionError("Detailed events belong to a different account.")
    if any("native-id-conflict" in event.get("issues", []) for event in normalized):
        raise CollectionError("Detailed events contain conflicting native identities; amounts cannot be added safely.")
    scoped = scope_grok_events(normalized, roster, routine_index)
    result = attribute_events(scoped["events"], roster, routine_index)
    snapshot["events"] = result["events"]
    snapshot["routines"] = routine_rows(result)
    for row in snapshot["routines"]:
        row["eventValueCents"] = row.get("chargedCents")
    snapshot["capabilities"].update(events=True, routines=routines_available)
    totals = result["totals"]
    snapshot["eventCoverage"] = {key: scoped[key] for key in ("includedCount", "excludedCount", "unknownCount")}
    snapshot["eventCoverage"].update(through=through, unassignedCount=len(result["unassigned"]), reasons=scoped["reasons"])
    snapshot["eventTotals"] = totals
    event_days = {}
    for event in result["events"]:
        timestamp = event.get("timestampISO")
        if timestamp:
            event_days.setdefault(timestamp[:10], []).append(event.get("chargedCents"))
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    totals["todayRecordedValueCents"] = complete_sum(event_days.get(today, [])) if through and through[:10] == today and all(e.get("timestampISO") for e in result["events"]) else None
    daily = {row["date"]: row for row in snapshot["daily"]}
    for date, values in event_days.items():
        row = daily.setdefault(date, {"date": date, "usageCents": None, "onDemandCents": None})
        row["eventValueCents"] = complete_sum(values)
    snapshot["daily"] = sorted(daily.values(), key=lambda row: row["date"])
    snapshot["tokenBreakdown"] = {
        label: totals[field]
        for label, field in (("input", "inputTokens"), ("output", "outputTokens"), ("cacheRead", "cacheReadTokens"), ("cacheWrite", "cacheWriteTokens"))
    } | {"scope": f"Reported token buckets from {len(result['events'])} identified Grok Bot events through {through}. Missing fields are omitted from these partial sums; see JSON for missing counts."}
    bot_summaries = {row["botId"]: row for row in result["bots"]}
    known_bot_rows = {row["id"] for row in snapshot["bots"]}
    for bot_id, summary in bot_summaries.items():
        if bot_id not in known_bot_rows:
            snapshot["bots"].append({"id": bot_id, "name": summary["name"], "usageCents": None, "includedCents": None, "onDemandCents": None, "tokens": None, "runs": None, "attribution": "exact-event-owner", "detail": ""})
    for row in snapshot["bots"]:
        summary = bot_summaries.get(row["id"])
        if summary is not None:
            row["eventCount"] = summary["eventCount"]
            for field in ("modelCostCents", "chargedCents"):
                row[field] = summary[field] if summary[field + "MissingCount"] == 0 else None
            row["eventValueCents"] = row["chargedCents"]
            row["detail"] = f"{summary['eventCount']} matched events through {through}; recorded usage value is not an invoice charge"
    if result["unassigned"]:
        summary = result["unassignedTotals"]
        snapshot["bots"].append({"id": "unassigned-events", "name": "Unassigned child runs", "usageCents": None, "includedCents": None, "onDemandCents": None, "eventValueCents": summary["chargedCents"] if summary["chargedCentsMissingCount"] == 0 else None, "modelCostCents": summary["modelCostCents"] if summary["modelCostCentsMissingCount"] == 0 else None, "tokens": None, "runs": None, "attribution": "unassigned", "detail": f"{len(result['unassigned'])} Grok Bot events without a proven parent; excluded from named bot event values"})
    model_events = {}
    for event in result["events"]:
        if event.get("model"):
            model_events.setdefault(event["model"], []).append(event)
    by_model = {row["id"]: row for row in snapshot["models"]}
    for model, events in model_events.items():
        row = by_model.get(model)
        if row is None:
            row = {"id": model, "name": model, "usageCents": None, "includedCents": None, "onDemandCents": None, "tokens": None, "runs": None, "attribution": "provider-event-model", "detail": ""}
            snapshot["models"].append(row)
        row["eventValueCents"] = complete_sum(event.get("chargedCents") for event in events)
        row["modelCostCents"] = complete_sum(event.get("modelCostCents") for event in events)
        row["detail"] = f"{len(events)} identified Grok Bot events through {through}; recorded usage value is not an invoice charge"
    snapshot["eventCoverage"]["attributedValueCents"] = complete_sum(e.get("chargedCents") for e in result["events"] if e["attribution"].get("botId"))
    snapshot["eventCoverage"]["unassignedValueCents"] = complete_sum(e.get("chargedCents") for e in result["unassigned"])
    snapshot["capabilities"]["bots"] |= bool(result["events"])
    snapshot["capabilities"]["models"] |= bool(model_events)
    snapshot["warnings"].append("Event model cost, provider charged amounts, and daily usage totals are separate measurements. Events end two minutes before collection to stabilize pagination.")
    if scoped["unknownCount"]:
        snapshot["warnings"].append(f"{scoped['unknownCount']} events lack enough metadata to identify their product; they are excluded from Grok Bot event totals.")
    if result["unassigned"]:
        snapshot["warnings"].append(f"{len(result['unassigned'])} Grok Bot events have no proven bot or routine owner; they remain visible in matching model drilldowns.")
    return snapshot


def private_write(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".grok-spend-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def store_snapshot(snapshot, output):
    output = Path(output)
    output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    database = output.parent / "ledger.sqlite3"
    # Create with private permissions before sqlite opens it.
    fd = os.open(database, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(database, 0o600)
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE IF NOT EXISTS snapshots(account TEXT, period_start TEXT, captured_at TEXT, data TEXT, PRIMARY KEY(account, period_start, captured_at))")
        compact = {key: value for key, value in snapshot.items() if key != "events"}
        compact["eventCount"] = len(snapshot.get("events", []))
        db.execute("INSERT OR REPLACE INTO snapshots VALUES(?,?,?,?)", (snapshot["account"]["key"], snapshot["period"]["start"], snapshot["generatedAt"], json.dumps(compact, allow_nan=False)))
        columns = [row[1] for row in db.execute("PRAGMA table_info(events)")]
        if columns and "period_start" not in columns:
            # Preserve pre-release observations without treating their mutable
            # metadata hashes as a trustworthy deduplicated current event set.
            db.execute("ALTER TABLE events RENAME TO legacy_event_observations")
        db.execute("CREATE TABLE IF NOT EXISTS events(account TEXT, period_start TEXT, event_id TEXT, data TEXT, PRIMARY KEY(account,period_start,event_id))")
        events = snapshot.get("events", [])
        if snapshot.get("capabilities", {}).get("events", bool(events)):
            # Replace one COMPLETE queried period atomically. Anonymous event
            # metadata can be corrected; changed hashes must not double count.
            identity = (snapshot["account"]["key"], snapshot["period"]["start"])
            db.execute("DELETE FROM events WHERE account=? AND period_start=?", identity)
            for event in events:
                db.execute("INSERT INTO events VALUES(?,?,?,?)", (*identity, event["eventId"], json.dumps(event, allow_nan=False)))
    private_write(output, snapshot)


def collect(args, token_override=None, before_write=None):
    token = token_override if token_override is not None else access_token(args.auth_helper, args.no_prompt)
    provider = Provider(token, args.team_id)
    me = provider.dashboard("GetMe")
    user_id = me.get("userId")
    if not isinstance(user_id, int):
        raise CollectionError("The provider did not identify the signed-in account.")
    account_key = hashlib.sha256((provider.claims["sub"] + ":" + str(args.team_id)).encode()).hexdigest()[:24]
    account_label = " ".join(str(me.get(key) or "").strip() for key in ("firstName", "lastName")).strip() or "Grok Bot account"
    roster = read_roster([provider.claims["sub"], me.get("authId")])
    cycle_args = {"teamId": args.team_id} if args.team_id is not None else {}
    cycle = provider.dashboard("GetMonthlyBillingCycle", cycle_args)
    start, end = number(cycle.get("startDateEpochMillis")), number(cycle.get("endDateEpochMillis"))
    if start is None or end is None or end <= start or not iso_ms(start) or not iso_ms(end):
        raise CollectionError("The provider did not return valid billing-cycle bounds.")
    period = {"start": iso_ms(start), "end": iso_ms(end)}
    warnings = ["Usage cost and on-demand usage are provider-reported billing values, not a settled invoice. Daily buckets use UTC.", "Credit-wallet usage is included in requests; the provider may report it separately from included and on-demand amounts."]
    try:
        weekly = provider.dashboard("GetSandUsageStatus")
    except AuthenticationError:
        raise
    except CollectionError:
        weekly = {}
        warnings.append("Weekly allowance is unavailable.")
    base = {"userId": user_id, "periodStartMs": str(start), "periodEndMs": str(end)} | cycle_args
    grouped = {group: {} for group in GROUPS}
    def fetch(group, metric):
        request = base | {"groupBy": GROUPS[group], "spendType": SPEND_TYPES[metric], "includeCreditUsage": True}
        if group != "products":
            request.update({"clientType": "grok-bot", "products": ["sand"]})
        response = provider.dashboard("GetDailySpendByCategory", request)
        daily_rows(response)
        return response
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(fetch, group, metric): (group, metric) for group in GROUPS for metric in SPEND_TYPES}
        for future in concurrent.futures.as_completed(futures):
            group, metric = futures[future]
            try:
                grouped[group][metric] = future.result()
            except AuthenticationError:
                raise
            except CollectionError as error:
                grouped[group][metric] = None
                warnings.append(f"{group.capitalize()} {metric}: {error}")
    if all(grouped[group].get("usageCents") is None for group in GROUPS):
        raise CollectionError("The account could not read grouped billing data. " + warnings[-1])
    snapshot = build_snapshot(account_key, account_label, period, weekly, grouped, roster, warnings)
    if not args.skip_events:
        try:
            try:
                from attribution import normalize_events, build_routine_index
                from history import remember_ownership
                from routine_catalog import normalize_catalog
            except ImportError:
                from collector.attribution import normalize_events, build_routine_index
                from collector.history import remember_ownership
                from collector.routine_catalog import normalize_catalog
            routine_index = {}
            routines_available = False
            snapshot["automationCatalog"] = normalize_catalog(None, expected_bot_ids=list(roster))
            if roster:
                try:
                    routine_response = provider.routines(list(roster))
                    routine_index = build_routine_index(routine_response)
                    snapshot["automationCatalog"] = normalize_catalog(routine_response, expected_bot_ids=list(roster))
                    snapshot["automationCatalog"]["observedAt"] = utc_now()
                    # Raw records include automation prompts. Retain only the
                    # independent metadata allowlists in the index/catalog.
                    del routine_response
                    routines_available = True
                except AuthenticationError:
                    raise
                except CollectionError:
                    warnings.append("Current routine ownership records are unavailable; previously observed exact owners are retained.")
            current_runs = len(routine_index)
            try:
                if before_write is not None:
                    before_write()
                roster, routine_index = remember_ownership(args.output, account_key, roster, routine_index, snapshot["generatedAt"])
            except (OSError, sqlite3.Error, ValueError):
                warnings.append("Local ownership history could not be updated; only current observed run links are available.")
            routines_available |= bool(routine_index)
            snapshot["automationCoverage"] = {"currentRunLinks": current_runs, "retainedRunLinks": len(routine_index), "historicalComplete": False,
                                              "detail": "Named automation totals cover exact observed run links. The provider may retain only recent runs; local history preserves links collected while this tracker runs."}
            # A fixed past cutoff keeps new requests from shifting page counts.
            event_end = min(end, int(time.time() * 1000) - 120_000)
            raw = provider.events(start, event_end) if event_end > start else []
            normalized = normalize_events(raw, account_key)
            integrate_events(snapshot, normalized, roster, routine_index, iso_ms(event_end), routines_available)
        except AuthenticationError:
            raise
        except CollectionError as error:
            warnings.append(f"Detailed event attribution unavailable: {error}")
        except (ImportError, ValueError, TypeError, KeyError):
            warnings.append("Detailed event attribution is unavailable because its schema changed; daily billing remains available.")
    if before_write is not None:
        before_write()
    store_snapshot(snapshot, args.output)
    print(json.dumps({"ok": True, "snapshot": str(args.output), "bots": len(snapshot["bots"]), "reconciliation": snapshot["reconciliation"]["status"], "warningCount": len(warnings)}))


def export_snapshot(args):
    snapshot = json.loads(Path(args.snapshot).read_text())
    if args.format == "json":
        private_write(args.output, snapshot)
    else:
        fields = ["name", "usageCents", "includedCents", "onDemandCents", "eventValueCents", "modelCostCents", "costBasis", "tokens", "attribution", "detail"]
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in snapshot.get(args.view, []):
                record = {key: row.get(key) for key in fields}
                for key, value in record.items():
                    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                        record[key] = "'" + value
                writer.writerow(record)
    print("Export saved.")


def report_automations(args):
    try:
        from automation_report import build_report, render_text, with_inventory, ReportError
    except ImportError:
        from collector.automation_report import build_report, render_text, with_inventory, ReportError
    if args.limit < 1:
        raise CollectionError("--limit must be a positive integer.")
    try:
        snapshot = json.loads(args.snapshot.read_text())
        if args.inventory:
            snapshot = with_inventory(snapshot, json.loads(args.inventory.read_text()))
        report = build_report(snapshot, args.window, args.enabled_only)
    except ReportError as error:
        raise CollectionError(str(error)) from None
    if args.format == "json":
        if args.output:
            private_write(args.output, report)
        else:
            print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
    else:
        rendered = render_text(report, args.limit)
        if args.output:
            args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".grok-spend-", dir=args.output.parent)
            try:
                with os.fdopen(fd, "w") as stream:
                    stream.write(rendered)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, args.output)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        else:
            print(rendered, end="")
    if args.output:
        print("Automation report saved.")


def watch_account(args):
    try:
        from watch import run
    except ImportError:
        from collector.watch import run
    return run(args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect_parser = commands.add_parser("collect", help="Read the current account's billing data")
    collect_parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    collect_parser.add_argument("--auth-helper", type=Path, default=Path(os.environ.get("GROK_SPEND_AUTH_HELPER", Path(__file__).resolve().parent.parent / ".build/release/GrokSpendAuth")))
    collect_parser.add_argument("--team-id", type=int)
    collect_parser.add_argument("--no-prompt", action="store_true")
    collect_parser.add_argument("--skip-events", action="store_true")
    collect_parser.set_defaults(run=collect)
    watch_parser = commands.add_parser("watch", help="Capture new run ownership and periodically refresh billing without a UI")
    watch_parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    watch_parser.add_argument("--auth-helper", type=Path, default=Path(os.environ.get("GROK_SPEND_AUTH_HELPER", Path(__file__).resolve().parent.parent / ".build/release/GrokSpendAuth")))
    watch_parser.add_argument("--team-id", type=int)
    watch_parser.add_argument("--no-prompt", action="store_true", help="Do not allow even the initial Keychain prompt")
    watch_parser.add_argument("--ownership-interval", type=int, default=60, help="Target seconds between routine metadata captures; requests can delay a capture")
    watch_parser.add_argument("--billing-interval", type=int, default=900, help="Target seconds between complete billing collections")
    watch_parser.add_argument("--cycles", type=int, help="Stop after this many ownership cycles; omit to run until interrupted")
    watch_parser.set_defaults(run=watch_account, skip_events=False)
    export_parser = commands.add_parser("export")
    export_parser.add_argument("--snapshot", type=Path, default=DEFAULT_OUTPUT)
    export_parser.add_argument("--output", type=Path, required=True)
    export_parser.add_argument("--format", choices=("json", "csv"), required=True)
    export_parser.add_argument("--view", choices=("bots", "models", "sources", "routines"), default="bots")
    export_parser.set_defaults(run=export_snapshot)
    automation_parser = commands.add_parser("automations", help="Rank exact automation links in a saved snapshot; no network requests")
    automation_parser.add_argument("--snapshot", type=Path, default=DEFAULT_OUTPUT)
    automation_parser.add_argument("--inventory", type=Path, help="Use newer automation state from this account's watch inventory sidecar")
    automation_parser.add_argument("--window", choices=("cycle", "7d", "24h"), default="cycle")
    automation_parser.add_argument("--format", choices=("text", "json"), default="text")
    automation_parser.add_argument("--enabled-only", action="store_true", help="Rank confirmed enabled automations; excluded unknown/disabled counts remain visible")
    automation_parser.add_argument("--limit", type=int, default=20, help="Maximum text rows; JSON includes all rows in the selected ranking")
    automation_parser.add_argument("--output", type=Path, help="Write an owner-only report file instead of stdout")
    automation_parser.set_defaults(run=report_automations)
    args = parser.parse_args()
    try:
        result = args.run(args)
    except (CollectionError, OSError, ValueError, sqlite3.Error) as error:
        print(str(error) if isinstance(error, CollectionError) else "A local billing file could not be read or written.", file=sys.stderr)
        return 2 if isinstance(error, AuthenticationError) else 1
    return result if type(result) is int else 0


if __name__ == "__main__":
    raise SystemExit(main())
