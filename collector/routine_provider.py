"""Read the account's routine metadata, honoring the provider's fallback flag."""
from __future__ import annotations

BASE_URL = "https://api2.cursor.sh/aiserver.v1.GrokBotService/"
MAX_BATCH = 200  # Same account-list batch size used by Grok Bot 0.70.2.


class RoutineSchemaError(ValueError):
    """A routine response cannot be safely matched to the requested account bots."""


def _identity(value):
    if not isinstance(value, str) or not value.strip():
        raise RoutineSchemaError("Routine metadata has an invalid bot identity.")
    return value.strip().lower()


def _automations(response):
    if not isinstance(response, dict):
        raise RoutineSchemaError("Routine metadata has an unsupported response shape.")
    rows = response.get("automations", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RoutineSchemaError("Routine metadata has an invalid automation list.")
    # recordJson is intentionally left opaque: it can contain routine prompts.
    # Callers retain only whitelisted identifiers and billing metadata in RAM.
    for row in rows:
        if not isinstance(row.get("recordJson", ""), str) or not isinstance(row.get("automationId", ""), str):
            raise RoutineSchemaError("Routine metadata has an invalid automation record.")
    return rows


def read_account_automations(request, agent_ids, *, time_zone=None):
    """Use only the two observed read-only routine-list RPCs.

    `request` has Provider._request's (url, payload) interface. Its exceptions
    propagate unchanged, so callers preserve authentication versus network
    failures. Returned records are not written or logged by this helper.
    """
    if not isinstance(agent_ids, (list, tuple)):
        raise RoutineSchemaError("Routine reads require an explicit bot list.")
    requested = {}
    for agent_id in agent_ids:
        requested.setdefault(_identity(agent_id), agent_id)
    if time_zone is not None and (not isinstance(time_zone, str) or not time_zone):
        raise RoutineSchemaError("Routine time zone is invalid.")
    options = {"timeZone": time_zone} if time_zone is not None else {}
    ids = list(requested.values())
    result = []
    for offset in range(0, len(ids), MAX_BATCH):
        batch = ids[offset:offset + MAX_BATCH]
        expected = {_identity(agent_id): agent_id for agent_id in batch}
        response = request(BASE_URL + "ListGrokBotAccountAutomations", {"agentIds": batch, **options})
        groups = response.get("agents", []) if isinstance(response, dict) else None
        if not isinstance(groups, list) or len(groups) != len(expected):
            raise RoutineSchemaError("Routine metadata did not cover the requested bots.")
        indexed = {}
        for group in groups:
            if not isinstance(group, dict):
                raise RoutineSchemaError("Routine metadata has an invalid bot group.")
            identity = _identity(group.get("agentId"))
            if identity not in expected or identity in indexed:
                raise RoutineSchemaError("Routine metadata returned an unexpected or duplicate bot.")
            rows = _automations(group)
            unavailable = group.get("unavailable", False)
            individually = group.get("readIndividually", False)
            if type(unavailable) is not bool or type(individually) is not bool or (unavailable and individually) or ((unavailable or individually) and rows):
                raise RoutineSchemaError("Routine metadata has conflicting availability flags.")
            indexed[identity] = (rows, unavailable, individually)
        for agent_id in batch:
            rows, unavailable, individually = indexed[_identity(agent_id)]
            if individually:
                response = request(BASE_URL + "ListGrokBotAgentAutomations", {"agentId": agent_id, **options})
                rows = _automations(response)
            group = {"agentId": agent_id, "automations": rows}
            if unavailable:
                group["unavailable"] = True
            result.append(group)
    return {"agents": result}
