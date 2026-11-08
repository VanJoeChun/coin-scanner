"""텔레그램 알림.

설정 파일: telegram.json  (telegram_setup.py 가 만들어 줌)
  {"bot_token": "...", "chat_id": "...", "min_grade": "A", "cooldown_hours": 6}
"""
import html
import json
import os
from datetime import datetime, timedelta, timezone

import requests

from scanner import ROOT

CFG_PATH = os.path.join(ROOT, "telegram.json")
STATE_PATH = os.path.join(ROOT, "results", "notified.json")
GRADES = {"A": 3, "B": 2, "C": 1, "D": 0}


def load_cfg():
    """telegram.json 또는 환경변수(GitHub Actions Secrets)에서 읽음."""
    c = {}
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, encoding="utf-8") as f:
            c = json.load(f)
    env = os.environ
    if env.get("TELEGRAM_BOT_TOKEN"):
        c["bot_token"] = env["TELEGRAM_BOT_TOKEN"]
    if env.get("TELEGRAM_CHAT_ID"):
        c["chat_id"] = env["TELEGRAM_CHAT_ID"]
    if env.get("TELEGRAM_MIN_GRADE"):
        c["min_grade"] = env["TELEGRAM_MIN_GRADE"]
    if env.get("TELEGRAM_COOLDOWN_HOURS"):
        c["cooldown_hours"] = float(env["TELEGRAM_COOLDOWN_HOURS"])
    return c if c.get("bot_token") and c.get("chat_id") else None


def send(text, cfg=None):
    cfg = cfg or load_cfg()
    if not cfg:
        return False
    r = requests.post(f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
                      json=dict(chat_id=cfg["chat_id"], text=text, parse_mode="HTML",
                                disable_web_page_preview=True), timeout=15)
    return r.ok


def _fmt(x):
    if x is None:
        return "-"
    return f"{x:,.2f}" if abs(x) >= 1000 else f"{x:.6g}"


def _pick_text(p):
    tp = p.get("tp2") or p.get("tp1")
    tp_txt = f"{_fmt(tp)} ({(tp / p['price'] - 1) * 100:+.2f}%)" if tp else "규칙 청산"
    if p.get("tp1") and p.get("tp2"):
        tp_txt += f"\n1차 익절(½): {_fmt(p['tp1'])}"
    rr = f"1:{p['rr_net']:.1f}" if p.get("rr_net") is not None else "-"
    lines = [
        f"<b>[{p['grade']} {p['pick_score']:.0f}점] {html.escape(p['product'])}</b> — {html.escape(p['name'])}",
        f"현재가: {_fmt(p['price'])}",
        f"손절: {_fmt(p['stop'])} (-{p['risk_pct']:.2f}%)",
        f"익절: {tp_txt}",
        f"손익비(수수료 후): {rr} · 권장 금액: ${p['size_usd']:.0f} (손절 시 -${p['loss_usd']:.0f})",
    ]
    lines += ["+ " + html.escape(w) for w in p.get("why", [])]
    lines += ["! " + html.escape(w) for w in p.get("risks", [])]
    lines.append(f"청산 규칙: {html.escape(p['exit_rule'])}")
    lines.append(f'<a href="https://www.coinbase.com/advanced-trade/spot/{p["product"]}">코인베이스에서 열기</a>')
    return "\n".join(lines)


def notify_picks(result, cfg=None):
    """새 A등급(또는 min_grade 이상) 후보만 보냄. 같은 코인은 cooldown 동안 다시 안 보냄."""
    cfg = cfg or load_cfg()
    if not cfg or not result:
        return 0
    min_g = GRADES.get(cfg.get("min_grade", "A"), 3)
    cooldown = timedelta(hours=cfg.get("cooldown_hours", 6))
    now = datetime.now(timezone.utc)
    try:
        state = json.load(open(STATE_PATH, encoding="utf-8"))
    except Exception:
        state = {}
    state = {k: v for k, v in state.items() if now - datetime.fromisoformat(v) < timedelta(days=2)}
    new = []
    for p in result.get("picks", []):
        if GRADES.get(p["grade"], 0) < min_g:
            continue
        last = state.get(p["product"])
        if last and now - datetime.fromisoformat(last) < cooldown:
            continue
        new.append(p)
    if not new:
        return 0
    reg = result["regime"]
    head = (f"🔔 <b>매수 후보 {len(new)}개</b> · {datetime.now().strftime('%m/%d %H:%M')}\n"
            f"시장: {reg['label']} · 변동성 {reg['vol_state']} · 1h 상승추세 {reg['breadth']:.0f}%\n"
            f"{html.escape(result.get('verdict', ''))}")
    if reg.get("warning"):
        head += f"\n⚠ {html.escape(reg['warning'])}"
    body = "\n\n".join(_pick_text(p) for p in new)
    tail = "\n\n<i>신호 시점 가격입니다. 주문 전 현재가를 확인하세요. 투자 판단은 본인 책임입니다.</i>"
    if send(head + "\n\n" + body + tail, cfg):
        for p in new:
            state[p["product"]] = now.isoformat()
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        json.dump(state, open(STATE_PATH, "w", encoding="utf-8"))
        return len(new)
    return 0
