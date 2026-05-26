#!/usr/bin/env bash
set -euo pipefail

ROOT="$HOME/telegram-codex-bridge"
PLIST="$HOME/Library/LaunchAgents/ai.codex.telegram-bridge.plist"

mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>ai.codex.telegram-bridge</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$ROOT/telegram_codex_bridge.py</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$ROOT</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/telegram-codex-bridge.out.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/telegram-codex-bridge.err.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
launchctl start ai.codex.telegram-bridge
echo "installed: $PLIST"
