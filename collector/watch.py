"""Foreground ownership capture with one in-memory, pinned sign-in.

Ownership reads precede less frequent full billing collection. The loop is
deliberately sequential: a slow billing read can delay a capture. If billing
crosses the ownership deadline, another capture runs before the next wait.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import sys
import time

try:
    from . import grok_spend, session
    from .attribution import build_routine_index
    from .history import remember_ownership
    from .routine_catalog import normalize_catalog
except ImportError:
    import grok_spend
    import session
    from attribution import build_routine_index
    from history import remember_ownership
    from routine_catalog import normalize_catalog


def inventory_path(output):
    output = Path(output)
    return output.with_name(output.stem + "-inventory.json")


class WatchRunner:
    """Testable synchronous loop; only SessionWorker owns the cached token.

    Args match collect plus ownership_interval, billing_interval, and cycles.
    cycles=None runs until cancellation or authentication invalidation. A cycle
    is one scheduled ownership capture and any billing work due with it.
    """

    def __init__(self, args, *, fingerprint=None, token_reader=None,
                 provider_factory=None, roster_reader=None, billing_collector=None,
                 monotonic=None, wall_time=None, sleep=None, destination=None):
        self.args = copy.copy(args)
        self.args.skip_events = False
        self._fingerprint = fingerprint or session.session_fingerprint
        self._token_reader = token_reader or grok_spend.access_token
        self._provider_factory = provider_factory or grok_spend.Provider
        self._roster_reader = roster_reader or grok_spend.read_roster
        self._billing_collector = billing_collector or grok_spend.collect
        self._monotonic = monotonic or time.monotonic
        self._wall_time = wall_time or time.time
        self._sleep = sleep or time.sleep
        self._destination = destination if destination is not None else sys.stdout
        self._team_id = self.args.team_id
        self._session_pin = None
        self._account_pin = None
        self._expires_at = None
        self._helper_used = False
        self._phase = None
        self._metadata = {}
        self.worker = session.SessionWorker(
            self.args, fingerprint=self._pinned_fingerprint,
            token_reader=self._read_once, collector=self._capture,
        )

    def clear(self):
        self.worker.clear()

    def _pinned_fingerprint(self):
        current = self._fingerprint()
        if self._session_pin is None:
            self._session_pin = current
        elif current != self._session_pin:
            raise grok_spend.AuthenticationError("The Grok Bot sign-in changed; ownership capture stopped.")
        return current

    def _read_once(self, helper, no_prompt=False):
        if self._helper_used:
            raise grok_spend.AuthenticationError("This capture session cannot read credentials again; restart it after signing in.")
        self._helper_used = True
        return self._token_reader(helper, no_prompt=no_prompt)

    def _check_session(self):
        self._pinned_fingerprint()
        if self.args.team_id != self._team_id:
            raise grok_spend.AuthenticationError("The account scope changed; ownership capture stopped.")
        if self._expires_at is not None and self._expires_at <= self._wall_time():
            raise grok_spend.AuthenticationError("The saved sign-in expired; ownership capture stopped.")

    def _validated_provider(self, token):
        self._check_session()
        provider = self._provider_factory(token, self._team_id)
        subject = provider.claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise grok_spend.AuthenticationError("The saved sign-in cannot establish an account scope.")
        raw_expiry = provider.claims.get("exp")
        expiry = grok_spend.number(raw_expiry)
        if raw_expiry is not None and expiry is None:
            raise grok_spend.AuthenticationError("The saved sign-in has an unsupported expiry.")
        self._expires_at = expiry
        self._check_session()
        me = provider.dashboard("GetMe")
        self._check_session()
        user_id, auth_id = me.get("userId"), me.get("authId")
        if type(user_id) is not int or (auth_id is not None and not isinstance(auth_id, str)):
            raise grok_spend.CollectionError("The provider did not establish the account identity.")
        identity = (subject, user_id, auth_id, self._team_id)
        if self._account_pin is None:
            self._account_pin = identity
        elif self._account_pin != identity:
            raise grok_spend.AuthenticationError("The validated account changed; ownership capture stopped.")
        account_key = hashlib.sha256((subject + ":" + str(self._team_id)).encode()).hexdigest()[:24]
        return provider, me, account_key

    def _capture(self, args, token_override=None):
        provider, me, account_key = self._validated_provider(token_override)
        if self._phase == "billing":
            # collect invokes this guard immediately before ownership/snapshot
            # writes, so a local account switch during a read cannot save data.
            self._billing_collector(args, token_override=token_override, before_write=self._check_session)
            self._check_session()
            return
        roster = self._roster_reader([provider.claims["sub"], me.get("authId")])
        if not roster:
            raise grok_spend.CollectionError("The account's local bot roster is unavailable.")
        response = provider.routines(list(roster))
        index = build_routine_index(response)
        catalog = normalize_catalog(response, expected_bot_ids=list(roster))
        del response  # Raw records can contain prompts; never write or log them.
        observed_at = grok_spend.utc_now()
        catalog["observedAt"] = observed_at
        self._check_session()
        _, retained = remember_ownership(args.output, account_key, roster, index, observed_at)
        self._check_session()
        grok_spend.private_write(inventory_path(args.output), {
            "accountKey": account_key, "observedAt": observed_at, "catalog": catalog,
            "currentRunLinks": len(index), "retainedRunLinks": len(retained),
        })
        self._metadata = {
            "botCount": len(roster), "automationCount": len(catalog["automations"]),
            "currentRunLinks": len(index), "retainedRunLinks": len(retained),
            "catalogCompleteness": catalog["completeness"],
            "unavailableBotCount": sum(group["completeness"] != "complete" for group in catalog["groups"]),
        }
        if catalog["completeness"] != "complete":
            # Available observations are still valuable and have been saved.
            # Unknown inventories must not be reported as successful empty ones.
            raise grok_spend.CollectionError("Ownership inventory is incomplete.")

    def _refresh(self, phase):
        self._phase, self._metadata = phase, {}
        response = self.worker.refresh(allow_prompt=not self._helper_used and not self.args.no_prompt)
        status = response["status"]
        # Do not forward arbitrary exceptions, paths, names, IDs, or worker
        # details. Status output is suitable for a terminal or redirected log.
        messages = {
            0: "Ownership captured." if phase == "ownership" else "Billing collection completed.",
            1: "Ownership capture unavailable; retained links preserved." if phase == "ownership" else "Billing collection unavailable; retained links preserved.",
            2: "Sign-in unavailable or changed; capture stopped. Open Grok Bot and restart the watcher.",
        }
        result = {"phase": phase, "status": status, "detail": messages[status]}
        if status == 0 or status == 1 and self._metadata:
            result.update(self._metadata)
        if status == 1 and self._metadata.get("catalogCompleteness") == "unknown":
            result["detail"] = "Ownership capture incomplete; retained links preserved."
        self._destination.write(json.dumps(result, allow_nan=False) + "\n")
        self._destination.flush()
        return status

    def run(self):
        ownership = getattr(self.args, "ownership_interval", 60)
        billing = getattr(self.args, "billing_interval", 900)
        cycles = getattr(self.args, "cycles", None)
        if (any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in (ownership, billing))
                or cycles is not None and (type(cycles) is not int or cycles < 1)):
            self.clear()
            raise ValueError("Capture intervals must be positive and cycles must be a positive integer.")
        completed = 0
        # A healthy ownership tick cannot recover an unsuccessful billing
        # collection. Each phase must recover through its own later success.
        phase_status = {"ownership": 0, "billing": 0}
        try:
            next_billing = self._monotonic()
            while cycles is None or completed < cycles:
                status = self._refresh("ownership")
                if status == 2:
                    return 2
                phase_status["ownership"] = status
                next_ownership = self._monotonic() + ownership
                if self._monotonic() >= next_billing:
                    status = self._refresh("billing")
                    if status == 2:
                        return 2
                    phase_status["billing"] = status
                    next_billing = self._monotonic() + billing
                    if self._monotonic() >= next_ownership:
                        status = self._refresh("ownership")
                        if status == 2:
                            return 2
                        phase_status["ownership"] = status
                        next_ownership = self._monotonic() + ownership
                completed += 1
                if cycles is None or completed < cycles:
                    self._sleep(max(0, next_ownership - self._monotonic()))
            return max(phase_status.values())
        except KeyboardInterrupt:
            return 130
        except (BrokenPipeError, OSError):
            return 1
        except Exception:
            # A failed clock/sleep or output adapter must not print arbitrary
            # exception text or a traceback containing private local state.
            return 1
        finally:
            self.clear()


def run(args):
    """CLI entry: isolate helper children for cancellation, then run in foreground."""
    runner = WatchRunner(args)
    try:
        session.install_shutdown(runner)
    except OSError:
        runner.clear()
        print("Capture could not establish cancellable credential access.", file=sys.stderr)
        return 1
    return runner.run()
