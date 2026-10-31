#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
event_study.py — 事件研究、路徑模擬、隨機基準、破位分類、成交分布。

這是本 skill 的核心。所有「勝率 / 賠率 / 期望值」都從這裡出來。

CLI:
    python event_study.py 01164.json --events A,G --stops -9,-12,-15 --targets 12,16 --cost 0.75
    python event_study.py 01164.json --breaks
    python event_study.py 01164.json --profile

import:
    from event_study import build_events, forward_stats, path_matrix, baseline_path, touch_bootstrap
"""

import json
import statistics as st
import sys

from indicators import compute_all

# ---------------------------------------------------------------- 事件定義
# 全部以「當日收盤」為判定基準，避免用未來資訊。
EVENT_SPECS = {
    # A 廣義超賣：樣本最大，通常用來證明「超賣沒 Alpha」
    "A": lambda I, i: I["rsi6"][i] is not None and I["rsi6"][i] < 30
                     and I["ma60"][i] is not None and I["close"][i] < I["ma60"][i],
    # B 極端超賣：跌破布林下軌
    "B": lambda I, i: I["rsi6"][i] is not None and I["rsi6"][i] < 30
                     and I["ma60"][i] is not None and I["close"][i] < I["ma60"][i]
                     and I["boll_dn"][i] is not None and I["close"][i] <= I["boll_dn"][i],
    # N 貼近前低（未破）
    "N": lambda I, i: I["prior_low"][i] is not None
                     and 0 <= I["low"][i] / I["prior_low"][i] - 1 <= 0.05
                     and I["rsi6"][i] is not None and I["rsi6"][i] < 35,
    # F 真破位：收盤創 250 日新低
    "F": lambda I, i: I["prior_low"][i] is not None and I["close"][i] < I["prior_low"][i],
    # G 假破位：盤中破前低、收盤收回
    "G": lambda I, i: I["prior_low"][i] is not None
                     and I["low"][i] < I["prior_low"][i] < I["close"][i],
    # I 長下影探前低
    "I": lambda I, i: (I["high"][i] - I["low"][i]) > 0
                     and (I["close"][i] - I["low"][i]) / (I["high"][i] - I["low"][i]) > 0.5
                     and I["close"][i] > I["open"][i]
                     and I["prior_low"][i] is not None
                     and I["low"][i] <= I["prior_low"][i] * 1.04,
    # H 深度回撤
    "H": lambda I, i: I["prior_high"][i] is not None and I["prior_high"][i] > 0
                     and I["close"][i] / I["prior_high"][i] - 1 <= -0.40
                     and I["rsi6"][i] is not None and I["rsi6"][i] < 32
                     and I["ma60"][i] is not None and I["close"][i] < I["ma60"][i],
    # S 極度超賣（不含均線條件，用於鋰礦等單邊下跌板塊）
    "S": lambda I, i: I["rsi6"][i] is not None and I["rsi6"][i] < 15,
    # D 深度回撤但無均線/RSI 條件（週期股專用）
    "D": lambda I, i: I["prior_high"][i] is not None and I["prior_high"][i] > 0
                     and I["close"][i] / I["prior_high"][i] - 1 <= -0.50,
}

EVENT_NAMES = {
    "A": "廣義超賣 RSI6<30 且 close<MA60",
    "B": "極端超賣 且破布林下軌",
    "N": "貼近前低 ≤5% 且 RSI6<35",
    "F": "真破位 close 創 250 日新低",
    "G": "假破位 盤中破前低、收盤收回",
    "I": "長下影探前低",
    "H": "深度回撤 ≥40% 且超賣",
    "S": "極度超賣 RSI6<15",
    "D": "較 252 日高回撤 ≥50%",
}


def build_events(I, kind, cooldown=10, start=252, end=None):
    """產生事件索引。**必須去重**：同一輪下跌會逐日重複觸發，不去重會虛增樣本約 24%。"""
    cond = EVENT_SPECS[kind]
    n = len(I["close"])
    end = n if end is None else end
    ev = []
    for i in range(start, end):
        if cond(I, i) and (not ev or i - ev[-1] > cooldown):
            ev.append(i)
    return ev


def _fwd(rows, i, H):
    c = rows[i][2]
    if c <= 0 or i + H >= len(rows):
        return None
    return (rows[i + H][2] / c - 1) * 100


def _mfe_mae(rows, i, H):
    c = rows[i][2]
    seg = rows[i + 1:i + H + 1]
    if not seg or c <= 0:
        return None, None
    return (max(r[3] for r in seg) / c - 1) * 100, (min(r[4] for r in seg) / c - 1) * 100


def _q(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    f = int(k)
    if f + 1 < len(xs):
        return xs[f] + (xs[f + 1] - xs[f]) * (k - f)
    return xs[f]


def forward_stats(rows, idxs, horizons=(5, 10, 20, 40)):
    """前向報酬分布。必須同時報 P25 / 中位 / P75——這類分布右偏嚴重，均值會騙人。"""
    out = {}
    for H in horizons:
        xs, mfes, maes = [], [], []
        for i in idxs:
            r = _fwd(rows, i, H)
            if r is None:
                continue
            mfe, mae = _mfe_mae(rows, i, H)
            xs.append(r)
            mfes.append(mfe)
            maes.append(mae)
        if not xs:
            continue
        out[H] = {
            "n": len(xs),
            "mean": st.mean(xs),
            "median": st.median(xs),
            "p25": _q(xs, 0.25),
            "p75": _q(xs, 0.75),
            "win": sum(1 for x in xs if x > 0) / len(xs) * 100,
            "mfe_med": st.median(mfes),
            "mae_med": st.median(maes),
            "mae_p25": _q(maes, 0.25),
        }
    return out


def path_stats(rows, idxs, up_pct, dn_pct, H=20, cost=0.75):
    """路徑模擬：止損與目標誰先觸及。

    ⚠️ 基準一律是「各事件自身的收盤價」，套用百分比障礙。
       絕不可把進場價寫死成常數——歷史事件價位從 1.4 到 5.6 都有，寫死會讓障礙完全失效。
    ⚠️ 同日同時觸及目標與止損 → 保守判為止損。
    """
    pnl, win, lose, open_ = [], 0, 0, 0
    for i in idxs:
        if i + H >= len(rows):
            continue
        b = rows[i][2]
        if b <= 0:
            continue
        u, d = b * (1 + up_pct / 100), b * (1 + dn_pct / 100)
        hit = None
        for j in range(i + 1, i + H + 1):
            if rows[j][4] <= d:
                hit = "dn"
                break
            if rows[j][3] >= u:
                hit = "up"
                break
        if hit == "up":
            win += 1
            pnl.append(up_pct - cost)
        elif hit == "dn":
            lose += 1
            pnl.append(dn_pct - cost)
        else:
            open_ += 1
            pnl.append((rows[i + H][2] / b - 1) * 100 - cost)
    total = win + lose + open_
    if not total:
        return None
    return {
        "n": total,
        "p_win": win / total * 100,
        "p_lose": lose / total * 100,
        "p_open": open_ / total * 100,
        "exp_net": st.mean(pnl),
    }


def path_matrix(rows, idxs, stops=(-5, -7, -9, -12, -15, -18),
                targets=(8, 12, 16, 20, 25), H=20, cost=0.75):
    """止損 × 目標 的淨期望矩陣。回傳 {(stop,target): stats}。"""
    return {(s, t): path_stats(rows, idxs, t, s, H, cost)
            for s in stops for t in targets}


def baseline_indices(n, step=20, start=252, H=20):
    """隨機進場基準：每 step 個交易日無條件進場一次，代表「什麼都不看」。"""
    return [i for i in range(start, n - H, step)]


def touch_bootstrap(rows, pcts_up=(3, 5, 8), pcts_dn=(-3, -5, -8), H=20,
                    start=252, step=1):
    """從「現在」出發，H 日內曾觸及某幅度的機率（歷史 bootstrap）。

    對歷史上每一天，量測其後 H 日的 MFE / MAE，統計超過門檻的比例。
    這是分佈推導值，不是對未來的承諾。
    """
    ups = {p: 0 for p in pcts_up}
    dns = {p: 0 for p in pcts_dn}
    n = 0
    for i in range(start, len(rows) - H, step):
        mfe, mae = _mfe_mae(rows, i, H)
        if mfe is None:
            continue
        n += 1
        for p in pcts_up:
            if mfe >= p:
                ups[p] += 1
        for p in pcts_dn:
            if mae <= p:
                dns[p] += 1
    return {
        "n": n,
        "up": {p: (c / n * 100 if n else None) for p, c in ups.items()},
        "dn": {p: (c / n * 100 if n else None) for p, c in dns.items()},
    }


def classify_breaks(I, start=252):
    """把「跌破前低」分成真破位（F）與假破位（G）。

    真破位 = 收盤 < 前低；假破位 = 盤中破、收盤收回。兩者結論相反，不可混談。
    """
    out = []
    for i in range(start, len(I["close"])):
        pl = I["prior_low"][i]
        if pl is None:
            continue
        low, close, high = I["low"][i], I["close"][i], I["high"][i]
        if low < pl and close > pl:
            rng = high - low
            out.append({
                "i": i, "date": I["dates"][i], "type": "fake",
                "prior_low": pl, "low": low, "close": close,
                "shadow": (close - low) / rng * 100 if rng > 0 else None,
                "rsi6": I["rsi6"][i],
            })
        elif close < pl:
            out.append({
                "i": i, "date": I["dates"][i], "type": "real",
                "prior_low": pl, "low": low, "close": close,
                "shadow": None, "rsi6": I["rsi6"][i],
            })
    return out


def break_sequence(breaks, gap=30):
    """把破位分組成序列，標上「第 N 次」。用於回答「跌這麼多次該近了嗎」。"""
    seq = 0
    prev = None
    for b in breaks:
        if prev is None or b["i"] - prev > gap:
            seq = 1
        else:
            seq += 1
        b["seq"] = seq
        prev = b["i"]
    return breaks


def seq_stats(rows, breaks, H=20):
    """按序列序號分桶，看 T+H 報酬。實測呈倒 U 型（第 1 次最差、第 4–5 次最好）。"""
    buckets = {}
    for b in breaks:
        s = b.get("seq")
        if s is None:
            continue
        key = "8+" if s >= 8 else str(s)
        r = _fwd(rows, b["i"], H)
        if r is None:
            continue
        buckets.setdefault(key, []).append(r)
    order = ["1", "2", "3", "4", "5", "6", "7", "8+"]
    out = {}
    for k in order:
        xs = buckets.get(k)
        if not xs:
            continue
        out[k] = {
            "n": len(xs), "mean": st.mean(xs), "median": st.median(xs),
            "win": sum(1 for x in xs if x > 0) / len(xs) * 100,
        }
    return out


def year_split(rows, idxs, H=20):
    """分年勝率。合併多年樣本時**必做**，否則早年牛市會拉高整體數字。"""
    buckets = {}
    for i in idxs:
        r = _fwd(rows, i, H)
        if r is None:
            continue
        buckets.setdefault(rows[i][0][:4], []).append(r)
    return {
        y: {
            "n": len(xs), "mean": st.mean(xs), "median": st.median(xs),
            "win": sum(1 for x in xs if x > 0) / len(xs) * 100,
        }
        for y, xs in sorted(buckets.items())
    }


def volume_profile(rows, buckets=24, lookback=250):
    """成交量按價格分桶（近 lookback 根）。

    看的不是「哪個價位量最大」，而是：現價上方堆了多少（解套壓力）、下方是否稀薄（流動性真空）。
    """
    seg = rows[-lookback:] if lookback and len(rows) > lookback else rows
    if not seg:
        return {"buckets": [], "above_pct": None, "below_pct": None}
    prices = [(r[3] + r[4]) / 2 for r in seg]
    lo, hi = min(prices), max(prices)
    if hi <= lo:
        return {"buckets": [], "above_pct": None, "below_pct": None}
    width = (hi - lo) / buckets
    vols = [0.0] * buckets
    for r, p in zip(seg, prices):
        k = min(buckets - 1, int((p - lo) / width))
        vols[k] += r[5]
    total = sum(vols) or 1.0
    price = seg[-1][2]
    above = sum(v for v, r in zip(vols, range(buckets))
                if lo + (r + 0.5) * width > price)
    below = sum(v for v, r in zip(vols, range(buckets))
                if lo + (r + 0.5) * width < price)
    return {
        "buckets": [
            {"low": lo + k * width, "high": lo + (k + 1) * width,
             "vol": vols[k], "share": vols[k] / total * 100}
            for k in range(buckets)
        ],
        "above_pct": above / total * 100,
        "below_pct": below / total * 100,
        "price_range": (lo, hi),
    }


def summary_table(rows, I, kinds=("A", "N", "F", "G", "I", "H"), H=(10, 20, 40), cooldown=10):
    """印出多事件對照表，並附隨機基準。"""
    print(f"{'事件':<28}{'n':>5}{'T+20均值':>10}{'T+20中位':>10}{'勝率':>8}{'MAE中位':>9}")
    print("-" * 72)
    for k in kinds:
        ev = build_events(I, k, cooldown)
        if not ev:
            print(f"{k} {EVENT_NAMES.get(k,''):<26}{0:>5}")
            continue
        fs = forward_stats(rows, ev, H)
        s = fs.get(20)
        if not s:
            continue
        print(f"{k} {EVENT_NAMES.get(k,''):<26}{s['n']:>5}{s['mean']:>10.2f}{s['median']:>10.2f}"
              f"{s['win']:>7.1f}%{s['mae_med']:>9.2f}")
    base = baseline_indices(len(rows), 20)
    bs = forward_stats(rows, base, H).get(20)
    if bs:
        print("-" * 72)
        print(f"{'隨機基準（每20日進場）':<26}{bs['n']:>5}{bs['mean']:>10.2f}"
              f"{bs['median']:>10.2f}{bs['win']:>7.1f}%{'-':>9}")


def _cli(argv):
    if not argv:
        print(__doc__)
        return 1
    rows = json.load(open(argv[0], encoding="utf-8"))
    I = compute_all(rows)
    args = argv[1:]

    if "--breaks" in args:
        bs = break_sequence(classify_breaks(I))
        recent = [b for b in bs if b["i"] >= len(rows) - 120]
        fake = sum(1 for b in recent if b["type"] == "fake")
        real = sum(1 for b in recent if b["type"] == "real")
        print(f"近 120 交易日：假破位 {fake} 次、真破位 {real} 次")
        for b in recent:
            print(f"  {b['date']} {b['type']:4} 前低 {b['prior_low']:.3f} "
                  f"低 {b['low']:.3f} 收 {b['close']:.3f} seq={b['seq']}")
        print("\n序號分桶（T+20）:")
        for k, v in seq_stats(rows, bs).items():
            print(f"  第{k:>2}次 n={v['n']:>3} 均值 {v['mean']:+.2f}% 中位 {v['median']:+.2f}% 勝率 {v['win']:.1f}%")
        return 0

    if "--profile" in args:
        p = volume_profile(rows)
        print(f"現價 {rows[-1][2]:.3f}；上方量佔 {p['above_pct']:.1f}%，下方 {p['below_pct']:.1f}%")
        for b in p["buckets"]:
            bar = "█" * int(b["share"] / 2)
            print(f"  {b['low']:7.2f}-{b['high']:7.2f} {b['share']:5.1f}% {bar}")
        return 0

    kinds = ["A", "N", "F", "G", "I", "H"]
    for a in args:
        if a.startswith("--events="):
            kinds = [x.strip() for x in a.split("=", 1)[1].split(",")]
    summary_table(rows, I, kinds)

    stops, targets, cost = (-9, -12, -15), (12, 16), 0.75
    for a in args:
        if a.startswith("--stops="):
            stops = tuple(float(x) for x in a.split("=", 1)[1].split(","))
        elif a.startswith("--targets="):
            targets = tuple(float(x) for x in a.split("=", 1)[1].split(","))
        elif a.startswith("--cost="):
            cost = float(a.split("=", 1)[1])

    for k in kinds[:2]:
        ev = build_events(I, k)
        if not ev:
            continue
        print(f"\n路徑矩陣 [{k}] n={len(ev)}  成本 {cost}%")
        for s in stops:
            line = f"  止損{s:>5.0f}% "
            for t in targets:
                r = path_stats(rows, ev, t, s, 20, cost)
                line += f"| 目標+{t:.0f}% 勝{r['p_win']:4.0f}% 淨{r['exp_net']:+6.2f}% " if r else ""
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
