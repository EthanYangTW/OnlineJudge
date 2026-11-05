# 將舊 FCU C 題庫匯入課堂

此流程供教師或部署代理操作。匯入器在執行時讀取私人題庫，不會把測資、選擇題正解、提示、詳解或參考程式碼複製到公開的 Git 儲存庫。一次指定一個**已建立的班級代碼**；不同班級可各自匯入同一題庫，並獨立設定配分、開放時間與學生。

## 匯入內容與發布

- 舊題庫目前有 20 題 C 標準輸入／輸出實作題、15 題 C 選擇題。匯入後分成 Chapter 2、3、4 的 C Quiz 與 C Lab，共六份 `practice` 練習。
- C 題由 QingdaoU JudgeServer 評測。每題第一組測資作為學生可見的範例，其餘測資只寫到後端的私人 `/data/test_case` 卷；學生頁面看不到正解、提示、詳解或預填解法。
- 題名會去掉開頭的題號；學生可自由切換題目。預設 C 題每題 10 分、選擇題每題 5 分。可在匯入時改預設分數，或匯入後由教師逐題修改。
- 六份練習建立後都是**未發布**。教師須檢查題目敘述、輸入輸出、範例、配分與評測，再於教師頁逐份發布。匯入器不會自行開放學生作答。

## Windows Docker 部署：私人檔案送入後端

先依 [部署交接](README_zh-TW.md)啟動三個服務儲存庫，並在教師頁建立班級。以下以班級代碼 `fcu-c-a` 與舊題庫路徑 `C:\FcuQduOJ\fcu-practice-clean` 為例；替換成實際路徑和班級代碼。PowerShell 在後端儲存庫執行：

```powershell
cd C:\FcuQduOJ\fcu-qduoj-fork
$old = 'C:\FcuQduOJ\fcu-practice-clean'
$backendId = docker compose --env-file fcu\.env -f fcu\compose.yaml ps -q oj-backend
if (-not $backendId) { throw 'oj-backend 尚未啟動' }
docker compose --env-file fcu\.env -f fcu\compose.yaml exec -T -u root oj-backend sh -lc 'umask 077; mkdir -p /tmp/fcu-bank; chmod 700 /tmp/fcu-bank'
docker cp "$old\src\lib\server\fcu\cLabs.json" "${backendId}:/tmp/fcu-bank/cLabs.json"
docker cp "$old\src\lib\server\fcu\cQuizzes.json" "${backendId}:/tmp/fcu-bank/cQuizzes.json"
docker cp "$old\problems" "${backendId}:/tmp/fcu-bank/problems"
```

`problems` 目錄只供匯入器讀取公開的 Input／Output 敘述；它的其他檔案可能含解答，整個暫存目錄須維持只有 root 可進入。**不要**把舊題庫放進前端 `dist`、公開上傳目錄、Git，或發給學生。

先檢查資料，不寫入資料庫：

```powershell
docker compose --env-file fcu\.env -f fcu\compose.yaml exec -T -u root oj-backend python manage.py import_fcu_c_bank --class-slug fcu-c-a --labs /tmp/fcu-bank/cLabs.json --choices /tmp/fcu-bank/cQuizzes.json --statements-dir /tmp/fcu-bank/problems --dry-run
```

輸出應顯示 `20 labs, 15 choices; 6 new unpublished quizzes`。確認班級代碼正確後，移除 `--dry-run` 再執行。若要自訂整批預設配分，可加 `--lab-points 20 --choice-points 4`；分數範圍是 1 至 1000。

```powershell
docker compose --env-file fcu\.env -f fcu\compose.yaml exec -T -u root oj-backend python manage.py import_fcu_c_bank --class-slug fcu-c-a --labs /tmp/fcu-bank/cLabs.json --choices /tmp/fcu-bank/cQuizzes.json --statements-dir /tmp/fcu-bank/problems
```

若還有 `fcu-c-b` 班，先在教師頁建立該班，再把上面指令的 `--class-slug` 改成 `fcu-c-b`。兩班的六份練習、C 題測資與成績各自獨立。重跑相同班級會跳過已建立且題目 ID 相同的練習，保留教師修改與既有作答；如果題目 ID 清單衝突，整次匯入會失敗，不覆寫既有題目或測資。要擴充新題，使用教師題目編輯功能建立新題與練習。

所有班級完成匯入後，刪除容器內的私人暫存檔：

```powershell
docker compose --env-file fcu\.env -f fcu\compose.yaml exec -T -u root oj-backend rm -rf /tmp/fcu-bank
```

## 教師檢查清單

1. 開啟 `/admin/classroom`，確認每個目標班級各有六份**未發布**練習。打開題目檢查標題、輸入輸出敘述、可見範例、題型與配分；不要把私人測資或答案貼進題幹。
2. 用隔離的測試學生帳號提交一題正確 C 程式、一題錯誤 C 程式與一題選擇題。確認多筆測資由 JudgeServer 評測、部分得分顯示正確，且學生只能看到自己的程式與成績。
3. 確認教師能看到全班成績與狀態，學生的公開排行榜只顯示姓名、分數與狀態，不顯示他人的 C 程式。再逐份發布要開放的練習。
4. 正式開放前備份 PostgreSQL 與 `test_cases` 卷。更改已提交 C 題的評測規則時，建立新題，避免既有成績與新測資混用。
