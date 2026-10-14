"""Account-scoped ownership observations retained beyond the provider's run list."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3

try:
    from .attribution import merge_routine_indexes
except ImportError:
    from attribution import merge_routine_indexes


def remember_ownership(output, account_key, roster, current_index, observed_at):
    """Retain exact observed identities, never infer an owner from activity times.

    An empty current index cannot erase earlier observations. Account and team
    are encoded in the caller's validated account_key. Conflicting observations
    are preserved as ambiguous by merge_routine_indexes.
    """
    if not isinstance(account_key, str) or not account_key:
        raise ValueError("Ownership history requires a validated account key.")
    database = Path(output).parent / "ledger.sqlite3"
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(database, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(fd, 0o600)
    os.close(fd)
    with sqlite3.connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS ownership_bots(account TEXT, bot_id TEXT, name TEXT, observed_at TEXT, PRIMARY KEY(account,bot_id))")
        db.execute("CREATE TABLE IF NOT EXISTS ownership_runs(account TEXT, conversation_id TEXT, data TEXT, first_seen TEXT, last_seen TEXT, PRIMARY KEY(account,conversation_id))")
        previous_roster = dict(db.execute("SELECT bot_id,name FROM ownership_bots WHERE account=?", (account_key,)))
        previous_index = {key: json.loads(data) for key, data in db.execute("SELECT conversation_id,data FROM ownership_runs WHERE account=?", (account_key,))}
        recovered_roster = {}
        # Upgrade existing installations using only previously proven event
        # ownership. Never read another account's events or conversation text.
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not previous_index and "events" in tables:
            recovered = {}
            seen = set()
            for (data,) in db.execute("SELECT data FROM events WHERE account=?", (account_key,)):
                event = json.loads(data)
                match = event.get("attribution") or {}
                conversation = event.get("conversationId")
                if (event.get("accountKey") != account_key
                        or match.get("method") != "routine-run-id"
                        or not isinstance(conversation, str)
                        or not match.get("botId")):
                    continue
                candidate = {"botId": match["botId"], "routineId": match.get("routineId"), "name": match.get("routineName")}
                identity = (conversation, candidate["botId"], candidate["routineId"], candidate["name"])
                if identity in seen:
                    continue
                seen.add(identity)
                recovered = merge_routine_indexes(recovered, {conversation: candidate})
            previous_index = recovered
            for entry in recovered.values():
                owners = entry["candidates"] if entry.get("ambiguous") else [entry]
                for owner in owners:
                    # The old account-validated attribution proves this bot's
                    # identity even when its display name/roster row is gone.
                    recovered_roster.setdefault(owner["botId"], previous_roster.get(owner["botId"], owner["botId"]))
        valid_roster = {key: name for key, name in roster.items()
                        if isinstance(key, str) and key and isinstance(name, str) and name}
        observed_roster = recovered_roster | valid_roster
        merged_roster = previous_roster | observed_roster
        merged_index = merge_routine_indexes(previous_index, current_index)
        for bot_id, name in observed_roster.items():
            db.execute("INSERT INTO ownership_bots VALUES(?,?,?,?) ON CONFLICT(account,bot_id) DO UPDATE SET name=excluded.name,observed_at=excluded.observed_at", (account_key, bot_id, name, observed_at))
        for conversation, candidate in merged_index.items():
            db.execute("INSERT INTO ownership_runs VALUES(?,?,?,?,?) ON CONFLICT(account,conversation_id) DO UPDATE SET data=excluded.data,last_seen=CASE WHEN ? THEN excluded.last_seen ELSE ownership_runs.last_seen END", (account_key, conversation, json.dumps(candidate, allow_nan=False), observed_at, observed_at, conversation in current_index))
    return merged_roster, merged_index
