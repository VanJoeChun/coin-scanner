"""로컬 웹 대시보드.  실행:  python3 app.py   →  http://localhost:8765"""
import json
import os
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from scanner import ROOT, Scanner, load_config

cfg = load_config()
scanner = Scanner(cfg)
auto = {"on": cfg.get("auto_scan_minutes", 5) > 0}


def _scan_and_notify():
    scanner.scan()
    try:
        from telegram_notify import load_cfg, notify_picks
        if load_cfg():
            notify_picks(scanner.result)   # 같은 코인은 cooldown 동안 중복 전송 안 함
    except Exception as e:
        print("텔레그램 전송 실패:", e)


def start_scan():
    if not scanner.status["running"]:
        threading.Thread(target=_scan_and_notify, daemon=True).start()


def scheduler():
    """5분봉 마감 15초 뒤마다 자동 스캔."""
    every = max(1, cfg.get("auto_scan_minutes", 5)) * 60
    while True:
        now = time.time()
        nxt = (now // every + 1) * every + 15
        time.sleep(max(1, nxt - now))
        if auto["on"]:
            start_scan()


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=os.path.join(ROOT, "static"), **k)

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/status":
            return self._json(dict(scanner.status, auto=auto["on"]))
        if u.path == "/api/result":
            return self._json(scanner.result or {})
        if u.path == "/api/chart":
            c = scanner.chart(q.get("product", [""])[0], q.get("strategy", [None])[0])
            return self._json(c or {}, 200 if c else 404)
        return super().do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/api/scan":
            start_scan()
            return self._json({"ok": True})
        if u.path == "/api/auto":
            auto["on"] = not auto["on"]
            return self._json({"auto": auto["on"]})
        self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    # 이전 결과가 있으면 먼저 보여 주기
    p = os.path.join(ROOT, "results", "latest.json")
    if os.path.exists(p):
        try:
            scanner.result = json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    port = cfg.get("port", 8765)
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=scheduler, daemon=True).start()
    start_scan()
    url = f"http://localhost:{port}"
    print(f"대시보드: {url}   (종료: Ctrl+C)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("종료")
