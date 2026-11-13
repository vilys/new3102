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
  5. Markdown 反引號裡提到的 repo 內檔案必須存在
     （2026-11 曾宣傳不存在的 package_skill.py，與第 1 項是同一類缺陷）
  6. 文件講「預設網格」時必須寫出 --help 實際印的數字
     （2026-11 曾把 CLI 的 3×2 寫成 6×5，那是函式預設不是 CLI 預設）
  7. 冷卻期掃描真的印出 len(stops)×len(targets) 格，且不再給死路提示

用法:
    python scripts/selfcheck.py            # 全部通過 exit 0，否則 exit 1
    python scripts/selfcheck.py --help     # 印出本說明，exit 0
    python scripts/selfcheck.py 報告.html  # 不接受檔案參數，exit 2
"""

import ast
import contextlib
import glob
import importlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# 這支腳本會活在兩個位置，掃描範圍必須跟著變：
#   repo 內      <repo>/skill/event-study-odds/scripts → ROOT=<repo>（含 README/CHANGELOG/cases）
#   已安裝       ~/.workbuddy/skills/event-study-odds/scripts → ROOT=skill 目錄
# 若一律往上三層，安裝後會變成掃 ~/.workbuddy 整個目錄，把別的專案備份
# 裡的舊 README 也算進來（實測會誤報 6×5）。
SKILL_ROOT = os.path.abspath(os.path.join(HERE, ".."))
_REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if (os.path.isfile(os.path.join(_REPO, "README.md"))
        and os.path.isdir(os.path.join(_REPO, "skill"))):
    ROOT = _REPO
else:
    ROOT = SKILL_ROOT
SCRIPTS = ["market_data.py", "indicators.py", "event_study.py",
           "verify_report.py", "selfcheck.py"]

# Markdown 引用檢查的排除規則。每一條都寫明理由，不要讓下一個人猜：
#   1. 含 < > * { } 或空白 → 佔位符（`<標的>.json`）或說明文字，不是真檔名
#   2. http/https 開頭 → 外部 URL，不是 repo 內檔案
#   3. 5 位代號的 .json（09696.json／01164.json）→ 使用者用 --out 產生的日 K，
#      本來就不會進 repo（.gitignore 另外擋掉）
#   4. wb-finance-skill/ 開頭 → 另一個 skill 的檔案，不在本 repo
_EXCLUDE_PREFIX = ("wb-finance-skill/",)
_DATA_JSON_RE = re.compile(r"^\d{4,5}(?:_[A-Za-z0-9]+)?\.json$")
_FILE_EXT_RE = re.compile(r"\.(?:py|md|html|json|csv|xlsx)$")
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


def _md_files():
    """回傳 [(相對路徑, 內容)]，涵蓋 repo 內所有 Markdown。"""
    out = []
    for path in sorted(glob.glob(os.path.join(ROOT, "**", "*.md"), recursive=True)):
        rel = os.path.relpath(path, ROOT).replace("\\", "/")
        if rel.startswith(".git/"):
            continue
        try:
            out.append((rel, open(path, encoding="utf-8").read()))
        except OSError:
            continue
    return out


def check_md_file_refs():
    """Markdown 反引號裡自稱是 repo 內檔案的，必須真的存在。

    第三輪的幽靈 baseline_path 在 docstring，當時靠 check_docstring_imports 擋住；
    第五輪的幽靈 package_skill.py 在 README，同一個病根換了出口。
    這裡把覆蓋面擴到 Markdown：以 basename 比對（文件常只寫檔名不寫路徑），
    排除規則見檔頭註解。
    """
    known = set()
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
        known.update(filenames)

    problems, checked = [], 0
    for rel, text in _md_files():
        for tok in re.findall(r"`([^`\n]+)`", text):
            tok = tok.strip()
            # `README.md:426` 這種帶行號的引用，先剝掉行號再比對
            tok = re.sub(r":\d+(?:-\d+)?$", "", tok)
            base = tok.replace("\\", "/").rsplit("/", 1)[-1]
            if not _FILE_EXT_RE.search(base):
                continue
            if any(ch in tok for ch in "<>*{} "):
                continue                      # 佔位符／說明文字
            if tok.startswith(("http://", "https://")):
                continue                      # 外部 URL
            if tok.startswith(_EXCLUDE_PREFIX):
                continue                      # 另一個 skill 的檔案
            if _DATA_JSON_RE.match(base):
                continue                      # 使用者產生的日 K
            checked += 1
            if base not in known:
                problems.append(
                    f"{rel}: 引用 `{tok}`，但 repo 內找不到 {base}")
    return problems, checked


def _cli_grid():
    """跑 --help 取得 CLI 實際預設網格，回傳 (標籤, "3×2")；拿不到回 (None, None)。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, os.path.join(HERE, "event_study.py"), "--help"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    m = re.search(r"目前 CLI 預設網格：(\d+)×(\d+)", r.stdout)
    if not m:
        return None, None
    return m.group(0), f"{m.group(1)}×{m.group(2)}"


def check_doc_defaults():
    """文件講「預設網格」時，同一行必須出現 --help 實際印的 N×M。

    2026-11 README 與 glossary 都把 CLI 預設寫成 6×5（那其實是 path_matrix()
    的函式預設），新手在 Step 3 看到 3×2、Step 4 卻讀到 6×5，會以為自己跑錯。
    單一來源是 --help，文件只能引用它印出來的數字。
    """
    _, want = _cli_grid()
    if not want:
        return ["event_study.py --help 沒有印出『目前 CLI 預設網格：N×M』"], 1

    problems, checked = [], 0
    for rel, text in _md_files():
        for i, line in enumerate(text.splitlines(), 1):
            if "預設" not in line or not re.search(r"\d+×\d+", line):
                continue
            checked += 1
            if want not in line:
                problems.append(
                    f"{rel}:{i} 講預設網格但沒寫 {want}：{line.strip()[:70]}")
    return problems, checked


def _synth_rows(n=600):
    """合成日 K：先跌 300 根建立 250 日低點，再在低點上方 0~11% 鋸齒。

    這樣會反覆觸發 WR（距 250 日低 5–15%），足以檢查掃描輸出格數。
    """
    rows = []
    for i in range(n):
        if i < 300:
            p = 10.0 - i * (5.0 / 300.0)
        else:
            p = 5.0 + 0.55 * ((i % 40) / 40.0)
        o, c = p, p + (0.02 if i % 2 else -0.02)
        rows.append([f"{2016 + i // 250}-{(i // 21) % 12 + 1:02d}-{i % 21 + 1:02d}",
                     o, c, max(o, c) + 0.03, min(o, c) - 0.03, 1000.0])
    return rows


def _run_event_cli(rows_path, extra):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, os.path.join(HERE, "event_study.py"), rows_path] + extra,
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)


def check_cli_grids():
    """CLI 的預設網格與冷卻期掃描，必須與 --help 說的數字一致。

    三件事一起驗，因為它們是同一條鏈：
      1. `--help` 印的 N×M == 不帶旗標實跑出來的格數
      2. sweep 帶 3×3 時，印出 3×3×len(cds) 格
      3. 掃描輸出不得再叫用戶加他已經加了的旗標

    舊版 `_cooldown_sweep` 只收單一 stop/target，提示卻叫用戶加
    `--stops/--targets` 對齊九格；加了也只換掉那一格。第 2 條就是為了
    讓這種「提示給的指令無效」在提交前 FAIL。
    """
    _, want_label = _cli_grid()
    if not want_label:
        return ["event_study.py --help 沒有印出『目前 CLI 預設網格：N×M』"], 1
    want_n = 1
    for part in want_label.split("×"):
        want_n *= int(part)

    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    problems, checked = [], 0
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_synth_rows(), fh)

        r = _run_event_cli(path, ["--events=WR"])
        checked += 1
        # 只取第一張矩陣：n<30 時工具會自動再附一段冷卻期掃描，那段的
        # 「目標+」不屬於預設網格，混進來會把 6 格誤算成 24 格。
        start = r.stdout.find("路徑矩陣")
        end = r.stdout.find("冷卻期敏感度")
        seg = r.stdout[start:end if end > start else len(r.stdout)]
        got_default = seg.count("目標+")
        if got_default != want_n:
            problems.append(
                f"--help 說預設 {want_label}，實跑卻印出 {got_default} 格")

        r2 = _run_event_cli(path, ["--events=WR", "--cooldown-sweep=5,10",
                                   "--stops=-9,-12,-15", "--targets=10,15,20"])
        checked += 1
        seg2 = r2.stdout[r2.stdout.find("冷卻期敏感度"):]
        got_sweep = seg2.count("淨")
        if got_sweep != 3 * 3 * 2:
            problems.append(
                f"冷卻期掃描印了 {got_sweep} 格，應為 18 格（3×3×2）")
        if "請加" in seg2 or "要對齊" in seg2:
            problems.append("冷卻期掃描仍叫用戶加旗標：提示可能是死路")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return problems, checked


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
        ("Markdown 引用的 repo 檔案都存在", check_md_file_refs),
        ("文件預設網格與 --help 一致", check_doc_defaults),
        ("冷卻期掃描格數與提示有效", check_cli_grids),
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
