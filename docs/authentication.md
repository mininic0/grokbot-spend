# Existing-session authentication

`GrokSpendAuth` reads the active Grok Bot session without loading Grok Bot's
JavaScript, patching the application, or changing its settings or credentials.
It performs no network requests and never renews access tokens.

On the observed Grok Bot 0.70.2 macOS installation:

- `~/Library/Application Support/Grok Bot/sand-secrets.json` has version `1`.
- Its `cursor-accounts` string contains JSON with an `active` account key and an
  `accounts` dictionary. Only the active slot's `cursor-access-token` is read.
- That token is base64-encoded Electron/Chromium `v10` ciphertext.
- The existing Keychain generic-password item has service `Grok Bot Safe Storage`
  and account `Grok Bot Key`.

The helper asks Security.framework for that one existing item. It does not
enumerate unrelated secrets, add Keychain items, change their access rules,
access refresh tokens, or copy the session store. macOS may ask for access using
its normal prompt. `--no-prompt` prevents interactive access and reports failure
if permission is unavailable. For the legacy login-Keychain backend, the helper
also disables optional Keychain UI for its own short-lived process; this does
not change the key's permissions or any persistent system setting. The modern
`LAContext.interactionNotAllowed` option alone did not suppress authorization
waiting for this item during validation.

The observed `v10` format uses PBKDF2-HMAC-SHA1 with salt `saltysalt`, 1003 rounds,
a 128-bit key, and AES-CBC with PKCS7 padding and an IV of sixteen spaces. This
compatibility code is independently implemented against the
[Chromium format](https://github.com/chromium/chromium/blob/140.0.7339.0/components/os_crypt/sync/os_crypt_mac.mm).
The helper rejects unfamiliar formats rather than falling back to plaintext or
trying unrelated Keychain entries.

## Interface

`GrokSpendAuth check [--no-prompt]` prints safe JSON status. `expiresAt`, when
present, is an unverified JWT expiry hint. This does not authenticate the session
to the provider; the collector must validate it with its ordinary read-only API
request.

`GrokSpendAuth access-token [--no-prompt]` returns only the access token and a
newline on stdout. Stdout must be a pipe or socket; terminals and regular files
are refused. The collector must capture that pipe in memory and must never log
or persist it. Failures print a fixed redacted message to stderr. The helper
times out after 180 seconds with prompts enabled, or 10 seconds in noninteractive
mode; callers should allow a little extra time for the helper to exit and also
bound their subprocess wait.

`GrokSpendAuth self-test` checks synthetic PBKDF2/AES interoperability fixtures,
active-account selection, signed-out behavior, unsupported-format rejection and
JWT expiry decoding without accessing a real session or Keychain.

| Exit | Meaning |
| --- | --- |
| 0 | Success |
| 64 | Invalid command |
| 65 | Unsafe stdout destination |
| 66 | Missing session or active account |
| 67 | Invalid/unsupported data or decryption failure |
| 68 | Keychain access unavailable |
| 69 | Saved token expired |
| 70 | Keychain access timed out |

If the stored token expires, open Grok Bot and let it maintain its own session,
then retry. Future app changes may require updating this independently observed
adapter. There is no claim that this is a public, stable provider API.

## Native app session lifetime

The native app runs `collector/session.py` as a private child with stdin/stdout
pipes. After one authorized helper read, that worker retains the access token
only in RAM. Subsequent refreshes reuse it and validate it with the provider.
It does not retain the Keychain password or the derived decryption key.

Before each refresh the worker safely reads the active encrypted session slot
and compares its fingerprint. An account/token change invalidates the cache;
a change while the Keychain prompt is open rejects the read. Authentication
errors also invalidate the cache. Network errors preserve it for another try.
No fingerprint or credential is included in the JSON-line control protocol.

Disconnect, app termination, pipe closure, or process cancellation releases the
worker's credential references. Python strings cannot be reliably zeroed; the
process lifetime is the boundary, and credentials are never deliberately written
to disk. Normal operating-system memory management still applies.

This session mechanism matters for local ad-hoc builds: a user approving an
access prompt does not guarantee persistent helper trust in Grok Bot's existing
file-based Keychain ACL. The app does not alter that ACL, relax signing or
partition checks, or try unrelated secrets. A new app session can require a new
normal macOS prompt. Background refreshes never request a prompt.
