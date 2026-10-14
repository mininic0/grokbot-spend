#!/bin/bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"

# Build only the command-line credential helper. This leaves the optional app
# and any previously approved helper in dist/ untouched.
swift build --configuration release --product GrokSpendAuth
printf 'Built %s/.build/release/GrokSpendAuth\n' "$project_root"
