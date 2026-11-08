"""대시보드 없이 터미널에서 한 번만 스캔:  python3 scan.py"""
from scanner import Scanner


def main():
    s = Scanner()
    s.scan()
    r = s.result
    if not r:
        print("스캔 실패:", s.status.get("error"))
        return
    g = r["regime"]
    print(f"\n시장 국면: {g['label']} | 변동성 {g['vol_state']} ({g['vol_ratio']:.2f}x) | 1h 상승추세 코인 비율 {g['breadth']:.0f}%")
    if g.get("warning"):
        print("  ⚠", g["warning"])
    print(f"\n▶ 지금 매수 후보: {r['verdict']}")
    for k, x in enumerate(r["picks"], 1):
        tp = x["tp2"] or x["tp1"]
        rr = f"1:{x['rr_net']:.1f}" if x.get("rr_net") is not None else "-"
        print(f"  {k}. [{x['grade']} {x['pick_score']:.0f}] {x['product']:<11} {x['name']}  현재 {x['price']:.6g}  "
              f"손절 {x['stop']:.6g}(-{x['risk_pct']:.2f}%)  익절 {f'{tp:.6g}' if tp else '규칙청산'}  손익비 {rr}  권장 ${x['size_usd']:.0f}")
        for w in x["why"]:
            print("       +", w)
        for w in x["risks"]:
            print("       !", w)
    print(f"\n오늘의 전략 순위 (최근 {r['backtest']['days']}일, 상위 {len(r['backtest']['coins'])}개 코인, 수수료 {r['config']['fee']}%/회 반영)")
    for s_ in r["strategies"][:8]:
        a, d1 = s_["bt"]["all"], s_["bt"]["d1"]
        print(f"  {s_['rank']:>2}. {s_['name']:<18} 점수 {s_['final']:+.3f} | {a['n']:>3}회 승률 {a['win']:4.0f}% 평균 {a['avg']:+.2f}% | 24h {d1['n']}회 {d1['sum']:+.2f}% | 국면적합 {s_['fit']:+.1f}")
    print(f"\n지금 신호 ({len(r['signals'])}개, 상위 15)")
    for x in r["signals"][:15]:
        tp = x["tp2"] or x["tp1"]
        tp_txt = f"익절 {tp:.6g}" if tp else "익절: 규칙 청산"
        print(f"  {x['product']:<12} {x['name']:<18} 현재 {x['price']:.6g}  손절 {x['stop']:.6g} (-{x['risk_pct']:.2f}%)  "
              f"{tp_txt}  점수 {x['score']:.0f}  {' '.join(x['notes'])}")
    print(f"\n결과 저장: results/latest.json  (소요 {r['elapsed']:.0f}초)")


if __name__ == "__main__":
    main()
