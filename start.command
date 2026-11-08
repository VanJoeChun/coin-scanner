#!/bin/bash
# 더블클릭으로 실행 (macOS). 처음 한 번만 가상환경을 만들고 패키지를 설치합니다.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv && ./.venv/bin/pip install -q -r requirements.txt
fi
./.venv/bin/python app.py
