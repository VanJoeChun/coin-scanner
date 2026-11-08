"""1시간마다 실행되는 작업: 스캔 1회 → 새 A등급 후보가 있으면 텔레그램 전송.
macOS launchd 가 install_hourly.sh 설정대로 이 파일을 실행합니다. 수동 실행도 가능:  python3 hourly.py
"""
import os
from datetime import datetime

from scanner import Scanner
from telegram_notify import load_cfg, notify_picks


def main():
    cfg = load_cfg()
    if not cfg:
        print("telegram.json 이 없습니다. 먼저 python3 telegram_setup.py 를 실행하세요.")
        return
    s = Scanner()
    s.scan()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not s.result:
        print(stamp, "스캔 실패:", s.status.get("error"))
        return
    grades = [p["grade"] for p in s.result.get("picks", [])]
    n = notify_picks(s.result, cfg)
    print(f"{stamp} 스캔 {s.result['scanned']}개 · 후보 등급 {grades or '없음'} · 텔레그램 전송 {n}개")
    write_summary(s.result, n)


def write_summary(r, sent):
    """GitHub Actions 실행 화면에 결과 표 남기기."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    g = r["regime"]
    lines = [f"### 스캔 결과 · 시장 {g['label']} · 변동성 {g['vol_state']} · 텔레그램 {sent}건",
             f"{r.get('verdict', '')}", "", "| 등급 | 코인 | 매매법 | 현재가 | 손절 | 익절 |", "|---|---|---|---|---|---|"]
    for p in r.get("picks", []):
        tp = p.get("tp2") or p.get("tp1")
        lines.append(f"| {p['grade']} {p['pick_score']:.0f} | {p['product']} | {p['name']} | {p['price']:.6g} | "
                     f"{p['stop']:.6g} (-{p['risk_pct']:.2f}%) | {f'{tp:.6g}' if tp else '규칙 청산'} |")
    lines += ["", "오늘의 전략 TOP 3: " + ", ".join(s["name"] for s in r["strategies"][:3])]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
