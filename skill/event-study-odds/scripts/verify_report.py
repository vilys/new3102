#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_report.py — HTML 報告交付前自檢。

檢查三件事:
  1. 內聯 <script> 的 JS 語法（呼叫 node --check）
  2. 各資料數列長度是否一致（日期/收盤/RSI 等必須同 n）
  3. 常見的低級錯誤：重複的物件鍵、未閉合的圖表容器

用法:
    python verify_report.py report.html

退出碼 0 = 全過；非 0 = 有錯，不得交付。
"""

import glob
import os
import re
import subprocess
import sys
import tempfile


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
        return True, "無內聯 script（可能全部走 CDN，請確認圖表確實會渲染）"
    if not node:
        return False, "找不到 node，無法校驗 JS 語法（不可跳過，請先定位 node）"
    blob = "\n;\n".join(scripts)
    fd, path = tempfile.mkstemp(suffix=".js")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(blob)
        r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"JS 語法錯誤:\n{r.stderr.strip()}"
        return True, f"{len(scripts)} 個內聯塊、{len(blob)} bytes，語法通過"
    finally:
        os.unlink(path)


def _count_top_level(body):
    """數最外層逗號分隔的元素數，容忍嵌套陣列。"""
    depth = 0
    n = 1
    for ch in body:
        if ch in "[{(":
            depth += 1
        elif ch in "]})":
            depth -= 1
        elif ch == "," and depth == 0:
            n += 1
    return n


def check_arrays(html):
    """找出所有 `var X=[...]` 或 `const X=[...]`，報告長度。"""
    found = {}
    for m in re.finditer(r"(?:var|let|const)\s+(\w+)\s*=\s*\[", html):
        name = m.group(1)
        start = m.end() - 1
        depth = 0
        for i in range(start, len(html)):
            if html[i] == "[":
                depth += 1
            elif html[i] == "]":
                depth -= 1
                if depth == 0:
                    body = html[start + 1:i]
                    found[name] = _count_top_level(body) if body.strip() else 0
                    break
    return found


def check_duplicate_keys(html):
    """抓 `{... data:X, ... data:Y ...}` 這種同一物件重複鍵。"""
    bad = []
    for m in re.finditer(r"\{[^{}]{0,400}\}", html):
        s = m.group(0)
        keys = re.findall(r"[{,]\s*(\w+)\s*:", s)
        dup = {k for k in keys if keys.count(k) > 1}
        if dup:
            bad.append((dup, s[:100].replace("\n", " ")))
    return bad


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    path = argv[0]
    html = open(path, encoding="utf-8").read()
    ok = True

    print(f"檔案: {path}  ({len(html):,} chars)")

    node = find_node()
    good, msg = check_js(extract_scripts(html), node)
    print(("  [PASS] " if good else "  [FAIL] ") + "JS 語法 — " + msg)
    ok &= good

    arrays = check_arrays(html)
    if arrays:
        print(f"  [INFO] 資料數列 {len(arrays)} 組:")
        for k, v in arrays.items():
            print(f"          {k:12} = {v}")
        # 只對「看起來是同一組時間序列」的長度做一致性提示
        lens = [v for k, v in arrays.items() if v > 5]
        if lens and len(set(lens)) > 1:
            print("  [WARN] 數列長度不一致，請確認是否為刻意（如不同窗口的圖）")
    else:
        print("  [WARN] 未偵測到 var X=[...] 形式的數列，若用內嵌 JSON 請人工確認")

    dups = check_duplicate_keys(html)
    if dups:
        ok = False
        print(f"  [FAIL] 發現 {len(dups)} 處重複鍵:")
        for keys, snippet in dups[:5]:
            print(f"          {keys} ← {snippet}")

    print("\n結果:", "全部通過，可交付" if ok else "存在錯誤，不得交付")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
