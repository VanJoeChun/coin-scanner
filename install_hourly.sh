#!/bin/bash
# 1시간마다(매시 1분) 스캔 → A등급 후보가 있으면 텔레그램 전송하도록 macOS에 등록
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/.venv/bin/python"
if [ ! -x "$PY" ]; then
  python3 -m venv "$DIR/.venv" && "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
fi
if [ ! -f "$DIR/telegram.json" ]; then
  echo "먼저 텔레그램을 연결합니다."; "$PY" "$DIR/telegram_setup.py"
fi
mkdir -p "$DIR/results" "$HOME/Library/LaunchAgents"
PLIST="$HOME/Library/LaunchAgents/com.jchun.coinscanner.plist"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.jchun.coinscanner</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/hourly.py</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StartCalendarInterval</key><dict><key>Minute</key><integer>1</integer></dict>
  <key>StandardOutPath</key><string>$DIR/results/hourly.log</string>
  <key>StandardErrorPath</key><string>$DIR/results/hourly.log</string>
</dict></plist>
PL
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "등록 완료: 매시 01분마다 실행됩니다. 기록: $DIR/results/hourly.log"
echo "지금 한 번 바로 실행해 볼게요..."
"$PY" "$DIR/hourly.py"
