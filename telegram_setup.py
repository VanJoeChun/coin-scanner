"""텔레그램 봇 연결 도우미:  python3 telegram_setup.py

1) 텔레그램에서 @BotFather 에게 /newbot → 봇 이름 정하기 → 토큰 받기
2) 이 스크립트에 토큰 붙여넣기
3) 만든 봇에게 아무 메시지나 보내기 (예: hi)
4) 엔터 → chat_id 를 자동으로 찾아 telegram.json 에 저장하고 테스트 메시지를 보냄
"""
import json
import os

import requests

from telegram_notify import CFG_PATH, send


def main():
    token = input("BotFather 에게 받은 봇 토큰을 붙여넣으세요: ").strip()
    me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
    if not me.get("ok"):
        print("토큰이 올바르지 않습니다:", me.get("description"))
        return
    print(f"봇 확인: @{me['result']['username']}")
    input("텔레그램에서 이 봇에게 아무 메시지나 보낸 뒤 엔터를 누르세요...")
    up = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15).json()
    chats = [u["message"]["chat"] for u in up.get("result", []) if "message" in u]
    if not chats:
        print("메시지를 못 찾았습니다. 봇에게 메시지를 보냈는지 확인하고 다시 실행하세요.")
        return
    chat = chats[-1]
    cfg = {"bot_token": token, "chat_id": str(chat["id"]), "min_grade": "A", "cooldown_hours": 6}
    if os.path.exists(CFG_PATH):
        old = json.load(open(CFG_PATH, encoding="utf-8"))
        cfg["min_grade"] = old.get("min_grade", "A")
        cfg["cooldown_hours"] = old.get("cooldown_hours", 6)
    json.dump(cfg, open(CFG_PATH, "w", encoding="utf-8"), indent=1)
    os.chmod(CFG_PATH, 0o600)
    ok = send("✅ 코인 스캐너 알림이 연결됐습니다. A등급 매수 후보가 나오면 여기로 보내드릴게요.", cfg)
    print("저장 완료: telegram.json /", "테스트 메시지 전송 성공" if ok else "테스트 메시지 전송 실패")


if __name__ == "__main__":
    main()
