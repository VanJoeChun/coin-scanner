"""스캐너: 데이터 수집 → 전략 백테스트(최근 성과) → 시장 국면 → 오늘의 전략 순위 → 실시간 신호."""
import json
import math
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import backtest as bt
import indicators as ind
import strategies as S
from data import CoinbasePublic

ROOT = os.path.dirname(os.path.abspath(__file__))

FIT = {  # 시장 국면별 전략 계열 적합도 (-1 ~ +1)
    "상승": {"breakout": 1.0, "pullback": 0.8, "momentum": 0.7, "reversal": -0.2},
    "횡보": {"breakout": -0.3, "pullback": 0.1, "momentum": -0.2, "reversal": 0.8},
    "하락": {"breakout": -0.8, "pullback": -0.6, "momentum": -0.5, "reversal": 0.3},
}


def load_config():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    # 공개 저장소에 계좌 규모를 올리지 않도록 환경변수(Secrets)로 덮어쓰기 가능
    for env, key in [("ACCOUNT_USD", "account_usd"), ("RISK_PER_TRADE_PCT", "risk_per_trade_pct"),
                     ("FEE_PCT_PER_SIDE", "fee_pct_per_side")]:
        if os.environ.get(env):
            cfg[key] = float(os.environ[env])
    return cfg


def _clean(x):
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    if isinstance(x, (np.floating,)):
        return _clean(float(x))
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, (pd.Timestamp, datetime)):
        return x.isoformat()
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    return x


class Scanner:
    def __init__(self, cfg=None):
        self.cfg = cfg or load_config()
        self.api = CoinbasePublic(self.cfg.get("requests_per_sec", 8))
        self.lock = threading.Lock()
        self.products, self.products_ts = [], 0
        self.c5, self.c1h, self.c1h_ts = {}, {}, {}
        self.bt_ts, self.bt_result = 0, None
        self.coin_perf = {}
        self.result = None
        self.status = dict(running=False, step="대기", done=0, total=0, last=None, error=None)

    # ------------------------------------------------------------------ helpers
    def _progress(self, step, done=None, total=None):
        self.status["step"] = step
        if done is not None:
            self.status["done"] = done
        if total is not None:
            self.status["total"] = total

    def _pmap(self, fn, items, label):
        out = {}
        self._progress(label, 0, len(items))
        with ThreadPoolExecutor(self.cfg.get("threads", 8)) as ex:
            futs = {ex.submit(fn, it): it for it in items}
            for k, f in enumerate(futs):
                it = futs[f]
                try:
                    out[it] = f.result()
                except Exception:
                    out[it] = None
                self.status["done"] = k + 1
        return out

    def _strategy_ids(self):
        en = self.cfg.get("enabled_strategies")
        return [k for k in S.STRATEGIES if not en or k in en]

    # ------------------------------------------------------------------ data
    def refresh_products(self):
        if time.time() - self.products_ts > 3600 or not self.products:
            self.products = self.api.usd_spot_products(self.cfg.get("quote", "USD"), self.cfg.get("exclude_stablecoins", True))
            ex = set(self.cfg.get("exclude_products", []))
            self.products = [p for p in self.products if p not in ex]
            self.products_ts = time.time()

    def fetch_1h(self, prods):
        stale = [p for p in prods if time.time() - self.c1h_ts.get(p, 0) > self.cfg.get("htf_refresh_min", 30) * 60]
        res = self._pmap(lambda p: self.api.candles(p, 3600, 300), stale, "1시간봉 수집")
        for p, df in res.items():
            if df is not None and not df.empty:
                self.c1h[p], self.c1h_ts[p] = df, time.time()

    def fetch_5m(self, prods, bars):
        res = self._pmap(lambda p: self.api.candles(p, 300, bars), prods, f"5분봉 수집({bars}봉)")
        return {p: df for p, df in res.items() if df is not None and len(df) >= 60}

    # ------------------------------------------------------------------ regime
    def regime(self, prepared):
        btc = self.c1h.get("BTC-USD")
        label, vol_ratio, detail = "횡보", 1.0, {}
        if btc is not None and len(btc) > 60:
            c = btc["close"]
            e20, e50 = ind.ema(c, 20), ind.ema(c, 50)
            if c.iloc[-1] > e50.iloc[-1] and e20.iloc[-1] > e50.iloc[-1]:
                label = "상승"
            elif c.iloc[-1] < e50.iloc[-1] and e20.iloc[-1] < e50.iloc[-1]:
                label = "하락"
            atrp = ind.atr(btc, 14) / c
            vol_ratio = float(atrp.iloc[-1] / atrp.tail(240).median())
            detail = dict(btc=float(c.iloc[-1]), btc_24h=float(c.iloc[-1] / c.iloc[-25] - 1) * 100)
        liquid = [p for p, x in prepared.items() if x["vol24"] >= self.cfg["min_24h_volume_usd"]]
        up = [p for p in liquid if bool(prepared[p]["d"]["htf_up"].iloc[-1])]
        breadth = len(up) / len(liquid) * 100 if liquid else 0.0
        # 시장 폭이 BTC 판단과 크게 다르면 한 단계 보정
        if label == "상승" and breadth < 30:
            label = "횡보"
        if label == "하락" and breadth > 60:
            label = "횡보"
        vol_state = "확장" if vol_ratio > 1.25 else ("수축" if vol_ratio < 0.8 else "보통")
        warn = None
        if label == "하락":
            warn = "BTC 1시간 추세 하락 + 현물은 롱만 가능 → 신호가 나와도 비중을 줄이거나 관망을 권장합니다."
        return dict(label=label, vol_ratio=vol_ratio, vol_state=vol_state, breadth=breadth,
                    liquid_count=len(liquid), warning=warn, **detail)

    def fit(self, fam, reg):
        f = FIT[reg["label"]][fam]
        if reg["vol_state"] == "확장" and fam in ("breakout", "momentum"):
            f += 0.3
        if reg["vol_state"] == "수축" and fam in ("reversal", "pullback"):
            f += 0.2
        return max(-1.0, min(1.0, f))

    # ------------------------------------------------------------------ backtest
    def run_backtest(self, liquid_sorted):
        cfg = self.cfg
        n_coins = cfg.get("backtest_top_n", 30)
        days = cfg.get("backtest_days", 3)
        coins = liquid_sorted[:n_coins]
        bars = days * 288 + 220
        data5 = self.fetch_5m(coins, bars)
        trades = []
        sids = [s for s in self._strategy_ids() if S.STRATEGIES[s].get("timeframe") != "1m"]
        self._progress("백테스트 계산", 0, len(data5))
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        for k, (p, df) in enumerate(data5.items()):
            d = S.prepare(df, self.c1h.get(p))
            for sid in sids:
                meta = S.STRATEGIES[sid]
                try:
                    o = meta["fn"](d, cfg.get("params", {}).get(sid, {}))
                    o.loc[o.index < cutoff, "signal"] = False          # 지표 예열 구간 제외
                    trades += bt.run(d, o, cfg, p, sid)
                except Exception:
                    traceback.print_exc()
            self.status["done"] = k + 1
        if "orb_open" in self._strategy_ids():
            trades += self.backtest_orb(coins[: cfg.get("orb_top_n", 10)], cfg.get("orb_backtest_days", 7))
        now = datetime.now(timezone.utc)
        summary = {}
        for sid in self._strategy_ids():
            tr = [t for t in trades if t["strategy"] == sid]
            summary[sid] = dict(all=bt.summarize(tr), d1=bt.summarize(tr, since=now - timedelta(hours=24)))
        # 코인별 × 전략별 성과 (이 코인에서 이 매매법이 최근 통했는지)
        coin_perf = {}
        for t in trades:
            coin_perf.setdefault((t["product"], t["strategy"]), []).append(t)
        self.coin_perf = {k: bt.summarize(v, prior_n=2) for k, v in coin_perf.items()}
        self.bt_result = dict(time=now, coins=coins, days=days, summary=summary,
                              trades=sorted(trades, key=lambda t: t["entry_time"], reverse=True)[:400])
        self.bt_ts = time.time()

    def fetch_1m_window(self, product, day_et):
        """해당 날짜 9:25~11:05 ET 1분봉."""
        start = pd.Timestamp(day_et).tz_localize("America/New_York") + pd.Timedelta(hours=9, minutes=25)
        end = start + pd.Timedelta(minutes=100)
        now = pd.Timestamp.now(tz="UTC")
        if start > now:
            return pd.DataFrame()
        return self.api.candles(product, 60, 100, end=min(end, now).to_pydatetime())

    def backtest_orb(self, coins, days):
        today = pd.Timestamp.now(tz="America/New_York").normalize().tz_localize(None)
        dates = [today - pd.Timedelta(days=k) for k in range(0, days + 3)]
        dates = [x for x in dates if x.weekday() < 5][:days]
        jobs = [(p, dt) for p in coins for dt in dates]
        res = self._pmap(lambda j: self.fetch_1m_window(*j), jobs, "개장 돌파(1분봉) 백테스트")
        trades = []
        for (p, dt), df in res.items():
            if df is None or df.empty:
                continue
            o = S.orb_open_5min(df, self.cfg.get("params", {}).get("orb_open", {}))
            trades += bt.run(df, o, dict(self.cfg, max_hold_bars=90), p, "orb_open")
        return trades

    # ------------------------------------------------------------------ live signals
    def live_signals(self, prepared, strat_score, reg):
        cfg = self.cfg
        fresh = cfg.get("fresh_bars", 2)
        sigs, watch = [], []
        sids = [s for s in self._strategy_ids() if S.STRATEGIES[s].get("timeframe") != "1m"]
        for p, x in prepared.items():
            d = x["d"]
            last = d.iloc[-1]
            price = float(last["close"])
            for sid in sids:
                meta = S.STRATEGIES[sid]
                try:
                    o = meta["fn"](d, cfg.get("params", {}).get(sid, {}))
                except Exception:
                    continue
                tail = o.tail(fresh)
                hits = tail.index[tail["signal"].fillna(False).astype(bool)]
                if len(hits):
                    t = hits[-1]
                    r = o.loc[t]
                    entry_ref = float(d.loc[t, "close"])
                    stop = float(r["stop"]) if not pd.isna(r["stop"]) else None
                    if stop is None or stop >= price:
                        continue
                    risk = entry_ref - stop
                    if risk <= 0 or price - entry_ref > 0.5 * risk:      # 이미 많이 올라 추격 금지
                        continue
                    if (price - stop) / price * 100 > cfg.get("max_risk_pct", 6.0):   # 손절폭이 너무 넓은 신호 제외(백테스트와 같은 기준)
                        continue
                    sigs.append(self._signal_row(p, sid, meta, t, price, entry_ref, stop, r, x, strat_score, reg))
                elif bool(o["watch"].iloc[-1]) and not pd.isna(o["trigger"].iloc[-1]):
                    trig = float(o["trigger"].iloc[-1])
                    if x["vol24"] >= cfg["min_24h_volume_usd"]:
                        watch.append(dict(product=p, strategy=sid, name=meta["name"], price=price, trigger=trig,
                                          dist_pct=(trig / price - 1) * 100, vol24=x["vol24"],
                                          strat_score=strat_score.get(sid, {}).get("final", 0)))
        # ORB 실시간 (평일 9:35~11:00 ET)
        now_et = pd.Timestamp.now(tz="America/New_York")
        if "orb_open" in self._strategy_ids() and now_et.weekday() < 5 and 9 * 60 + 35 <= now_et.hour * 60 + now_et.minute <= 11 * 60:
            liq = sorted(prepared, key=lambda p: -prepared[p]["vol24"])[: cfg.get("orb_top_n", 10)]
            for p in liq:
                try:
                    df1 = self.fetch_1m_window(p, now_et.normalize().tz_localize(None))
                    o = S.orb_open_5min(df1, cfg.get("params", {}).get("orb_open", {}))
                    if o["signal"].tail(3).any():
                        t = o.index[o["signal"]][-1]
                        r = o.loc[t]
                        price = float(df1["close"].iloc[-1])
                        if price > r["stop"]:
                            sigs.append(self._signal_row(p, "orb_open", S.STRATEGIES["orb_open"], t, price,
                                                         float(df1.loc[t, "close"]), float(r["stop"]), r,
                                                         prepared[p], strat_score, reg))
                except Exception:
                    traceback.print_exc()
        sigs.sort(key=lambda s: -s["score"])
        watch.sort(key=lambda w: (-w["strat_score"], abs(w["dist_pct"])))
        return sigs, watch[: cfg.get("max_watch", 60)]

    def _signal_row(self, p, sid, meta, t, price, entry_ref, stop, r, x, strat_score, reg):
        cfg = self.cfg
        risk = price - stop
        tp1 = None if pd.isna(r["tp1"]) else float(r["tp1"])
        tp2 = None if pd.isna(r["tp2"]) else float(r["tp2"])
        fee_rt = 2 * cfg["fee_pct_per_side"]
        risk_pct = risk / price * 100
        acct, rp = cfg["account_usd"], cfg["risk_per_trade_pct"] / 100
        qty_usd = min(acct * rp / (risk_pct / 100 + fee_rt / 100), acct * cfg["max_position_pct"] / 100)
        ss = strat_score.get(sid, {})
        liq = min(1.0, max(0.0, (math.log10(max(x["vol24"], 1)) - 5) / 3))   # $100k→0, $100M→1
        score = 100 * (0.55 * ss.get("rank_norm", 0.5) + 0.2 * liq + 0.15 * float(x["htf_up"])
                       + 0.10 * (1.0 if risk_pct >= 2 * fee_rt else 0.3))
        notes = []
        if x["vol24"] < cfg["min_24h_volume_usd"]:
            notes.append("저유동성")
        if risk_pct < 2 * fee_rt:
            notes.append("손절폭이 수수료 대비 좁음")
        if reg["label"] == "하락":
            notes.append("시장 하락 국면")
        d = x["d"]
        last = d.iloc[-1]
        atr = float(last["atr"]) if not pd.isna(last["atr"]) else 0.0
        ext_atr = (price - float(last["sma20"])) / atr if atr > 0 and not pd.isna(last["sma20"]) else 0.0
        vol_surge = float(d["volume"].tail(3).mean() / last["vol_avg"]) if last["vol_avg"] and not pd.isna(last["vol_avg"]) else 1.0
        return dict(product=p, strategy=sid, name=meta["name"], family=meta["family"], signal_time=t,
                    chg24=x.get("chg24"), ext_atr=ext_atr, vol_surge=vol_surge,
                    coin_bt=self.coin_perf.get((p, sid)), strat_avg_r=ss.get("bt", {}).get("all", {}).get("avg_r"),
                    price=price, entry_ref=entry_ref, stop=stop, tp1=tp1, tp2=tp2,
                    risk_pct=risk_pct, tp_pct=((tp2 or tp1 or price) / price - 1) * 100 if (tp2 or tp1) else None,
                    exit_rule=meta["rule"], vol24=x["vol24"], htf_up=bool(x["htf_up"]),
                    size_usd=qty_usd, score=score, notes=notes,
                    strat_rank=ss.get("rank"), low_liq=x["vol24"] < cfg["min_24h_volume_usd"])

    # ------------------------------------------------------------------ 지금 매수 후보
    def build_picks(self, sigs, watch, reg, n=5):
        cfg = self.cfg
        fee = cfg["fee_pct_per_side"]
        by_coin = {}
        for s in sigs:
            if s["low_liq"]:
                continue
            by_coin.setdefault(s["product"], []).append(s)
        picks = []
        for p, lst in by_coin.items():
            best = max(lst, key=lambda s: s["score"])
            sc, why, risk = best["score"], [], []
            why.append(f"{best['name']} 신호 (오늘 전략 순위 {best['strat_rank']}위)")
            # 1) 이 코인에서 이 매매법이 최근 통했는가
            cb = best.get("coin_bt")
            if cb and cb["n"] >= 2:
                if cb["avg"] > 0.3:
                    sc += 10; why.append(f"이 코인에서 최근 {cb['n']}회 평균 {cb['avg']:+.2f}% (잘 맞음)")
                elif cb["avg"] > 0:
                    sc += 4; why.append(f"이 코인에서 최근 {cb['n']}회 평균 {cb['avg']:+.2f}%")
                else:
                    sc -= 10; risk.append(f"이 코인에선 최근 {cb['n']}회 평균 {cb['avg']:+.2f}%로 안 맞았음")
            # 2) 여러 매매법이 동시에 가리키는가
            others = sorted({s["name"] for s in lst if s["strategy"] != best["strategy"]})
            if others:
                sc += min(12, 6 * len(others)); why.append("동시 신호: " + ", ".join(others))
            # 3) 수수료 뺀 손익비
            price, stop = best["price"], best["stop"]
            cost = price * 2 * fee / 100
            tgt = best["tp2"] or best["tp1"]
            if tgt:
                rr = (tgt - price - cost) / (price - stop + cost)
                best["rr_net"] = rr
                if rr >= 2:
                    sc += 8; why.append(f"수수료 뺀 손익비 1:{rr:.1f}")
                elif rr >= 1.3:
                    sc += 3
                elif rr < 1:
                    sc -= 8; risk.append(f"수수료 뺀 손익비 1:{rr:.1f} (낮음)")
            else:
                ar = best.get("strat_avg_r")
                best["rr_net"] = None
                if ar is not None and ar < 0:
                    sc -= 6; risk.append("목표가 없이 규칙 청산 + 최근 평균 R 마이너스")
            # 4) 상대 강도·거래량·추격 위험
            rs = (best.get("chg24") or 0) - (reg.get("btc_24h") or 0)
            if rs > 2:
                sc += 5; why.append(f"BTC보다 강함 (24h {best['chg24']:+.1f}%)")
            elif rs < -5:
                sc -= 4; risk.append(f"BTC보다 약함 (24h {best['chg24']:+.1f}%)")
            if best["vol_surge"] >= 1.5:
                sc += 5; why.append(f"거래량 평소의 {best['vol_surge']:.1f}배")
            if best["ext_atr"] > 3:
                sc -= 10; risk.append(f"20이평에서 ATR {best['ext_atr']:.1f}배 떨어짐 (추격 위험)")
            if not best["htf_up"]:
                risk.append("1시간 추세는 아직 상승 아님")
            if reg["label"] == "하락":
                sc -= 15; risk.append("시장 하락 국면")
            for nt in best["notes"]:
                if nt not in ("시장 하락 국면",) and nt not in risk:
                    risk.append(nt)
            sc = max(0.0, min(100.0, sc))
            # 수수료에 비해 손절폭이 좁거나 손익비가 1 미만이면 A등급 금지(텔레그램 A 알림 품질 유지)
            if "손절폭이 수수료 대비 좁음" in best["notes"] or (best.get("rr_net") is not None and best["rr_net"] < 1):
                sc = min(sc, 79.0)
            grade = "A" if sc >= 80 else ("B" if sc >= 65 else ("C" if sc >= 50 else "D"))
            acct_loss = best["size_usd"] * ((price - stop) / price + 2 * fee / 100)
            gain = best["size_usd"] * ((best["tp2"] / price - 1) - 2 * fee / 100) if best["tp2"] else None
            picks.append(dict(best, pick_score=sc, grade=grade, why=why, risks=risk,
                              loss_usd=acct_loss, gain_usd=gain, n_signals=len(lst)))
        picks.sort(key=lambda x: -x["pick_score"])
        good = [x for x in picks if x["grade"] in ("A", "B")]
        if reg["label"] == "하락" and not any(x["grade"] == "A" for x in picks):
            verdict = "관망 권장 — 시장이 하락 국면이고 A등급 후보가 없습니다."
        elif good:
            verdict = f"진입 검토 가능 후보 {len(good)}개 (A/B등급)."
        elif picks:
            verdict = "신호는 있지만 조건이 약합니다(C/D등급). 작게 하거나 다음 스캔을 기다리세요."
        else:
            verdict = "지금 조건을 만족한 코인이 없습니다. 아래 '돌파 대기' 코인을 지켜보세요."
        return picks[:n], verdict

    # ------------------------------------------------------------------ main
    def scan(self):
        if self.status["running"]:
            return
        self.status.update(running=True, error=None)
        t0 = time.time()
        try:
            cfg = self.cfg
            self._progress("코인 목록 조회")
            self.refresh_products()
            prods = self.products
            self.fetch_1h(prods)
            data5 = self.fetch_5m(prods, cfg.get("live_bars", 300))
            self._progress("지표 계산", 0, len(data5))
            prepared = {}
            for k, (p, df) in enumerate(data5.items()):
                d = S.prepare(df, self.c1h.get(p))
                tail = df.tail(288)
                vol24 = float((tail["close"] * tail["volume"]).sum())
                chg24 = float(df["close"].iloc[-1] / tail["open"].iloc[0] - 1) * 100
                prepared[p] = dict(d=d, vol24=vol24, chg24=chg24, htf_up=bool(d["htf_up"].iloc[-1]))
                self.status["done"] = k + 1
            self.c5 = {p: x["d"] for p, x in prepared.items()}
            liquid_sorted = sorted([p for p in prepared if prepared[p]["vol24"] >= cfg["min_24h_volume_usd"]],
                                   key=lambda p: -prepared[p]["vol24"])
            if self.bt_result is None or time.time() - self.bt_ts > cfg.get("backtest_refresh_min", 60) * 60:
                self.run_backtest(liquid_sorted)
            reg = self.regime(prepared)
            # 전략 순위
            ss = {}
            w = cfg.get("regime_weight", 0.15)
            for sid in self._strategy_ids():
                meta = S.STRATEGIES[sid]
                sm = self.bt_result["summary"][sid]
                perf = 0.5 * sm["all"]["score"] + 0.5 * sm["d1"]["score"]
                f = self.fit(meta["family"], reg)
                ss[sid] = dict(id=sid, name=meta["name"], family=meta["family"], rule=meta["rule"],
                               video=f"https://www.youtube.com/watch?v={meta['video']}",
                               perf=perf, fit=f, final=perf + w * f, bt=sm)
            order = sorted(ss, key=lambda s: -ss[s]["final"])
            for k, sid in enumerate(order):
                ss[sid]["rank"] = k + 1
                ss[sid]["rank_norm"] = 1 - k / max(1, len(order) - 1)
            sigs, watch = self.live_signals(prepared, ss, reg)
            picks, verdict = self.build_picks(sigs, watch, reg, cfg.get("max_picks", 5))
            top = [ss[s] for s in order]
            best = top[0] if top else None
            pick = None
            if best:
                pick = dict(best)
                if best["final"] <= 0:
                    pick["comment"] = "최근 백테스트에서 수수료를 빼고 이긴 전략이 없습니다. 오늘은 관망하거나 아주 작게만 하는 것을 권합니다."
                else:
                    pick["comment"] = (f"최근 {self.bt_result['days']}일·24시간 백테스트 성과와 현재 시장({reg['label']}, 변동성 {reg['vol_state']})에 "
                                       f"가장 잘 맞는 전략입니다.")
            movers = sorted(({"product": p, "chg24": x["chg24"], "vol24": x["vol24"]} for p, x in prepared.items()
                             if x["vol24"] >= cfg["min_24h_volume_usd"]), key=lambda m: -m["chg24"])[:15]
            self.result = _clean(dict(
                time=datetime.now(timezone.utc), scanned=len(prepared), total_products=len(prods),
                regime=reg, pick=pick, picks=picks, verdict=verdict, strategies=top, signals=sigs, watch=watch, movers=movers,
                backtest=dict(time=self.bt_result["time"], coins=self.bt_result["coins"], days=self.bt_result["days"],
                              trades=self.bt_result["trades"][:200]),
                config=dict(fee=cfg["fee_pct_per_side"], account=cfg["account_usd"], risk=cfg["risk_per_trade_pct"],
                            min_vol=cfg["min_24h_volume_usd"]),
                elapsed=time.time() - t0))
            self._save()
            self.status.update(last=self.result["time"])
        except Exception as e:
            traceback.print_exc()
            self.status["error"] = str(e)
        finally:
            self.status.update(running=False, step="완료" if not self.status["error"] else "오류")

    def _save(self):
        os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
        with open(os.path.join(ROOT, "results", "latest.json"), "w", encoding="utf-8") as f:
            json.dump(self.result, f, ensure_ascii=False, indent=1)
        if self.result["signals"]:
            day = datetime.now().strftime("%Y%m%d")
            path = os.path.join(ROOT, "results", f"signals_{day}.csv")
            df = pd.DataFrame(self.result["signals"]).drop(columns=["exit_rule", "notes"], errors="ignore")
            df["scan_time"] = self.result["time"]
            df.to_csv(path, mode="a", header=not os.path.exists(path), index=False)

    def chart(self, product, strategy=None):
        d = self.c5.get(product)
        if d is None:
            return None
        d = d.tail(300)
        out = dict(product=product, candles=[dict(time=int(t.timestamp()), open=r.open, high=r.high, low=r.low,
                                                   close=r.close, volume=r.volume) for t, r in d.iterrows()])
        lines = {}
        for col in ["sma20", "sma50", "sma200", "hma100", "bb_up", "bb_lo", "vwap"]:
            if col in d:
                lines[col] = [dict(time=int(t.timestamp()), value=float(v)) for t, v in d[col].items() if not pd.isna(v)]
        out["lines"] = lines
        marks = []
        if strategy and strategy in S.STRATEGIES and S.STRATEGIES[strategy].get("timeframe") != "1m":
            o = S.STRATEGIES[strategy]["fn"](d, self.cfg.get("params", {}).get(strategy, {}))
            for t in o.index[o["signal"].fillna(False).astype(bool)]:
                marks.append(dict(time=int(t.timestamp()), position="belowBar", color="#16a34a", shape="arrowUp",
                                  text=S.STRATEGIES[strategy]["name"].split()[0]))
        out["markers"] = marks
        return _clean(out)
