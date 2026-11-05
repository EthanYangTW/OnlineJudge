# FCU C 評測器壓力測試

此測試只連到主機 `127.0.0.1:5482` 上的 FCU Compose 測試站。它會建立名稱以 `fcu-load-` 開頭的獨立班級、隱藏測資及測試帳號，使用真正的 `/api/submission` 同時送出 C 程式，再由 HTTP 確認評測結果、學生程式草稿、已提交程式、個人成績、教師成績表，以及學生無法讀取彼此的提交。結束時只刪除本次建立的測試資料。

在三個映像都已建置、`docker compose ps` 顯示服務正常後，於後端儲存庫執行。主機需有 Python 3 與 Docker Compose；此腳本只用 Python 標準函式庫。`fcu/.env` 是私有檔案，不可提交 Git。

```powershell
py -3 fcu\stress_classroom.py --env-file fcu\.env --students 1
py -3 fcu\stress_classroom.py --env-file fcu\.env --students 22
```

macOS／Linux 使用 `python3` 與 `/` 路徑。若本機採不同的迴圈位址埠，加上 `--base-url http://127.0.0.1:<埠號>`。腳本拒絕公開網址，避免對正式學生站誤送測試資料。

每兩位測試學生分別提交完整正確程式與只通過一組隱藏測資的程式；教師可看到滿分 30 或部分分數 10。輸出列出 HTTP 提交與評測完成時間的中位數、95 百分位數及最大值。若失敗，先查 `docker compose ... logs oj-backend oj-judge` 與 Judge Server 健康狀態，再修正問題重跑。

需要保留本次測試班級做介面檢查時才加 `--keep`。腳本會只對這個測試班級暫停 IP 綁定，讓本機瀏覽器可登入；不影響正式班級。測試帳密會寫進主機暫存目錄下權限 `0600` 的 JSON 檔，輸出只顯示檔案路徑。檢查完以輸出中的路徑清除：

```powershell
py -3 fcu\stress_classroom.py --env-file fcu\.env --students 1 --keep
py -3 fcu\stress_classroom.py --env-file fcu\.env --cleanup-from <上一步的 JSON 完整路徑>
```

平常不加 `--keep`，腳本會自動清理；不要把測試帳密 JSON 寄出或提交 Git。

此測試評估 22 個同時送出的編譯／評測請求；正式考場仍須另外從兩台實際學生電腦確認反向代理來源 IP、時間到後拒收、斷線重連，以及校內網路隔離。
