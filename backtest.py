"""간단하고 보수적인 봉 단위 백테스트.

- 진입: 신호봉 종가 (+슬리피지)
- 같은 봉에서 손절과 익절이 동시에 닿으면 손절로 처리(보수적)
- 수수료: 진입·청산 양쪽에 fee_pct 적용
- 코인·전략당 동시에 1포지션
"""
import numpy as np
import pandas as pd


def run(d, o, cfg, product="", strategy=""):
    fee = cfg["fee_pct_per_side"] / 100.0
    slip = cfg.get("slippage_pct", 0.05) / 100.0
    max_hold = cfg.get("max_hold_bars", 96)
    min_risk = cfg.get("min_risk_pct", 0.25) / 100.0
    max_risk = cfg.get("max_risk_pct", 6.0) / 100.0

    O, H, L, C = (d[x].values for x in ["open", "high", "low", "close"])
    sig = o["signal"].fillna(False).values.astype(bool)
    stp = o["stop"].values.astype(float)
    tp1 = o["tp1"].values.astype(float)
    tp2 = o["tp2"].values.astype(float)
    ex1 = o["exit1"].fillna(False).values.astype(bool)
    ex2 = o["exit2"].fillna(False).values.astype(bool)
    trl = o["trail"].values.astype(float)
    n = len(d)
    trades = []
    i = 0
    while i < n - 1:
        if not sig[i] or np.isnan(stp[i]):
            i += 1
            continue
        entry = C[i] * (1 + slip)
        stop = stp[i]
        risk = entry - stop
        if risk <= 0 or risk / entry < min_risk or risk / entry > max_risk:
            i += 1
            continue
        t1, t2 = tp1[i], tp2[i]
        rem, pnl, half_done = 1.0, 0.0, False
        reason, j = "timeout", i
        for j in range(i + 1, n):
            if not np.isnan(trl[j - 1]):
                stop = max(stop, trl[j - 1])
            if L[j] <= stop:                                   # 손절(갭이면 시가)
                px = min(O[j], stop) * (1 - slip)
                pnl += rem * (px / entry - 1)
                rem, reason = 0.0, ("breakeven" if half_done and stop >= entry else "stop")
                break
            if not half_done and not np.isnan(t1) and H[j] >= t1:
                pnl += 0.5 * (t1 / entry - 1)
                rem, half_done = 0.5, True
                stop = max(stop, entry)                       # 본전 손절로 상향
            if not np.isnan(t2) and H[j] >= t2:
                pnl += rem * (t2 / entry - 1)
                rem, reason = 0.0, "target"
                break
            if not half_done and ex1[j]:
                pnl += 0.5 * (C[j] * (1 - slip) / entry - 1)
                rem, half_done = 0.5, True
            if ex2[j]:
                pnl += rem * (C[j] * (1 - slip) / entry - 1)
                rem, reason = 0.0, "exit_rule"
                break
            if j - i >= max_hold:
                pnl += rem * (C[j] * (1 - slip) / entry - 1)
                rem, reason = 0.0, "timeout"
                break
        if rem > 0:                                           # 데이터 끝: 미청산 → 현재가 평가
            pnl += rem * (C[j] / entry - 1)
            reason = "open"
        net = pnl - 2 * fee
        trades.append(dict(product=product, strategy=strategy, entry_time=d.index[i], exit_time=d.index[j],
                           entry=entry, stop=stp[i], pnl_pct=net * 100, r_mult=net * entry / risk,
                           reason=reason, bars=j - i))
        i = j + 1
    return trades


def summarize(trades, since=None, prior_n=5):
    df = pd.DataFrame(trades)
    if df.empty:
        return dict(n=0, win=0.0, avg=0.0, sum=0.0, pf=0.0, avg_r=0.0, score=0.0)
    if since is not None:
        df = df[df["entry_time"] >= since]
        if df.empty:
            return dict(n=0, win=0.0, avg=0.0, sum=0.0, pf=0.0, avg_r=0.0, score=0.0)
    wins = df.loc[df.pnl_pct > 0, "pnl_pct"].sum()
    losses = -df.loc[df.pnl_pct <= 0, "pnl_pct"].sum()
    n = len(df)
    return dict(
        n=int(n),
        win=float((df.pnl_pct > 0).mean() * 100),
        avg=float(df.pnl_pct.mean()),
        sum=float(df.pnl_pct.sum()),
        pf=float(wins / losses) if losses > 0 else (99.0 if wins > 0 else 0.0),
        avg_r=float(df.r_mult.mean()),
        # 표본이 적으면 0 쪽으로 당겨서(축소 추정) 우연히 1~2번 이긴 전략이 1등 되는 것을 방지
        score=float(df.pnl_pct.sum() / (n + prior_n)),
    )
