# Grok Spend

A local, read-only collector that explains Grok Bot usage by bot, model, source, and automation. It provides a command-line automation audit, JSON/CSV exports, and an optional macOS companion. Independent and unaffiliated with Grok, xAI, or Cursor.

**Experimental:** the adapter uses observed private interfaces from Grok Bot 0.70.2. Provider changes may require an update. Missing data stays visible rather than becoming a fabricated zero.

## What the numbers mean

- **Reported usage:** amounts from the provider's daily billing endpoint, when present. Some accounts return token counts while omitting every dollar amount. Those amounts display as unavailable.
- **Recorded usage value:** the sum of the detailed event feed's `chargedCents` field. Despite its name, this field can contain included-plan consumption. It is **not an invoice or proof of an extra cash charge**.
- **Model usage value:** `tokenUsage.totalCents`, kept separate from recorded value. Missing values stay missing.
- **On-demand:** the separately requested daily on-demand amount, never inferred from an event's model value or `chargedCents`.

Bot, model, and routine views overlap. Routine values are part of their parent bot's value; do not add the views together. Bot attribution uses exact account-local IDs and documented routine run IDs. Historical child runs without a proven parent appear as **Unassigned child runs**. A named bot's event value may therefore be incomplete even when its matched events all have amounts.

Detailed events use a fixed cutoff two minutes before collection to stabilize pagination. Daily figures can update on a different schedule. Partial token-bucket sums are labeled and carry missing-field counts in JSON. No model prices are guessed from token counts or model aliases.

## Finding expensive automations

Run `python3 collector/grok_spend.py automations --window 24h` to rank named automations by recorded usage value, with their bot owner, value per matched run, matched runs, and last activity. The catalog supplies the latest observed enabled/disabled state; missing state remains unknown. `--enabled-only` narrows the ranking while keeping excluded state counts and total attribution coverage visible. Compare the billing period, last seven days, or last 24 hours. Recent windows end at the event cutoff, and a run spanning a window contributes only events inside that window.

These are **observed subsets**, not complete historical automation totals or guaranteed savings from disabling one. The provider's routine records can contain only recent runs. Unassigned child usage and bot activity without a proven automation stay separate. Use the exact automation name and owner to find its controls in Grok Bot.

Grok Spend retains previously observed run-to-automation links in its account-scoped local ledger, so a newer rolling run list cannot erase older ownership evidence. Run the collector to capture future links; it cannot reconstruct a missing historical relationship from timing alone. Conflicting owners remain ambiguous.

## Command-line setup

Requires macOS 14+, Swift 6/Xcode Command Line Tools, and Python 3.10+ available as `python3`. Python uses its standard library; the Swift app has no external package dependencies.

Open Grok Bot and sign in normally, then build only the credential helper:

```sh
./scripts/build-auth-helper.sh
python3 collector/grok_spend.py watch
```

This runs in the foreground without opening a dashboard. It captures routine metadata roughly every minute and refreshes billing roughly every 15 minutes. The loop is sequential: network requests and a full billing collection can delay the next ownership capture. After a long billing read it immediately captures ownership again. Short-lived runs can still be missed if the provider drops them between successful reads.

The first credential read may show the normal macOS Keychain prompt. The watcher keeps its token only in RAM and reads it once. It stops when the sign-in changes, expires, or is rejected; open Grok Bot to renew its own session, then restart the watcher. It does not refresh credentials or request another prompt in the same run. `Ctrl+C` stops the process and releases its credential references. No login item, launch agent, or scheduled task is installed.

`--ownership-interval` and `--billing-interval` set target intervals in seconds. `--cycles 2` runs a bounded check. `--no-prompt` also forbids the initial Keychain prompt.

The default snapshot and ledger are saved in `~/Library/Application Support/Grok Spend/`. Each metadata capture also saves `snapshot-inventory.json` beside `snapshot.json`; a custom `--output audit.json` produces `audit-inventory.json`. In a second terminal, read billing with the newer inventory state:

```sh
python3 collector/grok_spend.py automations --window 24h \
  --inventory "$HOME/Library/Application Support/Grok Spend/snapshot-inventory.json"
```

The report checks that inventory and billing belong to the same account and refuses older inventory. Inventory updates change observed enabled/disabled state; they do not make billing events newer than the saved cutoff.

## Optional macOS companion

```sh
./scripts/build-app.sh
open "dist/Grok Spend.app"
```

Open Grok Bot and sign in normally, then use **Refresh** in Grok Spend. macOS may ask whether `GrokSpendAuth` can read the existing `Grok Bot Safe Storage` item. The app cannot approve this for you.

The native app keeps the authorized access token only in a private child process's memory while connected, and refreshes every 15 minutes after a successful manual refresh. Quitting the app or selecting **Disconnect session** clears that process. Closing the dashboard window keeps the menu-bar app and its automatic updates running. A changed Grok Bot account/session invalidates the cached token. Expired or denied authentication pauses automatic updates until a manual refresh succeeds. Existing Keychain approval may not persist for locally ad-hoc-signed helpers; the in-memory session avoids repeated reads during one app session. Choose either the foreground watcher or the companion for ongoing collection to avoid redundant reads.

The local build is ad-hoc signed, not Developer ID signed or notarized. Python is not bundled.

To explore with synthetic data and no account access:

```sh
open -n "dist/Grok Spend.app" --args --demo
```

`--snapshot /absolute/path/snapshot.json` opens an offline file preview. Refresh in demo/preview mode rereads that file and makes no network requests.

## Local data and privacy

Snapshots and an account-scoped SQLite ledger live in `~/Library/Application Support/Grok Spend/`, with owner-only files. They contain billing metadata: timestamps, model names, token counts, amounts, bot/routine names, and conversation identifiers. Exports contain this private metadata too; review them before sharing.

No access tokens, cookies, Keychain passwords, or conversation text are written to snapshots, exports, or Git. The credential helper passes its result through a pipe and refuses terminal/file output. Credentials go only to the provider's observed hosts over verified TLS; redirects are rejected. There is no telemetry or tracker backend.

The collector does not send bot messages, start routines, change billing settings or models, modify Grok Bot, or refresh its credentials. When the session expires, open Grok Bot to let it maintain its own sign-in. See [authentication details](docs/authentication.md).

## CLI

```sh
python3 collector/grok_spend.py collect \
  --auth-helper ".build/release/GrokSpendAuth"
python3 collector/grok_spend.py export --format csv --view bots --output bots.csv
python3 collector/grok_spend.py automations --window 24h
python3 collector/grok_spend.py automations --window 7d --limit 50
python3 collector/grok_spend.py automations --window cycle --format json --output automations.json
```

`automations` reads the existing snapshot without account access. It ranks exact named automation links by recorded usage value, with owner, matched run count, value per matched run, last activity, and enabled/disabled/unknown state when inventory is available. State is observed at inventory collection time (snapshot time for older files), not at the historical event time. `--enabled-only` ranks confirmed enabled automations and reports how many disabled or unknown-state rows were excluded; coverage still includes all states.

The report also shows the value linked to routines, named bot activity without a proven routine, unassigned child runs, and unknown ownership. Use `--snapshot PATH` for another saved snapshot. Recent windows end at the saved event cutoff, include both bounds, and are clamped to the collected billing period. JSON includes all ranked automations; `--limit` controls text rows only.

Missing amounts remain unavailable, with reported subtotals and missing-event counts shown. Reports are observed subsets, not complete historical totals, invoices, or guaranteed savings from disabling an automation. Output files are owner-only and contain private billing metadata.

Use `--no-prompt` for a one-shot collection that must not show a Keychain prompt. `--skip-events` reads daily aggregates only; it cannot explain individual event values. `--team-id INTEGER` explicitly scopes a team account. The native app defaults to the personal account.

All pagination must finish before event totals are accepted. If the service changes page counts, repeats pages, or exceeds time/size limits, the app reports the event feed as unavailable. The latest successful snapshot is preserved when collection itself fails.

## Development

```sh
python3 -m unittest discover -s tests -v
./scripts/check-ui-contract.sh
./scripts/check-session-transport.sh
swift build -c release
.build/release/GrokSpendAuth self-test
```

Use synthetic fixtures for tests. Do not commit live billing responses, credentials, screenshots of real accounts, or extracted proprietary source. `private/`, `.build/`, and `dist/` are ignored.

## Components

- `collector/`: Python collector, foreground watcher, normalization, local ledger, CLI, and in-memory session worker.
- `Sources/GrokSpendAuth/`: native read-only access to the user's existing Grok Bot session.
- `Sources/GrokSpend/`: native menu-bar companion and detailed dashboard.
- `scripts/`: build and packaging commands.
- `docs/`: independently documented protocol observations and validation results.

## Reverse-engineering approach

We inspected the installed Electron bundle's generated schemas and request construction, then validated behavior using authenticated read-only responses. Static presence of a field does not prove the backend populates it.

[REA](https://github.com/morluto/rea) 6.4.0 was actually run against the installed ASAR and isolated billing functions. The whole-app pass read 616 relevant files and parsed 600 JavaScript files, with partial relationship coverage under its resource limits. Focused analysis recovered the billing call/property references, while semantic tracing still reported unknown external behavior. REA results are static evidence, not proof of live billing semantics. Raw graphs and extracted code remain local and are not distributed.

The collector independently implements the observed protocol. Public reference research included [Agent HUD's Grok adapter](https://github.com/jazzenchen/agent-hud-open) and [Cursor's Admin API documentation](https://prod.cursor.com/docs/account/teams/admin-api); team-admin semantics are not assumed to apply unchanged to personal endpoints.

## License

MIT for this project's original code. Grok Bot and all referenced projects retain their own licenses and trademarks.
