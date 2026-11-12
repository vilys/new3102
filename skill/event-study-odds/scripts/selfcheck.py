#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selfcheck.py — 腳本自身的回歸檢查（檢查代碼，不檢查報告）。

檢查項目：
  1. docstring 裡宣傳的 `from X import ...` 全部真的存在
     （2026-11 曾宣傳一個不存在的 baseline_path，照抄就 ImportError）
  2. 四支 CLI 的 --help 都 exit 0，且不產生 traceback
  3. build_events 對未知事件抛 ValueError 並附可用清單，不是裸 KeyError
  4. 無參數呼叫時印出 __doc__，不是 IndexError

用法:
    python scripts/selfcheck.py        # 全部通過 exit 0，否則 exit 1
"""

import ast
import importlib
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = ["market_data.py", "indicators.py", "event_study.py", "verify_report.py"]


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


def main():
    setup_console()
    groups = [
        ("docstring 的 import 都存在", check_docstring_imports),
        ("--help 皆 exit 0", check_help),
        ("無參數呼叫皆印說明", check_no_args),
        ("未知事件抛 ValueError 並附清單", check_unknown_event),
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
