# 教授提供的 C 題：未發布草稿

這份題庫根據 2026-11-05 教授提供的八段教學範例整理。**目前只準備草稿，尚未指定班級、匯入正式伺服器或發布。**教授貼出的完整程式只用來核對題意，不會放進學生題面、前端、Git 或測資卷。私有匯入檔包含題目敘述與評測輸入／輸出，須另行保管。

| 草稿題目 | 自動評測的輸入／輸出約定 | 發布前須確認 |
| --- | --- | --- |
| 判斷直角三角形 | 三個整數；輸出 `Yes` 或 `No` | 是否接受非正邊長，以及指定輸出字串 |
| 判斷奇數或偶數 | 讀到 `0` 停止；每個非零數輸出一行 `Even`／`Odd` | `isEven()` 函式要求是否需人工檢查 |
| 5.19 星號矩形 | `side1 side2`；輸出 `side1` 列，每列 `side2` 個 `*` | 列數與欄數的方向 |
| 數字逐位輸出 | 1～32767；位數間恰好兩個空白 | 範例中的兩個空白與結尾空白容忍度 |
| 5.23 時間換算成秒 | 六個整數，兩組 12 小時制時間；輸出秒數差絕對值 | 這是「轉成 function」的考題；純輸出評測無法保證學生真的有寫函式，應人工檢查程式結構。跨半天時間先後也要先定義 |
| 貨幣換算 | 輸入一個美元整數；輸出日元與歐元，各兩位小數 | 原範例印 1～10 美元表格；這份草稿改成單筆輸入，才能用多組測資評測。**教授需確認改寫後才發布** |
| 5.28 數字總和 | 一個整數；輸出各位數字總和 | 負數與 0 的處理 |
| 最小公倍數 LCM | 兩個非負整數；輸出 LCM | GCD 函式要求需人工檢查；任一數為 0 時輸出 0 |

線上評測採標準輸入／輸出，不要求程式輸出 `Enter ...` 等互動提示。每題第一組測資是可見範例，後續測資只在後端私有卷。草稿每題預設 10 分、C17、2 秒、128 MiB，教師可在匯入後調整配分與題面。**未發布草稿不會出現在學生頁面。**

## 私有題庫檔

持久保存的檔案位於本機 `/Users/ethan/bugbounty-notes/fcu-practice-clean/.fcu-data/fcu-professor-2026-11-05.private.json`，權限 `0600`，有八題、62 組測資。舊專案已將 `.fcu-data/` 排除於 Git 之外。另有 `/private/tmp/fcu-professor-2026-11-05.private.json` 暫存副本；以持久保存的檔案為準。此檔不在公開儲存庫，也不能放入後端 Docker build context、前端 `dist` 或任何公開下載目錄。移到 Windows 前，先選擇有備份與存取控管的教師私有位置，並確認權限。匯入器不需要也不接受參考解答程式碼。

## 指定班級後匯入

先在教師頁 `/admin/classroom` 建立班級並取得班級代碼。以下以 Windows 的 `C:\FcuQduOJ\private\professor-bank.json` 與 `fcu-c-a` 為例，路徑和代碼須替換為實際值。此步驟只在**教授確認上述輸入／輸出約定後**執行。

```powershell
cd C:\FcuQduOJ\fcu-qduoj-fork
$backendId = docker compose --env-file fcu\.env -f fcu\compose.yaml ps -q oj-backend
docker compose --env-file fcu\.env -f fcu\compose.yaml exec -T -u root oj-backend sh -lc 'umask 077; mkdir -p /tmp/fcu-professor; chmod 700 /tmp/fcu-professor'
docker cp 'C:\FcuQduOJ\private\professor-bank.json' "${backendId}:/tmp/fcu-professor/bank.json"
docker compose --env-file fcu\.env -f fcu\compose.yaml exec -T -u root oj-backend python manage.py import_fcu_professor_bank --class-slug fcu-c-a --bank /tmp/fcu-professor/bank.json --dry-run
```

乾跑應顯示 `8 C questions; 1 new unpublished quiz`。確認班級與題面後，移除 `--dry-run` 重跑。可用 `--points 15` 指定整批預設配分。每個班級各執行一次；同班重跑會保留教師修改與已提交資料。若有同名題組但題目清單不同，指令會拒絕覆寫。匯入完成後刪除容器內的暫存檔：

```powershell
docker compose --env-file fcu\.env -f fcu\compose.yaml exec -T -u root oj-backend rm -rf /tmp/fcu-professor
```

匯入後先保持**未發布**，由教師檢查所有題面、範例、配分及上表歧義；以測試學生驗證正解與錯解、時間限制及私有測資後，再決定要發布成練習或另建限時測驗。現有介面不支援直接把 `practice` 題組切換成 `exam`，期中考要另建 `exam` 題組並設定開始／結束時間。函式結構要求需要教師審閱原始碼，不能只靠標準輸出分數判定。
