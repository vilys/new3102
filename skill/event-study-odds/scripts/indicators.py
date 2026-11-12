#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
indicators.py — 本地技術指標。口徑固定，避免各家平台數值不一致。

已對照驗證（01164, 2026-09-11 收盤）:
    MACD  hist = 2*(DIF-DEA)  ← 中文慣例，非 1 倍
    ATR14 以百分比表示 = ATR / close * 100

全部函式回傳與輸入等長的 list，前段不足期數處為 None。

CLI:
    python indicators.py 01164.json            # 印最後一根的完整指標
    python indicators.py 01164.json 2920       # 指定索引
    python indicators.py 01164.json 2026-10-23 # 指定日期
"""

import json
import os
import sys


def setup_console():
    """把 stdout/stderr 切到 UTF-8（Windows cp950 console 會 UnicodeEncodeError）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def sma(x, k):
    out = [None] * len(x)
    s = 0.0
    for i, v in enumerate(x):
        s += v
        if i >= k:
            s -= x[i - k]
        if i >= k - 1:
            out[i] = s / k
    return out


def ema(x, k):
    """標準 EMA：以首 k 個有效值的 SMA 播種，之後遞推。

    不用「以首值播種」——那會讓早期誤差沿遞推一路殘留，MACD 會偏離第三方平台。
    x 允許含 None（前段不足期數），會自動跳過。
    """
    out = [None] * len(x)
    vals = [(i, v) for i, v in enumerate(x) if v is not None]
    if len(vals) < k:
        return out
    seed = sum(v for _, v in vals[:k]) / k
    out[vals[k - 1][0]] = seed
    a = 2.0 / (k + 1)
    prev = seed
    for i, v in vals[k:]:
        prev = a * v + (1 - a) * prev
        out[i] = prev
    return out


def rsi(x, k=6):
    """Wilder 平滑。前 k 期用簡單平均，之後遞推。"""
    out = [None] * len(x)
    if len(x) <= k:
        return out
    gain = loss = 0.0
    ag = al = None
    for i in range(1, len(x)):
        d = x[i] - x[i - 1]
        up, dn = max(d, 0.0), max(-d, 0.0)
        if i <= k:
            gain += up
            loss += dn
            if i == k:
                ag, al = gain / k, loss / k
                out[i] = 100 - 100 / (1 + (ag / al if al else 999))
        else:
            ag = (ag * (k - 1) + up) / k
            al = (al * (k - 1) + dn) / k
            out[i] = 100 - 100 / (1 + (ag / al if al else 999))
    return out


def boll(x, k=20, n=2):
    mid = sma(x, k)
    up = [None] * len(x)
    dn = [None] * len(x)
    for i in range(k - 1, len(x)):
        w = x[i - k + 1:i + 1]
        m = mid[i]
        var = sum((v - m) ** 2 for v in w) / k
        sd = var ** 0.5
        up[i] = m + n * sd
        dn[i] = m - n * sd
    return up, mid, dn


def kdj(high, low, close, n=9, k_smooth=3, d_smooth=3):
    length = len(close)
    K = [None] * length
    D = [None] * length
    J = [None] * length
    pk = pd = 50.0
    for i in range(length):
        if i < n - 1:
            continue
        hh = max(high[i - n + 1:i + 1])
        ll = min(low[i - n + 1:i + 1])
        rsv = 50.0 if hh == ll else (close[i] - ll) / (hh - ll) * 100
        pk = (k_smooth - 1) / k_smooth * pk + 1 / k_smooth * rsv
        pd = (d_smooth - 1) / d_smooth * pd + 1 / d_smooth * pk
        K[i], D[i], J[i] = pk, pd, 3 * pk - 2 * pd
    return K, D, J


def macd(x, fast=12, slow=26, signal=9):
    ef, es = ema(x, fast), ema(x, slow)
    dif = [None if (a is None or b is None) else a - b for a, b in zip(ef, es)]
    dea = ema(dif, signal)          # dif 前段為 None，ema() 會自動略過
    hist = [None if (a is None or b is None) else 2 * (a - b) for a, b in zip(dif, dea)]
    return dif, dea, hist


def atr(high, low, close, k=14):
    tr = [None] * len(close)
    for i in range(len(close)):
        if i == 0:
            tr[i] = high[i] - low[i]
        else:
            tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    out = [None] * len(close)
    if len(close) < k:
        return out
    first = sum(tr[:k]) / k
    out[k - 1] = first
    for i in range(k, len(close)):
        out[i] = (out[i - 1] * (k - 1) + tr[i]) / k
    return out


def atr_pct(high, low, close, k=14):
    a = atr(high, low, close, k)
    return [None if (v is None or c == 0) else v / c * 100 for v, c in zip(a, close)]


def prior_low(low, n=250):
    """⚠️ 關鍵：不含當日的前 n 日最低價。含當日會讓所有破位事件永遠不成立。"""
    out = [None] * len(low)
    for i in range(len(low)):
        if i >= n:
            out[i] = min(low[i - n:i])
    return out


def prior_high(high, n=252):
    out = [None] * len(high)
    for i in range(len(high)):
        if i >= n:
            out[i] = max(high[i - n:i])
    return out


def compute_all(rows):
    """一次算齊，回傳 dict（keys 對齊 rows 索引）。"""
    dates = [r[0] for r in rows]
    o = [r[1] for r in rows]
    c = [r[2] for r in rows]
    h = [r[3] for r in rows]
    lo = [r[4] for r in rows]
    v = [r[5] for r in rows]
    up, mid, dn = boll(c, 20, 2)
    K, D, J = kdj(h, lo, c)
    dif, dea, hist = macd(c)
    return {
        "dates": dates, "open": o, "close": c, "high": h, "low": lo, "volume": v,
        "ma5": sma(c, 5), "ma10": sma(c, 10), "ma20": sma(c, 20),
        "ma60": sma(c, 60), "ma120": sma(c, 120), "ma250": sma(c, 250),
        "boll_up": up, "boll_mid": mid, "boll_dn": dn,
        "rsi6": rsi(c, 6), "rsi14": rsi(c, 14),
        "k": K, "d": D, "j": J,
        "dif": dif, "dea": dea, "hist": hist,
        "atr": atr(h, lo, c, 14), "atr_pct": atr_pct(h, lo, c, 14),
        "prior_low": prior_low(lo, 250), "prior_high": prior_high(h, 252),
    }


def summary_at(rows, idx=None):
    """印出某一日（預設最後一日）的技術快照，供報告引用。"""
    I = compute_all(rows)
    i = len(rows) - 1 if idx is None else idx

    def g(k):
        v = I[k][i]
        return "—" if v is None else f"{v:.4f}"

    print(f"{I['dates'][i]}  close={I['close'][i]:.3f}")
    for k in ("ma5", "ma10", "ma20", "ma60", "ma120", "ma250",
              "boll_up", "boll_mid", "boll_dn", "rsi6", "rsi14",
              "k", "d", "j", "dif", "dea", "hist", "atr", "atr_pct"):
        print(f"  {k:9} {g(k)}")
    return I


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

    idx = None
    if len(argv) > 1:
        spec = argv[1]
        if spec.lstrip("-").isdigit():
            idx = int(spec)
            if not -len(rows) <= idx < len(rows):
                print(f"錯誤：索引 {idx} 超出範圍（共 {len(rows)} 根，可用 0–{len(rows)-1}）",
                      file=sys.stderr)
                return 2
        else:
            hits = [j for j, r in enumerate(rows) if r[0] == spec]
            if not hits:
                print(f"錯誤：找不到日期 {spec}（資料範圍 {rows[0][0]} → {rows[-1][0]}）",
                      file=sys.stderr)
                return 2
            idx = hits[0]
    summary_at(rows, idx)
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
