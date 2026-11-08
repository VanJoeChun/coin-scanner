"""'매매법아카이브' 채널 영상 13개의 규칙을 코인베이스 현물(롱 전용) 5분봉용으로 옮긴 전략들.

각 전략 함수는 지표가 계산된 5분봉 DataFrame `d` 를 받아 아래 컬럼을 가진 DataFrame을 돌려준다.
  signal : 이 봉 종가에 매수 신호 (bool)
  stop   : 손절가
  tp1    : 1차 익절가(절반 매도)       - 없으면 NaN
  tp2    : 최종 익절가(전량 매도)      - 없으면 NaN
  exit1  : 이 봉 종가에 절반 매도      (bool)
  exit2  : 이 봉 종가에 전량 매도      (bool)
  trail  : 추적 손절가(올라가기만 함)  - 없으면 NaN
  watch  : 진입 직전 대기 상태          (bool)
  trigger: 대기 상태일 때 돌파 기준가

영상은 대부분 4시간봉/일봉 + 롱·숏 양방향이지만, 코인베이스 현물은 공매도가 안 되므로
'숏 신호'는 보유 코인을 정리하는 신호로만 쓴다(롱 전용).
"""
import numpy as np
import pandas as pd

import indicators as ind

NAN = np.nan


def _out(d):
    o = pd.DataFrame(index=d.index)
    o["signal"] = False
    for c in ["stop", "tp1", "tp2", "trail", "trigger"]:
        o[c] = NAN
    for c in ["exit1", "exit2", "watch"]:
        o[c] = False
    return o


def _rmult(o, d, r1=None, r2=None, be=None):
    """R 배수 익절가 계산 (진입가 = 신호봉 종가)."""
    risk = d["close"] - o["stop"]
    if r1 is not None:
        o["tp1"] = d["close"] + r1 * risk
    if r2 is not None:
        o["tp2"] = d["close"] + r2 * risk
    return o


# --------------------------------------------------------------------------- 지표 준비
def prepare(df5, df1h):
    d = df5.copy()
    c, h, l, v = d["close"], d["high"], d["low"], d["volume"]
    d["atr"] = ind.atr(d, 14)
    d["vol_avg"] = ind.sma(v, 20)
    for n in (5, 10, 20, 25, 50, 150, 200):
        d[f"sma{n}"] = ind.sma(c, n)
    d["bb_mid"], d["bb_up"], d["bb_lo"], d["bb_bw"] = ind.bollinger(c, 20, 2.0)
    d["rsi"] = ind.rsi(c, 14)
    d["mfi"] = ind.mfi(d, 14)
    d["macd"], d["macd_sig"], d["macd_hist"] = ind.macd(c)
    d["hma55"] = ind.hma(c, 55)
    d["hma100"] = ind.hma(c, 100)
    # 세력 CCI: 거래량 가중 평균(VWMA)을 기준으로 한 CCI
    tp = (h + l + c) / 3
    vwma = (tp * v).rolling(20).sum() / v.rolling(20).sum().replace(0, NAN)
    md = (tp - vwma).abs().rolling(20).mean()
    d["pcci"] = (tp - vwma) / (0.015 * md.replace(0, NAN))
    # 롤링 VWAP (48봉 = 4시간)
    d["vwap"] = (tp * v).rolling(48).sum() / v.rolling(48).sum().replace(0, NAN)

    # 1시간봉 추세 (완성된 1시간봉만 사용 → 미래 데이터 누수 방지)
    if df1h is not None and len(df1h) > 60:
        H = df1h.copy()
        H["e20"] = ind.ema(H["close"], 20)
        H["e50"] = ind.ema(H["close"], 50)
        H["up"] = (H["close"] > H["e50"]) & (H["e20"] > H["e50"])
        H["down"] = (H["close"] < H["e50"]) & (H["e20"] < H["e50"])
        H = H[["up", "down"]].copy()
        H.index = H.index + pd.Timedelta(hours=1)  # 1시간봉 완성 시각
        key = pd.DataFrame({"k": d.index + pd.Timedelta(minutes=5)}, index=d.index)
        m = pd.merge_asof(key.sort_values("k"), H.sort_index(), left_on="k", right_index=True, direction="backward")
        d["htf_up"] = m["up"].fillna(False).astype(bool).values
        d["htf_down"] = m["down"].fillna(False).astype(bool).values
    else:
        d["htf_up"] = True
        d["htf_down"] = False
    return d


# --------------------------------------------------------------------------- 1. 쿨라메기 박스 돌파
def qullamaggie_box(d, p):
    o = _out(d)
    N = p.get("box_len", 12)
    c, h, l, v, a = d["close"], d["high"], d["low"], d["volume"], d["atr"]
    s10, s20, s50 = d["sma10"], d["sma20"], d["sma50"]
    ctx = (s20 > s50) & (s50 > s50.shift(6)) & d["htf_up"]           # 스테이지1 흐름 유지
    bh = h.shift(1).rolling(N).max()
    bl = l.shift(1).rolling(N).min()
    dipped = (c.shift(1) < s20.shift(1)).rolling(N).max() > 0           # 20이평 아래로 눌림
    held50 = (c.shift(1) >= s50.shift(1)).rolling(N).min() > 0          # 박스 중 50이평 이탈 없음
    box_vol = v.shift(1).rolling(N).mean()
    tight = (bh - bl) <= p.get("box_atr", 4.0) * a
    setup = ctx & dipped & held50 & tight
    o["signal"] = setup & (c > bh) & (c > d["open"]) & (v >= p.get("vol_mult", 2.0) * box_vol)
    o["stop"] = bl                                                       # 2차 손절(박스 아래)
    o["exit1"] = c < s10                                                 # 10이평 아래 마감 → 절반
    o["exit2"] = c < s20                                                 # 20이평 아래 마감 → 나머지
    o["watch"] = setup & (c <= bh) & (c >= bh - 0.6 * a)
    o["trigger"] = bh
    return o


# --------------------------------------------------------------------------- 2. 아담 쿠 4단계 돌파
def adam_khoo(d, p):
    o = _out(d)
    N = p.get("box_len", 6)
    c, h, l, a = d["close"], d["high"], d["low"], d["atr"]
    s10, s20, s50 = d["sma10"], d["sma20"], d["sma50"]
    aligned_before = ((s10 > s20) & (s20 > s50)).shift(N).rolling(10).max() > 0   # 1단계 정배열
    dip10 = (c.shift(1) < s10.shift(1)).rolling(N + 4).max() > 0                  # 2단계 10이평 아래 눌림
    held50 = (l.shift(1) > s50.shift(1)).rolling(N + 4).min() > 0                 # 50이평은 지킴
    bh = h.shift(1).rolling(N).max()                                              # 3단계 상자
    bl = l.shift(1).rolling(N).min()
    tight = (bh - bl) <= p.get("box_atr", 3.0) * a
    setup = aligned_before & dip10 & held50 & tight & (s20 > s50) & d["htf_up"]
    body_top = np.maximum(d["open"], c)
    o["signal"] = setup & (c > bh) & (body_top > bh) & (c > d["open"])             # 4단계 몸통 돌파
    o["stop"] = bl
    o["exit1"] = c < s10
    o["exit2"] = c < s20
    o["watch"] = setup & (c <= bh) & (c >= bh - 0.6 * a)
    o["trigger"] = bh
    return o


# --------------------------------------------------------------------------- 3. 알렉스 HMA 리테스트
def hma_retest(d, p):
    o = _out(d)
    hm = d["hma100"]
    c, l = d["close"], d["low"]
    blue = hm > hm.shift(1)
    gray_dot = (c > hm) & ~blue                                  # 분홍 HMA 위로 마감(회색 점)
    had_dot = gray_dot.shift(1).rolling(p.get("dot_window", 24)).max() > 0
    turn_blue = blue & ~blue.shift(1).fillna(False).astype(bool)
    o["signal"] = turn_blue & (c > hm) & had_dot & ~d["htf_down"]
    o["stop"] = l.rolling(p.get("swing", 12)).min()             # 직전 저점
    o["exit2"] = ~blue                                           # 분홍으로 바뀌면 정리
    o["watch"] = gray_dot
    o["trigger"] = NAN
    return o


# --------------------------------------------------------------------------- 4. 임마누엘 20/200 스퀴즈
def emmanuel_squeeze(d, p):
    o = _out(d)
    c, a = d["close"], d["atr"]
    s20, s200 = d["sma20"], d["sma200"]
    up200 = s200 > s200.shift(p.get("slope_bars", 12))
    gap = (s20 - s200).abs() / s200
    narrow = gap < p.get("max_gap_pct", 0.003)
    top = np.maximum(s20, s200)
    o["signal"] = up200 & narrow & (c > top) & (c > d["open"]) & (c.shift(1) <= top.shift(1))
    o["stop"] = s200 - 0.1 * a                                   # 종가가 200이평 아래면 정리
    o["exit2"] = c < s200
    o["watch"] = up200 & narrow & (c <= top)
    o["trigger"] = top
    return o


# --------------------------------------------------------------------------- 5. 트레이더 리 세력 CCI
def trader_lee_cci(d, p):
    o = _out(d)
    c, l = d["close"], d["low"]
    mid = d["bb_mid"]
    pc = d["pcci"]
    o["signal"] = ind.crosses_above(pc, 0) & (c > mid) & (mid > mid.shift(2)) & \
        (d["volume"] >= p.get("vol_mult", 1.5) * d["vol_avg"])
    o["stop"] = np.minimum(d["bb_lo"], l.rolling(6).min())      # 안전용 손절
    o["exit2"] = (pc < 0) & (c < mid)                            # 세력CCI·캔들이 함께 무너지면 정리
    return o


# --------------------------------------------------------------------------- 6. 로스 카메론 RSI 쌍바닥
def ross_rsi_double_bottom(d, p):
    o = _out(d)
    c, l, a = d["close"], d["low"], d["atr"]
    lo = d["bb_lo"]
    first = (d["rsi"] < 30) & (c < lo)                           # 1·2단계: RSI<30 + 하단밴드 밖 마감
    W = p.get("window", 30)
    first_recent = first.shift(2).rolling(W).max() > 0
    first_low = l.where(first).shift(2).rolling(W, min_periods=1).min()
    second_inside = (c > lo) & (c.shift(1) > lo.shift(1))         # 4단계: 두 번째 바닥은 밴드 안
    near = l.rolling(4).min() <= first_low + 1.5 * a
    bounced = d["high"].shift(3).rolling(12, min_periods=1).max() >= first_low + 1.0 * a   # 1차 바닥 후 반등이 있었어야 '두 번째' 바닥
    not_still_first = ~(first | first.shift(1).fillna(False).astype(bool))
    o["signal"] = first_recent & ind.bullish_engulfing(d) & second_inside & near & bounced & not_still_first
    o["stop"] = l.rolling(4).min() - 0.05 * a                    # 두 번째 바닥 아래
    _rmult(o, d, r2=p.get("tp_r", 3.0))                           # 목표 = 손절폭 x3
    return o


# --------------------------------------------------------------------------- 7. 존 볼린저 밴드 돌파 (MFI)
def bollinger_mfi(d, p):
    o = _out(d)
    c, l = d["close"], d["low"]
    brk = (c > d["bb_up"]) & (d["mfi"] >= p.get("mfi_min", 85))
    second = brk & (brk.shift(1).rolling(10).max() > 0)          # 첫 신호는 예비, 10봉 안 두 번째에 진입
    o["signal"] = second
    o["stop"] = l.shift(1).rolling(5).min()                      # 직전 5봉 최저가
    o["exit2"] = c < d["bb_mid"]                                  # 중심선 아래 마감 → 정리
    o["watch"] = brk & ~second
    o["trigger"] = d["bb_up"]
    return o


# --------------------------------------------------------------------------- 8. 데이비드 폴 클린 MACD
def david_paul_clean_macd(d, p):
    o = _out(d)
    line, hist = d["macd"], d["macd_hist"]
    band = p.get("band_k", 0.5) * line.rolling(150, min_periods=60).std()
    out_up = line > band
    darker = (hist > 0) & (hist > hist.shift(1))
    fresh = out_up & ~out_up.shift(1).fillna(False).astype(bool) & darker
    reentry = out_up & darker & (hist.shift(1) < hist.shift(2))   # 옅어졌다 다시 짙어진 첫 막대
    o["signal"] = (fresh | reentry) & d["htf_up"]
    o["stop"] = d["low"].rolling(8).min()                         # 바로 앞 저점
    _rmult(o, d, r2=p.get("tp_r", 1.0))                            # 영상: 고정 2%(4h) → 5분봉은 1R
    o["watch"] = (line.abs() <= band)
    return o


# --------------------------------------------------------------------------- 9. 테스타 25이평 눌림목
def testa_pullback(d, p):
    o = _out(d)
    c, l, a = d["close"], d["low"], d["atr"]
    s5, s25, s150 = d["sma5"], d["sma25"], d["sma150"]
    trend = (s25 > s150) & (s150 > s150.shift(12)) & (c > s150)
    dipped = (c.shift(1) < s25.shift(1)).rolling(6).max() > 0
    o["signal"] = trend & dipped & ind.crosses_above(c, s25) & (c > d["open"]) & d["htf_up"]
    o["stop"] = l.rolling(8).min() - 0.2 * a                      # 넉넉한 손절
    _rmult(o, d, r1=p.get("tp1_r", 1.0))                           # 손절 거리만큼 위에서 절반
    o["exit2"] = c < s25
    o["watch"] = trend & (c < s25) & (c > s25 - 0.8 * a)
    o["trigger"] = s25
    return o


# --------------------------------------------------------------------------- 10. 하락 추세선 돌파
def trendline_break(d, p):
    o = _out(d)
    h, c, l = d["high"].values, d["close"].values, d["low"].values
    n, k = len(d), p.get("pivot", 3)
    piv = np.zeros(n, bool)
    for i in range(k, n - k):
        if h[i] == h[i - k:i + k + 1].max():
            piv[i] = True
    line = np.full(n, NAN)
    last = []
    for i in range(n):
        j = i - k                                                 # 피벗은 k봉 뒤에 확정
        if j >= 0 and piv[j]:
            last.append(j)
            last = last[-2:]
        if len(last) == 2 and h[last[1]] < h[last[0]]:
            x1, x2 = last
            slope = (h[x2] - h[x1]) / (x2 - x1)
            line[i] = h[x2] + slope * (i - x2)
    line = pd.Series(line, index=d.index)
    hm = d["hma55"]
    o["signal"] = ind.crosses_above(d["close"], line) & (d["close"] > d["open"]) & ~d["htf_down"]
    o["stop"] = d["low"].rolling(10).min()
    _rmult(o, d, r1=1.5)
    o["exit2"] = hm < hm.shift(1)
    o["watch"] = (d["close"] < line) & (d["close"] > line - 0.6 * d["atr"])
    o["trigger"] = line
    return o


# --------------------------------------------------------------------------- 11. 훌 이동평균선 색 전환
def hull_color(d, p):
    o = _out(d)
    hm = d["hma55"]
    mint = hm > hm.shift(1)
    o["signal"] = mint & ~mint.shift(1).fillna(False).astype(bool) & (d["close"] > hm) & ~d["htf_down"]
    o["stop"] = d["low"].rolling(10).min()                         # 바로 전 저점 아래
    _rmult(o, d, r1=1.5)                                            # 1.5배에서 절반
    o["exit2"] = ~mint                                              # 분홍으로 바뀌면 나머지
    return o


# --------------------------------------------------------------------------- 12. 피보나치 골든 존
def fib_golden_zone(d, p):
    o = _out(d)
    h, l, c, op, a = (d[x].values for x in ["high", "low", "close", "open", "atr"])
    n, W = len(d), p.get("window", 72)
    sig = np.zeros(n, bool)
    stop = np.full(n, NAN)
    tp = np.full(n, NAN)
    for i in range(W, n):
        seg_h = h[i - W:i - 1]
        hi = int(np.argmax(seg_h)) + i - W
        if hi > i - 3:                                            # 고점 확정 후 되돌림
            continue
        lo = int(np.argmin(l[i - W:hi + 1])) + i - W
        H, L = h[hi], l[lo]
        if lo >= hi or H - L < 3 * a[i]:
            continue
        if l[hi + 1:i].min() < L if hi + 1 < i else False:       # 이미 저점 이탈
            continue
        z_top, z_bot = H - 0.5 * (H - L), H - 0.618 * (H - L)
        if l[i] <= z_top and c[i] >= z_bot and c[i] > op[i] and l[i] > L:
            sig[i] = True
            stop[i] = L - 0.05 * a[i]                             # 점선 시작 저점(1) 아래
            tp[i] = H                                             # 점선 끝 고점(0)
    o["signal"] = pd.Series(sig, index=d.index) & d["htf_up"]
    o["stop"] = stop
    o["tp2"] = tp
    return o


# --------------------------------------------------------------------------- 13. 니콜라스 다바스 박스
def darvas_box(d, p):
    o = _out(d)
    W = p.get("box_len", 20)
    c, h, l, a = d["close"], d["high"], d["low"], d["atr"]
    ceil = h.shift(1).rolling(W).max()
    floor = l.shift(1).rolling(W).min()
    hv = h.shift(1).to_numpy(dtype=float)
    age = np.full(len(hv), np.nan)                                   # 천장이 생긴 지 몇 봉
    if len(hv) > W:
        win = np.lib.stride_tricks.sliding_window_view(np.nan_to_num(hv, nan=-np.inf), W)
        age[W - 1:] = W - 1 - np.argmax(win, axis=1)
    age = pd.Series(age, index=d.index)
    sideways = (ceil - floor) <= p.get("box_atr", 4.0) * a
    setup = sideways & (age >= 3) & d["htf_up"]
    o["signal"] = setup & (c > ceil)                               # 몸통이 천장 위에서 마감
    o["stop"] = ceil - 0.25 * a                                    # 천장 바로 아래
    o["trail"] = l.shift(1).rolling(12).min()                      # 새 박스 바닥으로 손절 올림
    o["watch"] = setup & (c <= ceil) & (c >= ceil - 0.6 * a)
    o["trigger"] = ceil
    return o


# --------------------------------------------------------------------------- 14. VWAP 1:3 (월가 1등)
def vwap_1to3(d, p):
    o = _out(d)
    vw = d["vwap"]
    up = vw > vw.shift(1)
    was_down = (~up.shift(1).fillna(True).astype(bool)).rolling(p.get("down_bars", 4)).min() > 0
    turn = up & was_down
    o["signal"] = turn & (d["close"] > vw)
    o["stop"] = d["low"] - 0.05 * d["atr"]                         # 꺾인 봉 저가
    _rmult(o, d, r1=p.get("tp1_r", 3.0))                            # 3배에서 절반
    o["exit2"] = ~up                                                # 선이 다시 꺾이면 나머지
    return o


# --------------------------------------------------------------------------- 15. 캐스퍼 미국장 개장 5분 돌파 (1분봉)
def orb_open_5min(d1, p):
    """d1: 1분봉. 9:30 ET 첫 5분(꼬리 기준) 고점 위로 1분봉 몸통 마감 → FVG 생기면 진입. 하루 1회."""
    o = _out(d1)
    if d1.empty:
        return o
    et = d1.index.tz_convert("America/New_York")
    mins = et.hour * 60 + et.minute
    day = et.date
    h, l, c, op = d1["high"].values, d1["low"].values, d1["close"].values, d1["open"].values
    sig = np.zeros(len(d1), bool)
    stop = np.full(len(d1), NAN)
    tp = np.full(len(d1), NAN)
    end_min = p.get("window_end_min", 11 * 60)
    for dd in sorted(set(day)):
        if pd.Timestamp(dd).weekday() >= 5:
            continue
        idx = np.where(day == dd)[0]
        first5 = [i for i in idx if 570 <= mins[i] < 575]
        if len(first5) < 5:
            continue
        top = max(h[i] for i in first5)
        after = [i for i in idx if 575 <= mins[i] < end_min]
        cross_i = None
        for i in after:
            if cross_i is None:
                if c[i] > top and c[i] > op[i]:
                    cross_i = i                                    # 몸통이 선 밖에서 마감한 첫 봉
                continue
            # FVG: i-2 고가 < i 저가 (세 번째 캔들이 빈칸 완성)
            if i - 2 >= cross_i - 1 and l[i] > h[i - 2] and c[i] > top:
                sig[i] = True
                stop[i] = l[cross_i]                               # 선을 처음 넘은 캔들 저가
                tp[i] = c[i] + p.get("tp_r", 2.0) * (c[i] - stop[i])
                break
            if i - cross_i > 10:
                break
    o["signal"] = sig
    o["stop"] = stop
    o["tp2"] = tp
    return o


# --------------------------------------------------------------------------- 레지스트리
# family: breakout / pullback / momentum / reversal  (시장 국면 적합도 계산용)
STRATEGIES = {
    "qullamaggie_box":   dict(fn=qullamaggie_box, name="쿨라메기 박스 돌파", family="breakout",
                              video="qWAdlQ7cW6o", rule="10/20/50 SMA 정배열 → 20이평 아래 눌림 박스 → 박스 평균 거래량 2배 돌파. 10이평 이탈 절반, 20이평 이탈 나머지"),
    "adam_khoo":         dict(fn=adam_khoo, name="아담 쿠 4단계 돌파", family="breakout",
                              video="8alpPJ4n_ao", rule="정배열 → 10이평 아래 눌림(50이평 유지) → 상자 → 몸통 돌파. 손절 상자 아래"),
    "hma_retest":        dict(fn=hma_retest, name="알렉스 HMA 리테스트", family="pullback",
                              video="uyy3_yqlKkU", rule="HMA100 위 마감(회색점) 후 HMA가 파랑으로 바뀔 때 진입, 직전 저점 손절, 분홍 전환 시 정리"),
    "emmanuel_squeeze":  dict(fn=emmanuel_squeeze, name="임마누엘 20/200 스퀴즈", family="breakout",
                              video="aDcyqOcPJxM", rule="200이평 상승 + 20·200 간격 좁음 + 두 선 위로 양봉 마감. 200이평 아래 마감 시 정리"),
    "trader_lee_cci":    dict(fn=trader_lee_cci, name="트레이더 리 세력 CCI", family="momentum",
                              video="tQadAOEVbFA", rule="세력CCI 0선 상향 + 볼린저 중심선 위 마감 + 중심선 상승 + 거래량 1.5배"),
    "ross_rsi_db":       dict(fn=ross_rsi_double_bottom, name="로스 카메론 RSI 쌍바닥", family="reversal",
                              video="lxz7Csyg9bk", rule="RSI<30 & 하단밴드 밖 1차 바닥 → 밴드 안 2차 바닥 + 상승장악형. 목표 3R"),
    "bollinger_mfi":     dict(fn=bollinger_mfi, name="존 볼린저 MFI 돌파", family="breakout",
                              video="J3Znau4RqSo", rule="상단밴드 위 마감 + MFI≥85, 첫 신호는 예비 → 10봉 안 두 번째 신호 진입. 중심선 이탈 정리"),
    "david_paul_macd":   dict(fn=david_paul_clean_macd, name="데이비드 폴 클린 MACD", family="momentum",
                              video="yWQ4aZgb_ik", rule="MACD선이 0 근처 회색띠 위로 이탈 + 막대 짙어짐, 눌림 재진입 포함. 고정 목표"),
    "testa_pullback":    dict(fn=testa_pullback, name="테스타 25이평 눌림목", family="pullback",
                              video="q7gyeDtLxEI", rule="5/25/150 정배열, 25이평 아래 눌림 후 양봉이 25이평 위 마감. 1R 절반+본전 손절, 25이평 이탈 정리"),
    "trendline_break":   dict(fn=trendline_break, name="하락 추세선 돌파", family="momentum",
                              video="jmkcgrBT8_M", rule="피벗 고점 2개로 그은 하락 추세선을 종가로 돌파"),
    "hull_color":        dict(fn=hull_color, name="훌 이동평균선 색 전환", family="pullback",
                              video="jmkcgrBT8_M", rule="HMA 분홍→민트 전환, 직전 저점 손절, 1.5R 절반, 다시 분홍이면 정리"),
    "fib_golden":        dict(fn=fib_golden_zone, name="피보나치 골든 존", family="pullback",
                              video="jmkcgrBT8_M", rule="전 고점 돌파 후 저점→고점 피보나치 0.5~0.618 반등 양봉. 손절 저점, 익절 고점"),
    "darvas_box":        dict(fn=darvas_box, name="다바스 박스", family="breakout",
                              video="9WkLLf0q_ug", rule="옆으로 기는 박스 천장 위 마감 매수, 천장 바로 아래 손절, 새 박스 바닥으로 손절 상향"),
    "vwap_1to3":         dict(fn=vwap_1to3, name="VWAP 1:3 (월가 1등)", family="reversal",
                              video="mYTYGP-yNWg", rule="VWAP이 내려가다 위로 꺾이면 진입, 꺾인 봉 저가 손절, 3R 절반, 다시 꺾이면 정리"),
    "orb_open":          dict(fn=orb_open_5min, name="캐스퍼 개장 5분 돌파", family="breakout", timeframe="1m",
                              video="pMPURUUJvDE", rule="9:30 ET 첫 5분 고점 위 1분봉 몸통 마감 + FVG → 진입, 돌파봉 저가 손절, 2R 익절. 평일 하루 1회"),
}
