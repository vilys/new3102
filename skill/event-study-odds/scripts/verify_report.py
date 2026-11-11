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
    5. 「失效線 / 止損 / 支撐」的價位是否對得上原始 K 線的低點
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
    """回傳 {name: {"n": len, "values": [float] or None}}。

    同時支援 `var X=[...]` 與 `window.X=[...]`——後者是本專案報告的實際寫法。
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
        found[name] = {"n": len(arr) if isinstance(arr, list) else _count_top_level(body),
                       "values": values}
    return found


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
_CLAIM_RE = re.compile(r"(失效線|止損|支撐)[^0-9\-]{0,24}(-?\d+\.\d+)")


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
    """把『失效線/止損/支撐』的價位對回原始 K 線的低點。

    回傳 (problems, checked, skipped_reason)
    """
    low_series = []
    for name, info in arrays.items():
        v = info.get("values")
        if not v:
            continue
        if re.search(r"(?i)^(l|lo|low)\d+$|(^|_)low$", name) and len(v) > 5:
            low_series.append((name, v[-260:]))
    if not low_series:
        return [], 0, "找不到低點數列（需 var/window 形式且含 low/L）"

    claims = []
    for m in _CLAIM_RE.finditer(html):
        # 「扣 0.75% 成本」這類百分比不是價位
        if m.end() < len(html) and html[m.end()] == "%":
            continue
        claims.append((m.group(1), float(m.group(2))))
    if not claims:
        return [], 0, "報告內找不到失效線/止損/支撐的具體價位"

    problems = []
    checked = 0
    for label, price in claims:
        if price <= 0:
            continue
        checked += 1
        best = None
        for name, v in low_series:
            for x in v:
                if x is None:
                    continue
                d = abs(x - price)
                if best is None or d < best[0]:
                    best = (d, x, name)
        if best and best[0] > 0.0015 * price:
            problems.append(
                f"{label} {price} 在近 260 根低點中找不到對應值"
                f"（最接近 {best[1]:.3f}，差 {best[0]:.3f}，來源 {best[2]}）"
            )
    return problems, checked, None


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

    problems, checked, reason = check_price_claims(html, arrays)
    if reason:
        skipped.append("價位對照")
        print(f"  [SKIP] 價位對照 — {reason}")
    elif problems:
        errors.append("價位對不上原始 K")
        print(f"  [FAIL] 價位對照（檢查 {checked} 處）:")
        for p in problems:
            print(f"          {p}")
    else:
        print(f"  [PASS] 價位對照 — {checked} 處均能在近 260 根低點中找到")

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
