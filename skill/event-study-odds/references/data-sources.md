# 資料源與環境（2026-10-31 實測）

所有端點與限制都在這個日期實際呼叫驗證過。標註「未驗證」的項目，代表本環境沒有對應 connector 或當次不可用，**不得假設它一定存在**。

---

## 一、日 K 線

### 港股（首選，穩定）

```
https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?param={sym},day,{start},{end},{cnt},qfq
```

- `sym` 形如 `hk01164`
- Header 必須帶 `User-Agent` 與 `Referer: https://gu.qq.com/`
- 回傳 `data.{sym}.qfqday` = `[[date, open, close, high, low, volume, ...], ...]`
- **欄位順序是 O-C-H-L，不是 OHLC。** 直接照抄別人的解析器最容易在這裡錯。
- 最後兩個元素通常是 `{}` 與換手率相關欄位，忽略即可。

**`cnt` 上限：1500。** 傳 2000/3000 會靜默降級（回約 640 根），或讓 `data` 從 dict 變成 list。這是本 skill 最容易踩的第一個坑。

**分段回溯法**（抓超過 1500 根）：

```
cursor = 指定結束日
loop:
    rows = fetch(end=cursor, cnt=1500)      # 回傳「以 cursor 為終點」的最後 1500 根
    merge(rows)
    if rows[0].date <= start or 無新增: break
    cursor = rows[0].date - 1 天
```

實測：01164 抓 2015-01-01 起，兩段即得 2,913 根（2015-01-02 → 2026-10-30），耗時約 5 秒。

### 美股 / ETF（Yahoo）

```
https://query1.finance.yahoo.com/v8/finance/chart/{SYM}?range=10y&interval=1d
```

- 解析 `chart.result[0]`：`timestamp` + `indicators.quote[0].{open,high,low,close,volume}`
- `close[i] is None` 的行必須跳過（停牌/無成交）
- 實測：URNM `range=10y` 得 1,736 根（2019-12-04 → 2026-10-30）

**不要用騰訊的 `usfqkline`**：同一組參數常只回 1–2 根，且不報錯，是最危險的靜默失敗。

### A 股（不建議用於統計）

騰訊 `fqkline?param=sz002466,...` 實測只回 1 根。取得 A 股日 K 需另尋來源；若只是要現價與漲跌幅，用報價接口即可。**若拿不到完整日 K，就不要對 A 股做事件研究**，改用港股/美股同業做交叉驗證。

### 當日盤中補 K（交易時段必做）

盤中日 K 接口未必含最新一根。用報價合成：

```
bar = [today, open, price, day_high, day_low, volume]
rows = [r for r in rows if r.date != today] + [bar]
rows.sort()
```

**不補的話，所有「當前狀態」其實是昨天的。** 這是第二個高頻錯誤。

---

## 二、實時報價

```
https://qt.gtimg.cn/q={sym}
```

編碼 **GBK**，內容以 `~` 分隔（`v_hk01164="1~中廣核礦業~01164~2.035~..."`）。

跨市場較穩定的欄位：

| 索引 | 含義 |
|---|---|
| 1 | 名稱 |
| 3 | 現價 |
| 4 | 昨收 |
| 5 | 今開 |
| 32 | 漲跌幅 % |
| 33 | 當日最高 |
| 34 | 當日最低 |

52 週高/低通常落在 47/48，但**不同市場會偏移**。不要硬編碼，用「高 ≥ 現價 ≥ 低 > 0」的合理性檢查定位，失敗就回 `None`，再從日 K 自行計算。

---

## 三、非價格資料（有 connector 才用，否則標註缺失）

| 資料 | 來源 | 備註 |
|---|---|---|
| 賣空比例、南向持股 | neodata（`港股卖空`、`沪深港通个券持股+卖空+交易流向`） | 需 connector |
| 券商評級/目標價 | etnet、富途、moomoo、`agentic_search` | 各家口徑差異大，務必標日期 |
| 產業價格（鈾、碳酸鋰） | 期貨交易所、產業媒體、`agentic_search` | 期貨代碼與現貨報價要分清 |
| 公司公告 | 港交所披露易 | 一手來源，優先用 |

**`agentic_search` 在本環境曾多次不可用**（tool not available）。寫報告前先確認工具存在；不存在就退回 `WebSearch`，並在局限中說明。

---

## 四、環境陷阱
| 陷阱 | 事實 |
|---|---|
| node 路徑 | 不要寫死版本號（例如 `.../22.22.2-6/node.exe`，`-6` 隨時會變）。優先用 PATH 的 `node`，否則用 glob 掃 `~/.workbuddy/binaries/node/versions/*/node.exe`；`verify_report.py` 已這樣做 |
| `WebSearch` 的 `freshness` | 只接受 `d[N]` / `m[N]` / `y[N]`，`w1` 會報錯 |
| Python 路徑 | 用 PATH 的 `python` / `python3`；不要依賴任何機器專屬的絕對路徑 |
| 檔案編碼 | 一律 UTF-8；Windows 下用 `open(..., encoding='utf-8')` |
| console 編碼 | 繁中 Windows 的 cp950 無法編碼簡體名稱。四支腳本已在進入點 `reconfigure(encoding="utf-8")`，不需使用者另設 `PYTHONIOENCODING` |
| `datetime.utcfromtimestamp` | 已棄用，改用 `datetime.fromtimestamp(t, timezone.utc)` |
| import 路徑 | `from indicators import ...` 需要 `scripts/` 在 `sys.path` 上。從 repo 根目錄執行時，先 `sys.path.insert(0, "skill/event-study-odds/scripts")`，或 `cd` 進該目錄 |

### TLS 與「空的 K 線陣列」

- **TLS 預設驗證憑證。** 被中間人替換價格會讓所有機率失真且不報錯。若某端點憑證確有問題，用 `--insecure` 或 `EVENT_STUDY_INSECURE_TLS=1` 顯式關閉，輸出會印警告。
- 請求區間早於上市日時，騰訊回的是 `{..., "day": []}`。這是**正常的歷史盡頭**，`_parse_tencent` 會回 `[]`，`fetch_hk` 的 `if not rows: break` 隨即停止，不會崩潰。
- 真正的結構異常（`data` 變 list、`day` 完全不存在、非 JSON）仍會 raise `FetchError`，並附可能原因。

---

## 五、抓取紀律（阿步的節流閥）

1. **單次研究標的上限 15 個**（人工紀律；腳本沒有批量入口，也沒有內建上限——避免一次打太多請求靠你自己數）。
2. **分段回溯上限 8 段**，腳本已內建 `MAX_SEGMENTS`。
3. **同一請求不重試超過 1 次**。限速、驗證碼、非預期回應 → 立即停手回報，不換 IP、不換 header 硬闖。
4. 抓到資料先印 `n` 與首尾日期。**沒有確認首尾日期的資料，不准進入統計。**
