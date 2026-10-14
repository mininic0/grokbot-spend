#!/bin/bash
set -euo pipefail

project_root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$project_root"
swift build -c release
binary_dir="$(swift build -c release --show-bin-path)"
app_dir="$project_root/dist/Grok Spend.app"
mkdir -p "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources/collector"

cp "$binary_dir/GrokSpend" "$app_dir/Contents/MacOS/GrokSpend"
cp "$binary_dir/GrokSpendAuth" "$app_dir/Contents/MacOS/GrokSpendAuth"
cp "$project_root/assets/demo-snapshot.json" "$app_dir/Contents/Resources/demo-snapshot.json"
# Package source only: never copy bytecode, test fixtures, runtime snapshots, or
# local credentials into the app.
find "$project_root/collector" -maxdepth 1 -type f -name '*.py' -exec cp '{}' "$app_dir/Contents/Resources/collector/" \;
test -f "$app_dir/Contents/Resources/collector/grok_spend.py"

cat > "$app_dir/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>CFBundleName</key><string>Grok Spend</string>
    <key>CFBundleDisplayName</key><string>Grok Spend</string>
    <key>CFBundleIdentifier</key><string>app.grokspend.local</string>
    <key>CFBundleExecutable</key><string>GrokSpend</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>14.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSPrincipalClass</key><string>NSApplication</string>
</dict></plist>
PLIST

codesign --force --sign - "$app_dir/Contents/MacOS/GrokSpendAuth"
codesign --force --sign - "$app_dir"
printf 'Built %s\n' "$app_dir"
printf 'Demo: open -n "%s" --args --demo\n' "$app_dir"
