#!/bin/bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
test_dir="$(mktemp -d)"
trap 'rm -rf "$test_dir"' EXIT
cat > "$test_dir/worker.py" <<'PY'
import argparse
import json
import os
import sys
import time
parser = argparse.ArgumentParser()
parser.add_argument('--output')
parser.add_argument('--auth-helper')
mode = parser.parse_args().output
for line in sys.stdin:
    request = json.loads(line)
    if mode == 'wait':
        time.sleep(10)
    elif mode == 'invalid':
        print('invalid', flush=True)
    elif mode == 'oversized':
        print('x' * 9000, flush=True)
    elif mode == 'auth':
        print(json.dumps({'status': 2, 'detail': 'Synthetic auth unavailable'}), flush=True)
    else:
        print(json.dumps({'status': 0, 'detail': f"{os.getpid()}:{request['allowPrompt']}"}), flush=True)
PY
cat > "$test_dir/Check.swift" <<'SWIFT'
import Foundation

@main
struct Check {
    static func main() async throws {
        let script = CommandLine.arguments[1]
        let session = try CollectorSession(script: script, helper: "/synthetic-unused", snapshot: "normal")
        let first = session.refresh(allowPrompt: true, timeout: 3)
        let second = session.refresh(allowPrompt: false, timeout: 3)
        precondition(first.status == 0 && second.status == 0)
        precondition(first.detail!.split(separator: ":")[0] == second.detail!.split(separator: ":")[0])
        precondition(first.detail!.hasSuffix(":True") && second.detail!.hasSuffix(":False"))
        session.stop()
        precondition(!session.isRunning)
        print("PASS: same worker serves explicit and silent refresh; disconnect stops session")
        for mode in ["auth", "invalid", "oversized", "wait"] {
            let worker = try CollectorSession(script: script, helper: "/synthetic-unused", snapshot: mode)
            let result = worker.refresh(allowPrompt: false, timeout: mode == "wait" ? 0.2 : 3)
            precondition(result.status == (mode == "auth" ? 2 : mode == "wait" ? 124 : 1))
            if mode != "auth" { precondition(!worker.isRunning) }
            worker.stop()
        }
        print("PASS: auth status, malformed/oversized replies, timeout and session cleanup")
        let waiting = try CollectorSession(script: script, helper: "/synthetic-unused", snapshot: "wait")
        let result = Task.detached { waiting.refresh(allowPrompt: true, timeout: 5) }
        try await Task.sleep(for: .milliseconds(150))
        let start = Date()
        waiting.stop()
        let stopped = await result.value
        precondition(stopped.status == 1 && Date().timeIntervalSince(start) < 2)
        print("PASS: disconnect interrupts an in-flight reply without blocking the UI")
    }
}
SWIFT
swiftc -swift-version 6 "$project_root/Sources/GrokSpend/CollectorSession.swift" "$test_dir/Check.swift" -o "$test_dir/check"
"$test_dir/check" "$test_dir/worker.py"
