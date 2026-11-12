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
    from event_study import build_events, forward_stats, path_matrix, baseline_indices, touch_bootstrap

    （baseline_indices 產生隨機基準的進場索引，要再交給 forward_stats / path_stats
      才會得到統計值；沒有 baseline_path 這個函式。）
"""

import json
import os
import statistics as st
import sys

from indicators import compute_all


def setup_console():
    """把 stdout/stderr 切到 UTF-8（Windows cp950 console 會 UnicodeEncodeError）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# 樣本量四級（對齊 references/statistics-protocol.md 第十節）
def sample_level(n):
    if n >= 100:
        return "A", "可給勝率與期望值"
    if n >= 30:
        return "B", "可給方向與粗略勝率"
    if n >= 10:
        return "C", "只能說方向，不得給小數勝率"
    return "D", "僅供軼事，禁止用勝率下結論"


def fmt_win(n, win):
    """依樣本量決定勝率怎麼印：<10 遮蔽；10–29 只給整數。"""
    if n < 10:
        return "  —"
    if n < 30:
        return f"{win:3.0f}%"
    return f"{win:3.1f}%"

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
    # R1 反彈至 MA60 附近且動能偏強（2026-11 雙標的報告用）
    #    「反彈到壓力位」的狀態，不是「探底」。
    "R1": lambda I, i: I["ma60"][i] not in (None, 0) and I["rsi6"][i] is not None
                      and abs(I["close"][i] / I["ma60"][i] - 1) <= 0.025
                      and I["rsi6"][i] >= 60,
    # R2 自 250 日低反彈 10–30%（2026-11 雙標的報告用）
    #    看的是「已經離底多遠」，而非「離底多近」。
    "R2": lambda I, i: I["prior_low"][i] not in (None, 0)
                      and 0.10 <= I["close"][i] / I["prior_low"][i] - 1 <= 0.30,
    # WR 距 250 日低 5–15% 的弱勢盤整（2026-11 雙標的報告用）
    #    介於「貼近前低」與「已反彈一段」之間：跌不深、也彈不動的狀態。
    #    與其他事件一樣用預設 cooldown=10 去重。
    "WR": lambda I, i: I["prior_low"][i] not in (None, 0)
                       and 0.05 <= I["close"][i] / I["prior_low"][i] - 1 <= 0.15,
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
    "R1": "反彈至 MA60 ±2.5% 且 RSI6≥60",
    "R2": "自 250 日低反彈 10–30%",
    "WR": "距 250 日低 5–15%（弱勢盤整）",
}


def build_events(I, kind, cooldown=10, start=252, end=None):
    """產生事件索引。**必須去重**：同一輪下跌會逐日重複觸發，不去重會虛增樣本約 24%。"""
    cond = EVENT_SPECS.get(kind)
    if cond is None:
        # 不要讓 KeyError 裸奔：把可用清單帶進例外訊息，任何呼叫路徑都能受惠
        raise ValueError(f"未知事件 {kind}（可用：{','.join(EVENT_SPECS)}）")
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


def summary_table(rows, I, kinds=("A", "N", "F", "G", "I", "H"), H=(10, 20, 40),
                  cooldown=10, show_years=False):
    """印出多事件對照表，並附隨機基準。

    ⚠️ 這裡會執行 statistics-protocol 第十節的樣本量紀律，不是只印出來：
       n<10       → 勝率欄遮蔽為「—」，表尾列為禁止下結論
       10<=n<30   → 勝率只給整數（不給小數點）
       n>=30      → 正常顯示
    """
    print(f"{'事件':<28}{'n':>5}{'T+20均值':>10}{'T+20中位':>10}{'勝率':>8}"
          f"{'MAE中位':>9}{'級別':>6}")
    print("-" * 78)
    banned, weak = [], []
    for k in kinds:
        ev = build_events(I, k, cooldown)
        if not ev:
            print(f"{k} {EVENT_NAMES.get(k,''):<26}{0:>5}{'—':>10}{'—':>10}{'—':>8}{'—':>9}{'D':>6}")
            banned.append(k)
            continue
        fs = forward_stats(rows, ev, H)
        s = fs.get(20)
        if not s:
            continue
        lvl, _ = sample_level(s["n"])
        if s["n"] < 10:
            banned.append(k)
        elif s["n"] < 30:
            weak.append(k)
        print(f"{k} {EVENT_NAMES.get(k,''):<26}{s['n']:>5}{s['mean']:>10.2f}"
              f"{s['median']:>10.2f}{fmt_win(s['n'], s['win']):>8}"
              f"{s['mae_med']:>9.2f}{lvl:>6}")
        if show_years:
            _print_year_split(rows, ev, 20, indent="      ")

    base = baseline_indices(len(rows), 20)
    bs = forward_stats(rows, base, H).get(20)
    if bs:
        # 基準列的級別必須跟事件列走同一個 sample_level：寫死 'A' 會讓 n=7 的
        # 基準列一邊遮蔽勝率、一邊自稱 A 級（同一列自我矛盾），也讓「超額 pp」
        # 建立在一個被誤標的參考點上。
        base_lvl, _ = sample_level(bs["n"])
        print("-" * 78)
        print(f"{'隨機基準（每20日進場）':<26}{bs['n']:>5}{bs['mean']:>10.2f}"
              f"{bs['median']:>10.2f}{fmt_win(bs['n'], bs['win']):>8}{'-':>9}{base_lvl:>6}")

    print()
    if banned:
        print(f"裁決：{'/'.join(banned)} 樣本 n<10（或 n=0）→ 禁止用其勝率作為進場依據，只能當軼事。")
    if weak:
        print(f"注意：{'/'.join(weak)} 樣本 10–29 → 只能說方向，不得引用小數點勝率。")
    if not banned and not weak:
        print("樣本量：全部事件 n≥30，可比較方向與勝率（仍需做分年檢查）。")


def _print_year_split(rows, idxs, H=20, indent="  "):
    """分年表 + 「近三年」紅線。

    ⚠️ 「近三年」必須是**日曆近三年**，不是「有事件的最後三個年份」。
    舊版用 `years[-3:]` 補槽位，會在缺少年份時把更早的年份拉進來
    （實例：09696 的 D 事件 2025 年無樣本，2023 被拉進來，把 43.8% 抬成 50.0% 過關）。
    截止年取自資料最後一根 K，不用 wall clock，否則回測舊資料會失真。
    """
    ys = year_split(rows, idxs, H)
    if not ys:
        print(f"{indent}分年：無可用樣本")
        return
    parts = [f"{y} n={v['n']} 勝{fmt_win(v['n'], v['win']).strip()} 均值{v['mean']:+.1f}%"
             for y, v in ys.items()]
    print(f"{indent}分年：" + " | ".join(parts))

    cutoff = int(rows[-1][0][:4])
    cal = [str(cutoff - 2), str(cutoff - 1), str(cutoff)]
    present = [y for y in cal if y in ys]
    missing = [y for y in cal if y not in ys]
    window = "/".join(cal)
    gap = f"，其中 {'、'.join(missing)} 無事件" if missing else ""
    tot = sum(ys[y]["n"] for y in present)

    if tot == 0:
        print(f"{indent}近三年（{window}{gap}）無任何事件 → 無法判定 50% 紅線")
        return
    if tot < 10:
        # n<10 依協議第十節不得給勝率；但也不能因此看起來像「過關」
        print(f"{indent}近三年（{window}{gap}）合計 n={tot} → 樣本不足（n<10），"
              f"不得作勝率結論；⚠️ 不可視為 ≥50%")
        return
    wins = sum(ys[y]["win"] * ys[y]["n"] for y in present) / tot
    flag = "⚠️ <50%，整體數字被早年拉高" if wins < 50 else "≥50%"
    # 勝率一律走 fmt_win，與表格註記（10–29 不給小數）一致
    print(f"{indent}近三年（{window}{gap}）合計 n={tot} "
          f"勝率 {fmt_win(tot, wins).strip()} → {flag}")


def _validate_kinds(kinds):
    """過濾未知事件代碼並印出可用清單；回傳過濾後（保序、去重）的清單。"""
    ok, bad, seen = [], [], set()
    for k in kinds:
        k = k.strip()
        if not k:
            continue
        if k in EVENT_SPECS:
            if k not in seen:
                seen.add(k)
                ok.append(k)
        elif k not in bad:
            bad.append(k)
    for k in bad:
        print(f"跳過未知事件 {k}（可用：{','.join(EVENT_SPECS)}）")
    return ok


def _opt(args, name, default=None):
    """取 --name=value 或 --name value。"""
    for i, a in enumerate(args):
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
        if a == name and i + 1 < len(args) and not args[i + 1].startswith("--"):
            return args[i + 1]
    return default


def _has(args, name):
    return any(a == name or a.startswith(name + "=") for a in args)


def _print_matrix(rows, I, k, stops, targets, cost, H=20, cooldown=10):
    ev = build_events(I, k, cooldown)
    if not ev:
        print(f"\n路徑矩陣 [{k}]：無事件")
        return
    usable = [i for i in ev if i + H < len(rows)]
    if not usable:
        print(f"\n路徑矩陣 [{k}]：事件 {len(ev)} 個，但全部距序列末端不足 {H} 日，無法模擬")
        return
    base = baseline_indices(len(rows), 20)
    print(f"\n路徑矩陣 [{k}] 事件 n={len(ev)} / 可用 n={len(usable)}  成本 {cost}%")
    print("  （每格附同參數隨機基準的超額 pp；超額 ≤0 代表訊號不優於『什麼都不看』）")
    excesses, nets = [], []
    for s in stops:
        line = f"  止損{s:>5.0f}% "
        for t in targets:
            r = path_stats(rows, usable, t, s, H, cost)
            b = path_stats(rows, base, t, s, H, cost)
            if not r:
                continue
            ex = r["exp_net"] - b["exp_net"] if b else None
            excesses.append((ex, s, t))
            nets.append((r["exp_net"], s, t))
            ex_s = f"{ex:+6.2f}" if ex is not None else "   n/a"
            line += (f"| 目標+{t:.0f}% 勝{fmt_win(r['n'], r['p_win'])}"
                     f" 淨{r['exp_net']:+6.2f}% 超{ex_s}pp ")
        print(line)
    if excesses:
        best = max(excesses, key=lambda x: (x[0] if x[0] is not None else -9e9))
        pos = sum(1 for e, _, _ in excesses if e is not None and e > 0)
        worst_net = min(n for n, _, _ in nets)
        if best[0] is None:
            verdict = "無基準可比，不得下結論"
        elif best[0] <= 0:
            verdict = "無 Alpha——全部格位未勝過同參數隨機基準"
        elif worst_net <= 0:
            verdict = (f"部分格位優於基準（{pos}/{len(excesses)}），但存在負期望格"
                       f"（最低 {worst_net:+.2f}%），不可整體宣稱正期望")
        else:
            verdict = f"有超額（{pos}/{len(excesses)} 格為正），最佳 +{best[0]:.2f}pp"
        print(f"  裁決：{verdict}")
        if len(usable) < 30:
            print(f"  ⚠️ 可用樣本 n={len(usable)}（<30）：以上數字只能說方向，且必須做冷卻期敏感度檢查"
                  f"（加 --cooldown-sweep=5,10,20）。")
            _print_year_split(rows, usable, H, indent="  ")


def _cooldown_sweep(rows, I, k, cds, stop, target, cost, H=20):
    print(f"\n冷卻期敏感度 [{k}]  止損 {stop:+.0f}% / 目標 +{target:.0f}% / T+{H}")
    # 預設只掃「−9% / +12%」一格，與報告常用的 3×3 九格（10/15/20）不同。
    # 不提示的話，讀者會拿 −0.73% 去對報告的 +0.11%，以為揭露寫錯。
    print(f"  （目前只掃 {stop:+.0f}% / {target:+.0f}% 這一格；"
          f"要對齊報告九格請加 --stops=-9,-12,-15 --targets=10,15,20）")
    for cd in cds:
        ev = build_events(I, k, cd)
        r = path_stats(rows, ev, target, stop, H, cost)
        if not r:
            print(f"  cooldown={cd:>3}  n=0")
            continue
        print(f"  cooldown={cd:>3}  n={r['n']:>3}  勝{fmt_win(r['n'], r['p_win'])}  "
              f"淨{r['exp_net']:+.2f}%")


def _cli(argv):
    setup_console()
    if not argv:
        print(__doc__)
        return 1
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    if not os.path.isfile(argv[0]):
        print(f"錯誤：找不到檔案 {argv[0]}\n"
              f"第一個參數必須是 market_data.py 產出的日 K JSON。先抓資料：\n"
              f"  python market_data.py hk01164 --start 2015-01-01 --out 01164.json",
              file=sys.stderr)
        return 2
    try:
        with open(argv[0], encoding="utf-8") as fh:
            rows = json.load(fh)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"錯誤：{argv[0]} 不是有效的日 K JSON（{exc}）", file=sys.stderr)
        return 2
    if not rows:
        print(f"錯誤：{argv[0]} 沒有任何 K 線", file=sys.stderr)
        return 2
    args = argv[1:]

    if "--insecure" in args:
        from market_data import set_insecure_tls
        set_insecure_tls(True)
        print("# ⚠️ 已關閉 TLS 憑證驗證（--insecure）：資料可能被替換，勿用於實盤決策",
              file=sys.stderr)

    # 盤中補 K 必須在任何指標計算之前完成
    appended = False
    if _has(args, "--intraday"):
        sym = _opt(args, "--intraday")
        if not sym:
            print("錯誤：--intraday 需要標的代號，例如 --intraday=hk01164", file=sys.stderr)
            return 2
        from market_data import quote, append_intraday
        before = rows[-1][0]
        q = quote(sym)
        rows = append_intraday(rows, q)
        appended = True
        print(f"# 已補當日盤中 K：{rows[-1][0]} 收 {rows[-1][2]:.3f} "
              f"量 {rows[-1][5]:,.0f}（原本最後一根 {before}）", file=sys.stderr)

    I = compute_all(rows)

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
            lvl, _ = sample_level(v["n"])
            warn = "  ⚠️ 樣本不足，禁止下結論" if v["n"] < 10 else ""
            print(f"  第{k:>2}次 n={v['n']:>3} 均值 {v['mean']:+.2f}% "
                  f"中位 {v['median']:+.2f}% 勝率 {fmt_win(v['n'], v['win'])}{warn}")
        print("\n⚠️ 破位序列的『第 N 次』規律目前只有單標的觀察，各桶 n<10 時不可當通則。")
        return 0

    if "--profile" in args:
        p = volume_profile(rows)
        asof = rows[-1][0]
        tag = "即時（已補當日 K）" if appended else "非即時"
        print(f"現價 {rows[-1][2]:.3f}（{asof} {'收盤' if not appended else '盤中'}，{tag}）")
        print(f"上方量佔 {p['above_pct']:.1f}%，下方 {p['below_pct']:.1f}%")
        if not appended:
            print("提示：交易時段內請加 --intraday=<sym>，否則『當前狀態』是上一交易日。")
        for b in p["buckets"]:
            bar = "█" * int(b["share"] / 2)
            print(f"  {b['low']:7.2f}-{b['high']:7.2f} {b['share']:5.1f}% {bar}")
        return 0

    kinds = ["A", "N", "F", "G", "I", "H"]
    ev_arg = _opt(args, "--events")
    if ev_arg:
        kinds = [x.strip() for x in ev_arg.split(",")]
    # 在「任何計算之前」過濾未知代碼。舊版只守矩陣那一條路徑，
    # summary_table 更早跑，打錯字會直接 KeyError traceback。
    kinds = _validate_kinds(kinds)
    if not kinds and ev_arg:
        return 2

    cd = int(_opt(args, "--cooldown", "10"))
    summary_table(rows, I, kinds, cooldown=cd, show_years=("--years" in args))

    stops, targets, cost = (-9, -12, -15), (12, 16), 0.75
    if _opt(args, "--stops"):
        stops = tuple(float(x) for x in _opt(args, "--stops").split(","))
    if _opt(args, "--targets"):
        targets = tuple(float(x) for x in _opt(args, "--targets").split(","))
    if _opt(args, "--cost"):
        cost = float(_opt(args, "--cost"))

    mv = _opt(args, "--matrix-events") or _opt(args, "--matrix")
    if mv:
        matrix_kinds = _validate_kinds([x.strip() for x in mv.split(",")])
    else:
        matrix_kinds = kinds[:2]
        if len(kinds) > 2:
            print(f"\n（僅輸出前 2 個事件的路徑矩陣：{','.join(matrix_kinds)}。"
                  f"用 --matrix-events={','.join(kinds)} 可指定全部。）")

    for k in matrix_kinds:
        _print_matrix(rows, I, k, stops, targets, cost, cooldown=cd)

    sweep = _opt(args, "--cooldown-sweep")
    if sweep:
        cds = [int(x) for x in sweep.split(",")]
        for k in matrix_kinds:
            if k in EVENT_SPECS:
                _cooldown_sweep(rows, I, k, cds, stops[0], targets[0], cost)
    elif matrix_kinds and matrix_kinds[0] in EVENT_SPECS:
        ev = build_events(I, matrix_kinds[0], cd)
        usable = [i for i in ev if i + 20 < len(rows)]
        if 0 < len(usable) < 30:
            _cooldown_sweep(rows, I, matrix_kinds[0], [5, 10, 20],
                            stops[0], targets[0], cost)
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
