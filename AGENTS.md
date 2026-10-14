# Grok Spend project instructions

Build an independent, local macOS spend tracker. Read README.md before changes.

- Never alter or patch the installed Grok Bot app, its configuration, models, routines, or credentials.
- Limit authenticated network operations to read-only usage/account metadata methods on observed provider hosts.
- No tokens, cookies, authorization headers, conversation text, or private billing fixtures in logs or Git.
- An absent or failed metric is unavailable, never a fabricated zero.
- Separate provider-reported usage cost, included usage, credits, and on-demand amounts. Do not label a value as an invoice charge without evidence.
- Grouped views overlap; never add bot, model, and source totals together.
- Attribute by exact account-scoped identifiers, not timing guesses. Preserve ambiguous and unknown records.
- Use synthetic fixtures for billing normalization, pagination/deduplication, and reconciliation tests.
- Keep UI states honest: last successful refresh and errors must be visible; demo data must be labeled.
