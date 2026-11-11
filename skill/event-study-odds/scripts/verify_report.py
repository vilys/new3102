#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_report.py — HTML 報告交付前自檢。

檢查兩層:

  渲染層
    1. 內聯 <script> 的 JS 語法（呼叫 node --check）
    2. 各資料數列長度是否一致
    3. 物件內的重複鍵（含嵌套物件）

  財務一致性層（啟發式，抓最常見的錯誤）
    4. 同一指標（MA/RSI/ATR）在報告內是否出現多個不一致的值
       （只抓「多值衝突」，抓不到「單一但錯誤」的值）
    5. 價位宣稱是否對得上原始 K 線：
       歷史低點型（失效線/支撐/前低）比低點、目標型（壓力/目標）比高點、
       計算型（止損）驗算術；落在數列窗口外的宣稱列 INFO 不判錯
    6. n<10 卻仍印出小數勝率
    7. 免責聲明是否存在

用法:
    python verify_report.py report.html

退出碼:
    0 = 全部通過
    1 = 存在錯誤，不得交付
    2 = 部分通過：有檢查被跳過（例如找不到 node 或找不到數列），需人工確認

⚠️ 「全部通過」只代表渲染層與上述啟發式檢查通過，不代表所有財務數字已被驗證。
"""

import glob
import json
import os
import re
import subprocess
import sys
import tempfile


def setup_console():
    """把 stdout/stderr 切到 UTF-8（Windows cp950 console 會 UnicodeEncodeError）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def find_node():
    """優先用 PATH 的 node；否則掃 workbuddy 內建版本（版本號會變，不能寫死）。"""
    for cand in ("node", "nodejs"):
        try:
            subprocess.run([cand, "--version"], capture_output=True, check=True)
            return cand
        except (OSError, subprocess.CalledProcessError):
            pass
    patterns = [
        os.path.expanduser("~/.workbuddy/binaries/node/versions/*/node.exe"),
        os.path.expanduser("~/.workbuddy/binaries/node/versions/*/bin/node"),
        "/usr/local/bin/node",
        "/opt/homebrew/bin/node",
    ]
    hits = []
    for p in patterns:
        hits.extend(glob.glob(p))
    return sorted(hits)[-1] if hits else None


def extract_scripts(html):
    return re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)


def check_js(scripts, node):
    if not scripts:
        return False, "無內聯 script（可能全部走 CDN，請確認圖表確實會渲染）", True
    if not node:
        return False, "找不到 node，無法校驗 JS 語法（不可跳過，請先定位 node）", True
    blob = "\n;\n".join(scripts)
    fd, path = tempfile.mkstemp(suffix=".js")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(blob)
        r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"JS 語法錯誤:\n{r.stderr.strip()}", False
        return True, f"{len(scripts)} 個內聯塊、{len(blob)} bytes，語法通過", False
    finally:
        os.unlink(path)


# ---------------------------------------------------------------- 數列

_ARRAY_RE = re.compile(
    r"(?:var|let|const)\s+(\w+)\s*=\s*\[|window\.(\w+)\s*=\s*\["
)


def _match_bracket(text, start):
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _count_top_level(body):
    depth, n, in_str, quote, esc = 0, 1, False, "", False
    for ch in body:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
            continue
        if ch in "\"'":
            in_str, quote = True, ch
        elif ch in "[{(":
            depth += 1
        elif ch in "]})":
            depth -= 1
        elif ch == "," and depth == 0:
            n += 1
    return n


def collect_arrays(html, scripts):
    """回傳 {name: {"n": len, "values": [float] or None, "nested": [[float]] or None}}。

    同時支援 `var X=[...]` 與 `window.X=[...]`——後者是本專案報告的實際寫法。
    `nested` 收 ECharts candlestick 這類「list of list」數列（每列 3–5 欄），
    讓價位對照也能驗證 K 線陣列（例如 `window.K=[[o,c,l,h],...]`）。
    """
    source = "\n".join(scripts) if scripts else html
    found = {}
    for m in _ARRAY_RE.finditer(source):
        name = m.group(1) or m.group(2)
        start = source.index("[", m.start())
        end = _match_bracket(source, start)
        if end < 0:
            continue
        body = source[start + 1:end]
        try:
            arr = json.loads("[" + body + "]")
        except (json.JSONDecodeError, ValueError):
            arr = None
        values = None
        if isinstance(arr, list):
            nums = []
            ok = True
            for v in arr:
                if isinstance(v, (int, float)):
                    nums.append(float(v))
                elif v is None:
                    nums.append(None)
                else:
                    ok = False
                    break
            if ok:
                values = nums
        nested = None
        if isinstance(arr, list) and len(arr) >= 5 and all(isinstance(r, list) for r in arr):
            w = len(arr[0])
            if 3 <= w <= 5 and all(len(r) == w for r in arr) and \
                    all(isinstance(x, (int, float)) for r in arr for x in r):
                nested = [[float(x) for x in r] for r in arr]
        found[name] = {"n": len(arr) if isinstance(arr, list) else _count_top_level(body),
                       "values": values, "nested": nested}
    return found


def _nested_column(rows, want="low"):
    """在巢狀 OHLC 陣列裡找出「恆為該列最小值／最大值」的唯一欄位。

    不硬編碼 index：`[o,c,l,h]`、`[低,收,高]`、`[o,h,l,c]` 都能正確定定位。
    若有多個欄位都恆為極值（無法辨識），回 None——寧可跳過也不要猜錯。
    """
    w = len(rows[0])
    hits = []
    for i in range(w):
        if all((r[i] == min(r)) if want == "low" else (r[i] == max(r)) for r in rows):
            hits.append(i)
    return hits[0] if len(hits) == 1 else None


# ---------------------------------------------------------------- 重複鍵

def _iter_object_bodies(text):
    stack = []
    for i, ch in enumerate(text):
        if ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            yield text[start + 1:i]


def _top_level_keys(body):
    keys, depth, cur = [], 0, []
    for ch in body:
        if ch in "[{(":
            depth += 1
        elif ch in "]})":
            depth -= 1
        if depth == 0 and ch == ":":
            k = "".join(cur).strip().strip("'\"")
            if re.fullmatch(r"[A-Za-z_$][\w$]*", k):
                keys.append(k)
            cur = []
        elif depth == 0 and ch == ",":
            cur = []
        elif depth == 0:
            cur.append(ch)
    return keys


def check_duplicate_keys(scripts):
    """掃描所有物件（含嵌套），找同一層級的重複鍵。

    舊版用 `\\{[^{}]{0,400}\\}` 只匹配扁平物件，嵌套物件內的重複鍵完全漏掉。
    """
    bad, seen = [], set()
    for body in _iter_object_bodies("\n".join(scripts)):
        if len(body) < 8 or ":" not in body:
            continue
        keys = _top_level_keys(body)
        dup = {k for k in keys if keys.count(k) > 1}
        if not dup:
            continue
        sig = (tuple(sorted(dup)), body[:80])
        if sig in seen:
            continue
        seen.add(sig)
        bad.append((dup, body[:100].replace("\n", " ")))
    return bad


# ---------------------------------------------------------------- 財務一致性

_METRIC_RE = re.compile(r"\b(MA\d{1,3}|RSI\d{1,2}|ATR)\b[\s:：=＝]{0,3}(\d+\.\d+)")
# 反向寫法：「2.088（MA20）」「23.95 (MA20)」——報告常用數字在前、標籤在後
_METRIC_REV_RE = re.compile(r"(\d+\.\d+)\s*[（(]\s*(MA\d{1,3}|RSI\d{1,2}|ATR)\s*[）)]")
# 歷史低點型：這些價位「應該」等於某根 K 的低點
# `[^0-9\-<>。！？]` 刻意擋掉跨標籤與跨句：否則「支撐…1.06 倍均量」會被誤認成價位
_LOW_CLAIM_RE = re.compile(
    r"(失效線|支撐|前低|觀察線|防守線|鐵底|頸線)[^0-9\-<>。！？]{0,24}(-?\d+\.\d+)")
# 目標型：多半是圖表推導的壓力／目標，也可能引用分析師目標價 → 只提示，不判 FAIL
_HIGH_CLAIM_RE = re.compile(
    r"(壓力|阻力|目標價|目標|下一站|上看)[^0-9\-<>。！？]{0,24}(-?\d+\.\d+)")
# 計算型：止損價通常由「進場價 × (1−止損%)」算出，本來就不會等於任何歷史低點
_STOP_CLAIM_RE = re.compile(
    r"(止損|止蝕|停損)[^0-9\n]{0,6}(?:[−\-]?\d+(?:\.\d+)?\s*%\s*[（(]?\s*)?"
    r"(?:-?\d+\.\d+[^0-9\n（(]{0,4}[（(]\s*約\s*)?"
    r"(-?\d+\.\d+)(?![\d%])")
# 進場價可能在關鍵詞前（「以 23.74 進場」）或後（「進場 2.230」）；
# 後者不可跨越逗號／頓號去抓下一個價位，否則「進場、止損設在 22.24」會誤取止損價當進場價
_ENTRY_RE = re.compile(r"進場(?:價)?[^\d\n，、。；]{0,4}(\d+\.\d+)")
_ENTRY_REV_RE = re.compile(r"(\d+\.\d+)[^\d\n]{0,4}(?:進場|買入)")
_STOP_PCT_RE = re.compile(r"(?:止損|止蝕|停損)[^\d\n%]{0,8}[−\-]?(\d+(?:\.\d+)?)\s*%")
_STOP_PCT_ANY_RE = re.compile(r"[−\-](\d+(?:\.\d+)?)\s*%")
_STOP_MULT_RE = re.compile(r"[×xX*]\s*(0\.\d+)")

_LOW_NAME_RE = re.compile(r"(?i)^(?:l|lo|low)\d*$|(?:^|_)low(?:_\d+)?$")
_HIGH_NAME_RE = re.compile(r"(?i)^(?:h|hi|high)\d*$|(?:^|_)high(?:_\d+)?$")


def _visible(html):
    """去掉 <script>、註解與所有 HTML 標籤再做文字比對。

    1. 報告的圖表軸標籤常寫成 `['1.95 前低','2.09 MA10']`，留在 script 裡
       會被「前低 → 2.09」誤抓成價位宣稱。
    2. 正文常寫成「失效線：<b>跌破 1.965」，若不禁掉標籤就會在 `<` 處斷開、
       整條宣稱漏抓——所以這裡是「剝掉標籤」而不是「禁止出現標籤」。
    """
    out = re.sub(r"<script(?![^>]*\bsrc=)[^>]*>.*?</script>", " ", html, flags=re.S)
    out = re.sub(r"<!--.*?-->", " ", out, flags=re.S)
    # 只剝「長得像 HTML 標籤」的片段（以字母或 / 開頭）；
    # 否則內文的 `RSI6<30 且 close<MA60` 會被當成標籤開頭，吞掉整段文字。
    out = re.sub(r"</?[a-zA-Z][^>]*>", " ", out)
    return out


def _price_series(arrays, want="low"):
    """收集低點／高點數列。

    來源有兩種：
      1. 扁平數值數列，名字像 `L1164` / `LO` / `low` / `H1164` / `high`
      2. 巢狀 K 陣列，用「恆為該列最小值（低）／最大值（高）的唯一欄位」取出
    回傳 [(標籤, [float])]。
    """
    out = []
    name_re = _LOW_NAME_RE if want == "low" else _HIGH_NAME_RE
    for name, info in arrays.items():
        v = info.get("values")
        if v and name_re.search(name) and len(v) > 5:
            vals = [x for x in v[-260:] if x is not None]
            if vals:
                out.append((name, vals))
        rows = info.get("nested")
        if rows and len(rows) > 5:
            idx = _nested_column(rows, want)
            if idx is not None:
                tail = rows[-260:]
                out.append((f"{name}[{idx}]", [r[idx] for r in tail]))
    return out


def _verify_stop_arithmetic(html, m, price):
    """嘗試驗算止損價：進場價 × (1−止損%) 或 進場價 × 係數。

    回傳 (status, detail)，status ∈ {"ok", "bad", "unknown"}。
    """
    ctx = html[max(0, m.start() - 60):m.end() + 160]
    entry = _ENTRY_RE.search(ctx) or _ENTRY_REV_RE.search(ctx)
    if not entry:
        return "unknown", "未見進場價，無法驗算"
    e = float(entry.group(1))

    # 百分比只從「止損」之後找：前面 60 字常是走勢區間（-14.7% / -22.0%），
    # 拿它當止損幅度會算出完全錯誤的期望價位。
    tail = html[m.start():m.end() + 120]
    pct = _STOP_PCT_RE.search(tail)
    if not pct:
        # 「止損設在 22.24 之下（約 21.90，−7.7%，約 1.7 倍 ATR）」這種寫法
        pct = _STOP_PCT_ANY_RE.search(tail)
    mult = _STOP_MULT_RE.search(tail)
    if pct:
        p = float(pct.group(1))
        expected, how = e * (1 - p / 100), f"{e:.3f} × (1−{p:g}%)"
    elif mult:
        k = float(mult.group(1))
        expected, how = e * k, f"{e:.3f} × {k:g}"
    else:
        return "unknown", "未見止損百分比，無法驗算"

    if abs(price - expected) <= max(0.005, 0.01 * abs(price)):
        return "ok", f"{how} = {expected:.3f}，相符"
    return "bad", f"{how} = {expected:.3f}，與 {price} 不符（差 {abs(price - expected):.3f}）"


def check_metric_conflict(html):
    """同一指標出現兩個「接近但不相等」的值 → 多半是混用了不同 cutoff 的資料。

    先按量級分簇（相差 >3% 視為不同標的／不同時期的敘述），
    再在簇內比對：相差 0.15%~3% 即判定為同指標多值。
    """
    vals = {}
    for m in _METRIC_RE.finditer(html):
        vals.setdefault(m.group(1), []).append(float(m.group(2)))
    for m in _METRIC_REV_RE.finditer(html):
        vals.setdefault(m.group(2), []).append(float(m.group(1)))

    conflicts = []
    for k, xs in vals.items():
        uniq = sorted(set(x for x in xs if x > 0))
        if len(uniq) < 2:
            continue
        cluster = [uniq[0]]
        for x in uniq[1:]:
            if x <= cluster[0] * 1.03:
                cluster.append(x)
            else:
                if len(cluster) > 1 and cluster[-1] - cluster[0] > 0.0015 * cluster[0]:
                    conflicts.append((k, cluster))
                cluster = [x]
        if len(cluster) > 1 and cluster[-1] - cluster[0] > 0.0015 * cluster[0]:
            conflicts.append((k, cluster))
    return conflicts


def check_price_claims(html, arrays):
    """把報告裡的價位宣稱對回原始 K 線。

    分三類處理，避免把「合理的計算值」誤判成錯誤：
      歷史低點型（失效線/支撐/前低/觀察線）→ 必須命中低點序列，否則 FAIL
      目標型（壓力/目標/下一站）           → 只對高點序列做提示，不判 FAIL
      計算型（止損）                       → 驗算術；算不出來只列 INFO

    價位若落在數列窗口之外（例如引用 2025-04 的低點，而數列只有近 105 根），
    無法驗證 → 列 INFO，不當成錯誤。

    回傳 (problems, checked, skipped_reason, infos)
    """
    text = _visible(html)
    low_series = _price_series(arrays, "low")
    high_series = _price_series(arrays, "high")
    if not low_series and not high_series:
        return [], 0, ("找不到低點／高點數列（需 var/window 形式且含 L/LO/low/H/high，"
                       "或巢狀 K 陣列）"), []

    problems, infos = [], []
    checked = 0

    def nearest(series, price):
        best = None
        for name, v in series:
            for x in v:
                d = abs(x - price)
                if best is None or d < best[0]:
                    best = (d, x, name)
        return best

    def window(series):
        lo = min(min(v) for _, v in series)
        hi = max(max(v) for _, v in series)
        return lo, hi

    # ---- 歷史低點型：這裡才是真正的「錯價位」檢查
    if low_series:
        for m in _LOW_CLAIM_RE.finditer(text):
            if m.end() < len(text) and text[m.end()] == "%":
                continue
            label, price = m.group(1), float(m.group(2))
            if price <= 0:
                continue
            checked += 1
            lo, hi = window(low_series)
            best = nearest(low_series, price)
            if not (lo * 0.995 <= price <= hi * 1.005):
                infos.append(f"{label} {price} 超出數列窗口（{lo:.3f}–{hi:.3f}），"
                             f"無法驗證（可能引用更早的低點）")
            elif best and best[0] > 0.0015 * price:
                problems.append(
                    f"{label} {price} 在近 260 根低點中找不到對應值"
                    f"（最接近 {best[1]:.3f}，差 {best[0]:.3f}，來源 {best[2]}）")

    # ---- 目標型：只提示，不判 FAIL（可能是分析師目標價或推導值）
    if high_series:
        for m in _HIGH_CLAIM_RE.finditer(text):
            if m.end() < len(text) and text[m.end()] == "%":
                continue
            label, price = m.group(1), float(m.group(2))
            if price <= 0:
                continue
            lo, hi = window(high_series)
            best = nearest(high_series, price)
            if not (lo * 0.995 <= price <= hi * 1.005):
                infos.append(f"{label} {price} 超出高點窗口（{lo:.3f}–{hi:.3f}），"
                             f"未自動驗證（分析師目標價或更早期高點屬正常）")
            elif best and best[0] > 0.0015 * price:
                infos.append(f"{label} {price} 未落在近 260 根高點上"
                             f"（最接近 {best[1]:.3f}，差 {best[0]:.3f}，來源 {best[2]}）")

    # ---- 計算型：止損價不是歷史低點，改驗算術
    for m in _STOP_CLAIM_RE.finditer(text):
        price = float(m.group(2))
        if price <= 0:
            continue
        checked += 1
        status, detail = _verify_stop_arithmetic(text, m, price)
        if status == "bad":
            problems.append(f"{m.group(1)} {price} 算術不符：{detail}")
        elif status == "unknown":
            infos.append(f"{m.group(1)} {price} 屬計算型，{detail}（不計入錯誤）")

    if not checked:
        return [], 0, "報告內找不到失效線/支撐/前低/止損的具體價位", infos
    return problems, checked, None, infos


def check_small_sample(html):
    """n<10 卻印出小數勝率。若緊鄰文字已明確標註樣本不足，則不視為問題。"""
    out = []
    excuse = re.compile(r"樣本不足|低於 10|低於10|不能拿|不得|僅供參考|軼事|n<10")
    for m in re.finditer(r"n\s*[=＝]\s*(\d+)[^%\n]{0,40}?(\d+\.\d+)\s*%", html):
        n, win = int(m.group(1)), m.group(2)
        if n >= 10:
            continue
        ctx = html[max(0, m.start() - 300):m.end() + 300]
        if excuse.search(ctx):
            continue
        out.append(f"n={n} 卻給出勝率 {win}%")
    return out


def check_disclaimer(html):
    return "不構成投資建議" in html


# ---------------------------------------------------------------- main

def main(argv):
    setup_console()
    if not argv:
        print(__doc__)
        return 2
    path = argv[0]
    html = open(path, encoding="utf-8").read()
    errors, warnings, skipped = [], [], []

    print(f"檔案: {path}  ({len(html):,} chars)")

    scripts = extract_scripts(html)
    node = find_node()
    good, msg, was_skipped = check_js(scripts, node)
    print(("  [PASS] " if good else "  [SKIP] " if was_skipped else "  [FAIL] ")
          + "JS 語法 — " + msg)
    if not good and was_skipped:
        skipped.append("JS 語法")
    elif not good:
        errors.append("JS 語法")

    arrays = collect_arrays(html, scripts)
    if arrays:
        lens = {k: v["n"] for k, v in arrays.items()}
        print(f"  [INFO] 資料數列 {len(lens)} 組: "
              + ", ".join(f"{k}={v}" for k, v in list(lens.items())[:8])
              + (" …" if len(lens) > 8 else ""))
        # 只比對數值型數列；長度遠小於主群的（< 主群一半）視為橫斷面資料（如同業名單、板塊表現），
        # 不是時間序列，不應觸發「長度不一致」。
        big = [v["n"] for v in arrays.values() if v["n"] > 5 and v.get("values") is not None]
        if big:
            from collections import Counter
            mode_len = Counter(big).most_common(1)[0][0]
            series = [n for n in big if n >= mode_len * 0.5]
            if len(set(series)) > 1:
                warnings.append(f"數列長度不一致（{sorted(set(series))}），請確認是否為不同窗口")
                print("  [WARN] 數列長度不一致，請確認是否為刻意（如不同窗口的圖）")
    else:
        skipped.append("數列檢查")
        print("  [SKIP] 未偵測到 var/window 形式的數列 → 此項未執行，不可視為通過")

    dups = check_duplicate_keys(scripts)
    if dups:
        errors.append("重複鍵")
        print(f"  [FAIL] 發現 {len(dups)} 處重複鍵（含嵌套物件）:")
        for keys, snippet in dups[:5]:
            print(f"          {keys} ← {snippet}")
    else:
        print("  [PASS] 重複鍵 — 未發現")

    conflicts = check_metric_conflict(html)
    if conflicts:
        errors.append("指標值衝突")
        print("  [FAIL] 同一指標出現多個不一致的值（疑似混用不同 cutoff）:")
        for k, uniq in conflicts:
            print(f"          {k}: {uniq}")
    else:
        print("  [PASS] 指標一致性 — 未發現同指標多值")

    problems, checked, reason, infos = check_price_claims(html, arrays)
    for info in infos:
        print(f"  [INFO] 價位對照 — {info}")
    if reason:
        skipped.append("價位對照")
        print(f"  [SKIP] 價位對照 — {reason}")
    elif problems:
        errors.append("價位對不上原始 K")
        print(f"  [FAIL] 價位對照（檢查 {checked} 處）:")
        for p in problems:
            print(f"          {p}")
    else:
        print(f"  [PASS] 價位對照 — {checked} 處均能在近 260 根低點中找到／算術相符")

    small = check_small_sample(html)
    if small:
        warnings.append("小樣本勝率")
        print("  [WARN] 小樣本仍給勝率（應標註或遮蔽）:")
        for s in small[:5]:
            print(f"          {s}")
    else:
        print("  [PASS] 小樣本 — 未發現 n<10 仍給小數勝率")

    if check_disclaimer(html):
        print("  [PASS] 免責聲明 — 存在")
    else:
        warnings.append("缺少免責聲明")
        print("  [WARN] 找不到『不構成投資建議』免責聲明")

    print()
    if errors:
        print("結果：存在錯誤，不得交付")
        print("  " + "；".join(dict.fromkeys(errors)))
        return 1
    if skipped or warnings:
        print("結果：部分通過，需人工確認")
        if skipped:
            print("  未執行的檢查：" + "、".join(dict.fromkeys(skipped)))
        if warnings:
            print("  警告：" + "、".join(dict.fromkeys(warnings)))
        print("  （渲染層通過 ≠ 財務數字全部正確）")
        return 2
    print("結果：全部通過（渲染層＋啟發式財務檢查），可交付")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
