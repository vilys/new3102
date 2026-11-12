#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selfcheck.py — 腳本自身的回歸檢查（檢查代碼，不檢查報告）。

檢查項目：
  1. docstring 裡宣傳的 `from X import ...` 全部真的存在
     （2026-11 曾宣傳一個不存在的 baseline_path，照抄就 ImportError）
  2. 五支 CLI 的 --help 都 exit 0，且不產生 traceback
  3. build_events 對未知事件抛 ValueError 並附可用清單，不是裸 KeyError
  4. 四支分析腳本無參數呼叫時印出 __doc__，不是 IndexError
     （selfcheck.py 的無參數模式就是「執行檢查」，不在此列）

用法:
    python scripts/selfcheck.py            # 全部通過 exit 0，否則 exit 1
    python scripts/selfcheck.py --help     # 印出本說明，exit 0
    python scripts/selfcheck.py 報告.html  # 不接受檔案參數，exit 2
"""

import ast
import importlib
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = ["market_data.py", "indicators.py", "event_study.py",
           "verify_report.py", "selfcheck.py"]
# 無參數時「執行檢查」而不是「印說明」的腳本；要排除在 no-args 檢查之外，
# 否則 selfcheck 會叫起自己，無限遞歸。
NO_ARG_RUNS = {"selfcheck.py"}


def setup_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def check_docstring_imports():
    """回傳 (problems, checked)。"""
    problems, checked = [], 0
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    for name in SCRIPTS:
        path = os.path.join(HERE, name)
        tree = ast.parse(open(path, encoding="utf-8").read())
        doc = ast.get_docstring(tree) or ""
        for line in doc.splitlines():
            m = re.match(r"\s*from (\w+) import (.+?)\s*$", line)
            if not m:
                continue
            mod, names = m.group(1), [x.strip() for x in m.group(2).split(",")]
            try:
                M = importlib.import_module(mod)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{name}: 無法匯入模組 {mod}（{exc}）")
                continue
            for nm in names:
                checked += 1
                if not hasattr(M, nm):
                    problems.append(f"{name}: docstring 宣傳 `from {mod} import {nm}`，但 {mod} 沒有 {nm}")
    return problems, checked


def check_help():
    problems, checked = [], 0
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    for name in SCRIPTS:
        checked += 1
        r = subprocess.run([sys.executable, os.path.join(HERE, name), "--help"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env)
        if r.returncode != 0:
            problems.append(f"{name} --help exit {r.returncode}（應為 0）")
        if "Traceback" in (r.stdout + r.stderr):
            problems.append(f"{name} --help 產生 traceback")
        if "—" not in r.stdout and "--" not in r.stdout:
            problems.append(f"{name} --help 沒有印出說明")
    return problems, checked


def check_no_args():
    problems, checked = [], 0
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    for name in SCRIPTS:
        if name in NO_ARG_RUNS:
            continue
        checked += 1
        r = subprocess.run([sys.executable, os.path.join(HERE, name)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env)
        if "Traceback" in (r.stdout + r.stderr):
            problems.append(f"{name} 無參數呼叫產生 traceback")
        if not r.stdout.strip():
            problems.append(f"{name} 無參數呼叫沒有印出說明")
    return problems, checked


def check_unknown_event():
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import event_study as E
    try:
        E.build_events({"close": [1.0] * 5}, "ZZZ")
    except ValueError as exc:
        if "可用" in str(exc):
            return [], 1
        return [f"build_events 的 ValueError 未附可用清單：{exc}"], 1
    except KeyError as exc:
        return [f"build_events 對未知事件抛 KeyError（應為 ValueError）：{exc}"], 1
    return ["build_events 對未知事件沒有報錯"], 1


def check_baseline_level():
    """掃描 summary_table 輸出，確認「隨機基準」那一列的級別不是寫死的。

    2026-11 曾把基準列寫死成 'A'：n=7 的基準一邊把勝率遮蔽成「—」、
    一邊自稱 A 級。這裡用三種資料長度各跑一次，級別必須等於
    sample_level(bs['n'])[0]（B / C / D）。
    """
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import contextlib
    import io as _io
    import event_study as E
    from indicators import compute_all

    problems = []
    for n in (1200, 600, 400):
        rows = [[f"{2015 + i // 250}-{(i // 21) % 12 + 1:02d}-{i % 21 + 1:02d}",
                 10 + i * 0.01, 10 + i * 0.01, 10.2 + i * 0.01, 9.8 + i * 0.01, 1000.0]
                for i in range(n)]
        buf = _io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                E.summary_table(rows, compute_all(rows), kinds=("WR",))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"n={n} 跑 summary_table 失敗（{exc}）")
            continue
        lines = [ln for ln in buf.getvalue().splitlines() if "隨機基準" in ln]
        if not lines:
            continue
        parts = lines[-1].split()
        try:
            printed_n, printed_lvl = int(parts[1]), parts[-1]
        except (ValueError, IndexError):
            problems.append(f"n={n} 無法解析基準列：{lines[-1]!r}")
            continue
        expect = E.sample_level(printed_n)[0]
        if printed_lvl != expect:
            problems.append(
                f"n={n} 基準列（可用 n={printed_n}）印 {printed_lvl}，"
                f"應為 sample_level→{expect}")
    return problems, 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    setup_console()
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    bad = [a for a in argv if a not in ("-h", "--help")]
    if bad:
        print("錯誤：selfcheck.py 不接受檔案或選項參數——它檢查的是腳本自身。\n"
              f"  收到：{' '.join(bad)}\n"
              "  要檢查報告請用：python scripts/verify_report.py 報告.html",
              file=sys.stderr)
        return 2
    groups = [
        ("docstring 的 import 都存在", check_docstring_imports),
        ("--help 皆 exit 0", check_help),
        ("無參數呼叫皆印說明", check_no_args),
        ("未知事件抛 ValueError 並附清單", check_unknown_event),
        ("基準列級別與 sample_level 一致", check_baseline_level),
    ]
    failed = 0
    for label, fn in groups:
        problems, checked = fn()
        if problems:
            failed += 1
            print(f"[FAIL] {label}（{checked} 項）")
            for p in problems:
                print(f"        {p}")
        else:
            print(f"[PASS] {label}（{checked} 項）")
    print()
    if failed:
        print(f"結果：{failed} 組未通過")
        return 1
    print("結果：全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
