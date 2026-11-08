#!/bin/bash
PLIST="$HOME/Library/LaunchAgents/com.jchun.coinscanner.plist"
launchctl unload "$PLIST" 2>/dev/null; rm -f "$PLIST"; echo "1시간 자동 실행을 해제했습니다."
