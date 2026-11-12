#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
market_data.py — 日K抓取、合併、盤中補K、報價。

用法（CLI）:
    python market_data.py --help
    python market_data.py hk01164 --start 2015-01-01 --out 01164.json
    python market_data.py US:URNM --range 10y --out urnm.json
    python market_data.py quote hk01164

支援的代號格式:
    hk + 4–5 位數字（hk01164）／US: + Yahoo 代號（US:CCJ）
    其他格式會在發請求之前就被擋下，不會拿任意字串去打端點。

用法（import）:
    from market_data import fetch_hk, fetch_us, append_intraday, quote
    rows = fetch_hk('hk01164', '2015-01-01')     # [[date, o, c, h, l, v], ...]
    rows = fetch_us('URNM')                      # 美股 / ETF 走 Yahoo

約定:
    rows 一律為升序 list[list]，欄位 [date(str), open, close, high, low, volume]
    ⚠️ 注意順序是 O-C-H-L（騰訊回傳順序），不是 OHLC。
"""

import json
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone


def setup_console():
    """把 stdout/stderr 切到 UTF-8。

    Windows 繁中 console 預設 cp950，騰訊回傳的簡體名稱（如「中广核矿业」）
    無法用 cp950 編碼，會直接 UnicodeEncodeError。Python 的 Windows console
    流是 WriteConsoleW 包裝，reconfigure 成 utf-8 後即可正確顯示。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# 預設驗證 TLS 憑證。被中間人餵假價格算出假機率，且不會報錯。
_CTX = ssl.create_default_context()
TLS_INSECURE = False


def set_insecure_tls(enabled=True):
    """顯式關閉 TLS 驗證（僅在端點憑證確有問題時使用）。"""
    global _CTX, TLS_INSECURE
    TLS_INSECURE = bool(enabled)
    _CTX = ssl.create_default_context()
    if enabled:
        _CTX.check_hostname = False
        _CTX.verify_mode = ssl.CERT_NONE


if os.environ.get("EVENT_STUDY_INSECURE_TLS") == "1":
    set_insecure_tls(True)

_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}

TENCENT_HK = "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
QUOTE = "https://qt.gtimg.cn/q={sym}"

# 單次請求上限。傳 2000/3000 會靜默降級或讓 data 變成 list。
MAX_CNT = 1500
# 回溯分段上限，避免無限迴圈把對方打爆（阿步的節流閥）
MAX_SEGMENTS = 8


class FetchError(RuntimeError):
    pass


def _get(url, timeout=40, encoding="utf-8"):
    req = urllib.request.Request(url, headers=_UA)
    raw = urllib.request.urlopen(req, timeout=timeout, context=_CTX).read()
    return raw.decode(encoding, errors="replace")


def _parse_tencent(payload, sym):
    try:
        j = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise FetchError(f"{sym}: 回傳非 JSON（{exc}）。可能是接口限流或降級。") from exc
    if j.get("code") not in (0, "0", None):
        raise FetchError(f"{sym}: 接口 code={j.get('code')} msg={j.get('msg')}")
    data = j.get("data")
    if not isinstance(data, dict) or sym not in data:
        # 典型失敗：data 變成 list，或 key 不存在
        raise FetchError(
            f"{sym}: data 結構異常（type={type(data).__name__}）。"
            "可能是接口降級或代號格式不符。"
        )
    d = data[sym]
    keys = [k for k in d if isinstance(d[k], list) and d[k] and isinstance(d[k][0], list)]
    if not keys:
        # 區分「正常的歷史盡頭」與「結構異常」：
        # 請求區間早於上市日時，騰訊回的是 {..., 'day': []}，這是正常 EOF，
        # 上層 fetch_hk 的 `if not rows: break` 會正確處理。不可當成錯誤。
        empty_bar_keys = [k for k in ("qfqday", "day", "hfqday", "week", "qfqweek")
                          if isinstance(d.get(k), list) and not d[k]]
        if empty_bar_keys:
            return []
        raise FetchError(
            f"{sym}: 找不到 K 線陣列，keys={list(d)[:8]}。"
            "可能原因：請求區間早於上市日（改用較晚的 --start）、"
            "或接口降級（單次 cnt 過大，上限 1500）。"
        )
    # 優先用 qfqday（前復權），否則 day
    key = "qfqday" if "qfqday" in keys else keys[0]
    out = []
    for r in d[key]:
        try:
            out.append([r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])])
        except (IndexError, ValueError):
            continue
    return out


def fetch_hk_segment(sym, end_date, cnt=MAX_CNT, start_date="1990-01-01"):
    url = f"{TENCENT_HK}?param={sym},day,{start_date},{end_date},{cnt},qfq"
    return _parse_tencent(_get(url), sym)


def fetch_hk(sym, start="2015-01-01", end=None, verbose=False):
    """分段回溯抓取港股日K（前復權），按日期去重合併後升序回傳。

    騰訊單次最多約 1500 根，要更長歷史必須以每段的首日往前推。
    """
    end = end or datetime.now().strftime("%Y-%m-%d")
    merged = {}
    cursor = end
    for seg in range(MAX_SEGMENTS):
        rows = fetch_hk_segment(sym, cursor)
        if not rows:
            break
        added = 0
        for r in rows:
            if r[0] not in merged:
                merged[r[0]] = r
                added += 1
        first = rows[0][0]
        if verbose:
            print(f"  [seg{seg+1}] {first} → {rows[-1][0]}  n={len(rows)}  新增={added}", file=sys.stderr)
        if first <= start or added == 0:
            break
        cursor = (datetime.strptime(first, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    out = [merged[k] for k in sorted(merged)]
    out = [r for r in out if r[0] >= start]
    if not out:
        raise FetchError(f"{sym}: 合併後無資料")
    return out


def fetch_us(sym, rng="10y"):
    """美股 / ETF 日K（Yahoo，未復權調整已含在 close 內）。"""
    url = YAHOO.format(sym=sym) + f"?range={rng}&interval=1d"
    payload = _get(url)
    j = json.loads(payload)
    res = (j.get("chart") or {}).get("result")
    if not res:
        raise FetchError(f"{sym}: Yahoo 無結果 {(j.get('chart') or {}).get('error')}")
    r = res[0]
    ts = r.get("timestamp") or []
    q = r["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(ts):
        c = q["close"][i]
        if c is None:
            continue
        o = q["open"][i] if q["open"][i] is not None else c
        h = q["high"][i] if q["high"][i] is not None else c
        lo = q["low"][i] if q["low"][i] is not None else c
        v = q["volume"][i] or 0
        out.append([
            datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"),
            float(o), float(c), float(h), float(lo), float(v),
        ])
    out.sort(key=lambda x: x[0])
    if not out:
        raise FetchError(f"{sym}: Yahoo 回傳空序列")
    return out


def quote(sym):
    """實時報價。回傳 dict：price / prev_close / open / high / low / pct / w52_high / w52_low。

    騰訊欄位以 '~' 分隔，不同市場欄位位置略有差異；此處只取跨市場一致的欄位，
    52 週高低點採「掃描是否為合理浮點數」的方式定位，避免硬編碼錯位。
    """
    raw = _get(QUOTE.format(sym=sym), timeout=15, encoding="gbk")
    body = raw.split('"')[1] if '"' in raw else raw
    f = body.split("~")

    def num(i):
        try:
            return float(f[i])
        except (IndexError, ValueError):
            return None

    d = {
        "symbol": sym,
        "name": f[1] if len(f) > 1 else "",
        "price": num(3),
        "prev_close": num(4),
        "open": num(5),
        "volume": num(6),
        "pct": num(32),
        "high": num(33),
        "low": num(34),
        "raw_len": len(f),
    }
    # 52 週高/低：騰訊通常落在 47/48，但不同市場會偏移。用「>= 現價」與「<= 現價」校驗。
    hi = num(47)
    lo = num(48)
    if hi is not None and lo is not None and hi >= (d["price"] or 0) >= lo > 0:
        d["w52_high"], d["w52_low"] = hi, lo
    else:
        d["w52_high"], d["w52_low"] = None, None
        # 港股這個欄位常整段錯位；回 null 比回錯數字好，但使用者需要知道怎麼補
        d["w52_note"] = "此市場欄位不可用；CLI 會自動用日 K 補算，程式內可呼叫 fill_w52(q)"
    return d


def fill_w52(q, bars=250):
    """用日 K 補算 52 週高低（騰訊的 47/48 欄位在港股常失效）。

    只有在原始欄位為 None 時才發請求，避免多打一次端點。
    """
    if not q or q.get("w52_high") is not None or q.get("price") is None:
        return q
    sym = q.get("symbol") or ""
    try:
        if sym.lower().startswith("us"):
            rows = fetch_us(sym, rng="1y")
        else:
            rows = fetch_hk(sym, start="2024-01-01")
    except Exception as exc:  # 網路/端點問題不該讓報價整體失敗
        q["w52_note"] = f"日 K 補算失敗（{exc}），52 週高低不可用"
        return q
    rows = rows[-bars:]
    if not rows:
        q["w52_note"] = "日 K 為空，52 週高低不可用"
        return q
    q["w52_high"] = max(r[3] for r in rows)
    q["w52_low"] = min(r[4] for r in rows)
    q["w52_source"] = f"日 K 補算（{rows[0][0]} → {rows[-1][0]}，{len(rows)} 根）"
    q.pop("w52_note", None)
    return q


def append_intraday(rows, q):
    """把當日盤中合成一根 K 補進序列（交易時段必做，否則「當前狀態」是昨天的）。

    已存在同日則覆蓋。
    """
    if not q or q.get("price") is None:
        return rows
    today = datetime.now().strftime("%Y-%m-%d")
    bar = [
        today,
        q.get("open") or q["price"],
        q["price"],
        q.get("high") or q["price"],
        q.get("low") or q["price"],
        float(q.get("volume") or 0.0),
    ]
    out = [r for r in rows if r[0] != today]
    out.append(bar)
    out.sort(key=lambda x: x[0])
    return out


def to_ohlc(rows):
    """轉成慣用的 (dates, open, close, high, low, volume) 六元組。"""
    d = [r[0] for r in rows]
    o = [r[1] for r in rows]
    c = [r[2] for r in rows]
    h = [r[3] for r in rows]
    lo = [r[4] for r in rows]
    v = [r[5] for r in rows]
    return d, o, c, h, lo, v


def _validate_symbol(sym):
    """在發請求之前檢查代號格式，避免拿任意字串去打端點。回傳錯誤字串或 None。"""
    s = (sym or "").strip()
    if re.fullmatch(r"(?i)hk\d{4,5}", s):
        return None
    if re.fullmatch(r"(?i)us:[A-Za-z0-9.\-]{1,10}", s):
        return None
    return (f"無法辨識的標的代號 {sym!r}。支援格式：\n"
            f"  港股  hk01164（hk + 4–5 位數字）\n"
            f"  美股  US:CCJ（US: + Yahoo 代號）")


def _fetch_error_message(exc):
    """把抓取例外翻成「原因 + 下一步」，不要在 CLI 露裸 traceback。

    區分四類：端點無資料／限流（FetchError）、TLS 憑證、逾時、一般連線失敗。
    TLS 那條**不建議直接關驗證**——--insecure 只作最後手段並會印警告。
    """
    if isinstance(exc, FetchError):
        return (f"抓取失敗：{exc}\n"
                f"  下一步：確認代號是否為 5 位含前導零（港股例：hk01164）；\n"
                f"  若代號正確，多半是接口限流或當日無成交——請隔幾分鐘再試，不要連續重試。")
    reason = getattr(exc, "reason", None) or exc
    if isinstance(reason, ssl.SSLError) or isinstance(exc, ssl.SSLError):
        return ("TLS 憑證驗證失敗：端點憑證無法通過驗證（常見於企業代理、憑證鏈不完整或系統時間錯誤）。\n"
                "  下一步：先修正系統時間或匯入企業根憑證。\n"
                "  ⚠️ --insecure 只作最後手段：它會關閉中間人防護，資料可能被替換，勿用於實盤決策。")
    if isinstance(reason, (socket.timeout, TimeoutError)) or isinstance(exc, (socket.timeout, TimeoutError)):
        return ("網路逾時：端點在時限內未回應。\n"
                "  下一步：確認網路後重試；若疑似接口限流，請隔幾分鐘再試，不要連續重試。")
    if isinstance(exc, urllib.error.URLError):
        return (f"網路連線失敗：{reason}\n"
                "  下一步：確認網路與代理設定（企業網路可能需要設定 HTTPS_PROXY）。")
    return f"抓取失敗：{exc}\n  下一步：確認代號與網路後重試。"


def _cli(argv):
    setup_console()
    if not argv:
        print(__doc__)
        return 1
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    cmd = argv[0]

    if "--insecure" in argv:
        set_insecure_tls(True)
        print("# ⚠️ 已關閉 TLS 憑證驗證（--insecure）：資料可能被中間人替換，勿用於實盤決策",
              file=sys.stderr)

    if cmd == "quote":
        if len(argv) < 2 or argv[1].startswith("-"):
            print("錯誤：quote 需要標的代號，例如 python market_data.py quote hk01164",
                  file=sys.stderr)
            return 2
        bad = _validate_symbol(argv[1])
        if bad:
            print(f"錯誤：{bad}", file=sys.stderr)
            return 2
        q = fill_w52(quote(argv[1]))
        print(json.dumps(q, ensure_ascii=False, indent=2))
        return 0

    bad = _validate_symbol(cmd)
    if bad:
        print(f"錯誤：{bad}", file=sys.stderr)
        return 2

    out = None
    start, rng, verbose = "2015-01-01", "10y", False
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("--out", "--start", "--range"):
            if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
                print(f"錯誤：{a} 需要一個值（例：{a} "
                      f"{'01164.json' if a == '--out' else '2015-01-01' if a == '--start' else '10y'}）",
                      file=sys.stderr)
                return 2
            val = argv[i + 1]
            if a == "--out":
                out = val
            elif a == "--start":
                start = val
            else:
                rng = val
            i += 2
        elif a == "--verbose":
            verbose = True; i += 1
        else:
            i += 1

    # 原本 fetch 這一段沒有保護：hk1164（漏前導零）通過格式驗證後直接裸 traceback。
    # 抓取失敗是預期內的情境（限流／下市／斷網），一律友善訊息 + exit 2。
    try:
        if cmd.startswith("US:") or cmd.startswith("us:"):
            rows = fetch_us(cmd.split(":", 1)[1].upper(), rng)
        else:
            rows = fetch_hk(cmd, start, verbose=verbose)
    except (FetchError, urllib.error.URLError, ssl.SSLError,
            socket.timeout, TimeoutError) as exc:
        print(f"錯誤：{_fetch_error_message(exc)}", file=sys.stderr)
        return 2

    if not rows:
        print(f"錯誤：{cmd} 抓不到任何日 K。\n"
              f"  下一步：確認代號與日期範圍後重試。", file=sys.stderr)
        return 2

    print(f"# {cmd}  n={len(rows)}  {rows[0][0]} → {rows[-1][0]}", file=sys.stderr)
    payload = json.dumps(rows)
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(payload)
        print(f"# written {out}", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
