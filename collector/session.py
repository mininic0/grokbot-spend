#!/usr/bin/env python3
"""Line-oriented billing worker; its existing-session token lives only in RAM."""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import stat
import struct
import sys

try:
    from . import grok_spend
except ImportError:
    import grok_spend

MAX_STORE_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024
STORE_PATH = Path.home() / "Library/Application Support/Grok Bot/sand-secrets.json"


def session_fingerprint(path=STORE_PATH):
    """Identify the active encrypted session without decrypting or exposing it."""
    invalid = "Grok Bot's session store is unavailable or has an unsupported format. Open Grok Bot and sign in again."
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or not 0 < info.st_size <= MAX_STORE_BYTES:
                raise ValueError()
            raw = stream.read(MAX_STORE_BYTES + 1)
        if not 0 < len(raw) <= MAX_STORE_BYTES:
            raise ValueError()
        store = json.loads(raw)
        if not isinstance(store, dict) or type(store.get("version")) not in (int, float) or store["version"] != 1:
            raise ValueError()
        serialized = store.get("cursor-accounts")
        if not isinstance(serialized, str):
            raise ValueError()
        accounts = json.loads(serialized)
        if not isinstance(accounts, dict) or not isinstance(accounts.get("accounts"), dict):
            raise ValueError()
        active = accounts.get("active")
        if not isinstance(active, str) or not active:
            raise ValueError()
        account = accounts["accounts"].get(active)
        if not isinstance(account, dict):
            raise ValueError()
        encoded = account.get("cursor-access-token")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError()
        cipher = base64.b64decode(encoded, validate=True)
        if not cipher.startswith(b"v10") or len(cipher) <= 3 or (len(cipher) - 3) % 16:
            raise ValueError()
        active_bytes = active.encode("utf-8")
        return hashlib.sha256(struct.pack(">Q", len(active_bytes)) + active_bytes + cipher).digest()
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise grok_spend.AuthenticationError(invalid) from None


class SessionWorker:
    def __init__(self, args, *, fingerprint=None, token_reader=None, collector=None):
        self.args = args
        self._fingerprint = fingerprint or session_fingerprint
        self._token_reader = token_reader or grok_spend.access_token
        self._collector = collector or grok_spend.collect
        self._token = None
        self._session = None

    def clear(self):
        # Python strings cannot be reliably zeroed; release every owned reference.
        self._token = None
        self._session = None

    def _detail(self, error, fallback):
        detail = str(error)
        if (not detail or len(detail) > 500 or (self._token and self._token in detail)
                or any(marker in detail.lower() for marker in ("eyj", "bearer", "authorization", "cursor-access-token", "workoscursorsessiontoken"))):
            return fallback
        return detail

    def refresh(self, allow_prompt=False):
        try:
            current = self._fingerprint()
            if current != self._session:
                self.clear()
            if self._token is None:
                token = self._token_reader(self.args.auth_helper, no_prompt=not allow_prompt)
                if self._fingerprint() != current:
                    token = None
                    raise grok_spend.AuthenticationError("Grok Bot's active sign-in changed during Keychain access. Refresh again.")
                self._token, self._session = token, current
                token = None
            # The collector validates GetMe on every refresh. Its CLI progress
            # output must never be mixed with this worker's JSON-line protocol.
            with contextlib.redirect_stdout(io.StringIO()):
                self._collector(self.args, token_override=self._token)
            return {"status": 0, "detail": None}
        except grok_spend.AuthenticationError as error:
            detail = self._detail(error, "The saved Grok Bot sign-in is unavailable. Use Connect to sign in again.")
            self.clear()
            return {"status": 2, "detail": detail}
        except grok_spend.CollectionError as error:
            return {"status": 1, "detail": self._detail(error, "Billing could not be refreshed. Retry when the service is available.")}
        except Exception:
            # No traceback or arbitrary exception text can reveal account data.
            return {"status": 1, "detail": "Billing could not be refreshed. Check the local billing files and retry."}

    def command(self, request):
        if not isinstance(request, dict) or not isinstance(request.get("command"), str):
            return {"status": 1, "detail": "Invalid session command."}, False
        command = request["command"]
        if command == "refresh":
            if set(request) - {"command", "allowPrompt"} or type(request.get("allowPrompt", False)) is not bool:
                return {"status": 1, "detail": "Refresh requires a boolean allowPrompt value."}, False
            return self.refresh(request.get("allowPrompt", False)), False
        if command in ("disconnect", "quit") and set(request) == {"command"}:
            self.clear()
            return {"status": 0, "detail": None}, command == "quit"
        return {"status": 1, "detail": "Invalid session command."}, False


def serve(worker, source, destination):
    """Consume bounded binary JSON lines; EOF, quit and broken pipes drop RAM auth."""
    def emit(response):
        destination.write(json.dumps(response, allow_nan=False) + "\n")
        destination.flush()

    try:
        while True:
            line = source.readline(MAX_REQUEST_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_REQUEST_BYTES:
                while line and not line.endswith(b"\n"):
                    line = source.readline(MAX_REQUEST_BYTES + 1)
                emit({"status": 1, "detail": "Session command exceeds the 64 KiB limit."})
                continue
            try:
                request = json.loads(line.decode("utf-8"))
                response, stop = worker.command(request)
            except (ValueError, UnicodeError, RecursionError):
                response, stop = {"status": 1, "detail": "Invalid JSON session command."}, False
            emit(response)
            if stop:
                break
    except (BrokenPipeError, OSError):
        pass
    finally:
        worker.clear()


def install_shutdown(worker):
    """Own a process group so cancelling the worker also cancels its auth helper."""
    group = os.getpid()
    if os.getpgrp() != group:
        os.setpgid(0, 0)
    if os.getpgrp() != group:
        raise OSError("Could not isolate the session worker.")

    def shutdown(_signum, _frame):
        worker.clear()
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        # Only this worker and children it launched belong to this new group.
        os.killpg(group, signal.SIGTERM)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=grok_spend.DEFAULT_OUTPUT)
    parser.add_argument("--auth-helper", type=Path, required=True)
    parser.add_argument("--team-id", type=int)
    args = parser.parse_args()
    args.no_prompt = True
    args.skip_events = False
    worker = SessionWorker(args)
    try:
        install_shutdown(worker)
    except OSError:
        return 1  # Never start credential access without cancellable children.
    serve(worker, sys.stdin.buffer, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
