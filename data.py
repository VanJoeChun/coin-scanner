"""Coinbase 공개(무인증) 시세 API 클라이언트.

- 상품 목록:  GET https://api.exchange.coinbase.com/products
- 캔들:      GET https://api.exchange.coinbase.com/products/{id}/candles?granularity=300&start=&end=
  응답 = [[time, low, high, open, close, volume], ...] (최신순, 최대 300~350개)
API 키가 필요 없고, 주문 기능은 없습니다.
"""
import threading
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests

BASE = "https://api.exchange.coinbase.com"
STABLES = {"USDC", "USDT", "DAI", "PYUSD", "GUSD", "PAX", "USDS", "EURC", "RLUSD", "FDUSD", "TUSD", "BUSD", "USD1", "GYEN"}


class RateLimiter:
    def __init__(self, per_sec):
        self.interval = 1.0 / per_sec
        self.lock = threading.Lock()
        self.next_t = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next_t)
            self.next_t = t + self.interval
        delay = t - time.monotonic()
        if delay > 0:
            time.sleep(delay)


class CoinbasePublic:
    def __init__(self, req_per_sec=8):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "daytrade-scanner/1.0"
        self.rl = RateLimiter(req_per_sec)

    def _get(self, path, params=None, tries=5):
        for i in range(tries):
            self.rl.wait()
            try:
                r = self.s.get(BASE + path, params=params, timeout=15)
                if r.status_code == 429:
                    time.sleep(1.0 + i)
                    continue
                r.raise_for_status()
                return r.json()
            except requests.RequestException:
                if i == tries - 1:
                    raise
                time.sleep(0.5 * (i + 1))
        return None

    def usd_spot_products(self, quote="USD", exclude_stables=True):
        prods = self._get("/products")
        out = []
        for p in prods:
            if p.get("quote_currency") != quote:
                continue
            if p.get("status") != "online" or p.get("trading_disabled") or p.get("cancel_only"):
                continue
            if p.get("auction_mode"):
                continue
            if exclude_stables and (p.get("fx_stablecoin") or p.get("base_currency") in STABLES):
                continue
            out.append(p["id"])
        return sorted(out)

    def candles(self, product, granularity=300, bars=300, end=None):
        """최근 `bars`개 완성 캔들 (오래된→최신 순). 진행 중인 캔들은 제거."""
        end = (end or datetime.now(timezone.utc)).replace(microsecond=0)
        frames, remaining, cur_end = [], bars + 1, end
        while remaining > 0:
            n = min(300, remaining)
            start = cur_end - timedelta(seconds=granularity * n)
            data = self._get(f"/products/{product}/candles", {
                "granularity": granularity,
                "start": start.isoformat(), "end": cur_end.isoformat()})
            if not data:
                break
            frames.append(pd.DataFrame(data, columns=["time", "low", "high", "open", "close", "volume"]))
            remaining -= n
            cur_end = start
            if len(data) < n * 0.5 and remaining > 0:  # 상장 초기 등 데이터 부족
                break
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames).drop_duplicates("time").sort_values("time")
        now_ts = time.time()
        df = df[df["time"] + granularity <= now_ts]  # 미완성 캔들 제거
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time")[["open", "high", "low", "close", "volume"]].astype(float)
        return df.tail(bars)
