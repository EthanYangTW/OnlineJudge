# FCU C 課堂：QingdaoU 三個 Fork 部署交接

此版本以 QingdaoU OnlineJudge 的 Django、Vue 與 JudgeServer 為基礎。教師操作與出題流程見[教師手冊](https://github.com/EthanYangTW/OnlineJudgeFE/blob/master/docs/FCU_CLASSROOM_zh-TW.md)；舊 FCU 題庫的私人匯入程序見[題庫匯入手冊](IMPORT_C_BANK_zh-TW.md)，教授新題草稿見[待確認題庫](PROFESSOR_C_DRAFTS_zh-TW.md)。本文件處理安裝、網路、驗證與備份。同一伺服器可建立多個班級，各班獨立管理學生、題目與分數。

## 必備儲存庫與資料

將下列三個儲存庫**並列**放在同一個資料夾，名稱維持如下：

| 資料夾 | GitHub |
| --- | --- |
| `fcu-qduoj-fork` | `https://github.com/EthanYangTW/OnlineJudge` |
| `fcu-qduoj-fe-fork` | `https://github.com/EthanYangTW/OnlineJudgeFE` |
| `fcu-qduoj-judge-fork` | `https://github.com/EthanYangTW/JudgeServer` |

需要 Windows Docker Desktop 的 **Linux containers** 與新版 `docker compose`（支援 BuildKit `additional_contexts`）。首次建置及下載套件時需要網路。考試時請由學校網路設備阻止學生網段連到外網，只允許連考試站與必要校內網路；網站本身不能強制關閉學生裝置的 Wi-Fi。資料庫、Redis、評測器及語法檢查器沒有對學生公開的主機連接埠。

## 首次建置（Windows PowerShell）

若三個儲存庫已位於 `C:\Users\chan\FcuQduOJ`，可在後端儲存庫執行 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\fcu\Install-FcuClassroom.ps1`。安裝程式會產生私有隨機環境值、建置前端與兩種 C 評測容器、確認服務健康，並把信任代理範圍縮到實際偵測到的 Docker 轉送位址；**不會更改 Caddy 或目前的 80 埠站台**。初始教師密碼只在 Windows 私有的 `fcu\.env` 中；請在該主機上由授權教師讀取，勿貼到聊天、Git 或學生頁。日後更新用相同指令加 `-Update`，會先備份資料。

在三個儲存庫的共同上層目錄執行；以下以此主機預定的 `C:\Users\chan\FcuQduOJ` 為例：

```powershell
cd C:\Users\chan\FcuQduOJ\fcu-qduoj-judge-fork
git submodule update --init --recursive

cd C:\Users\chan\FcuQduOJ\fcu-qduoj-fe-fork
npm install --legacy-peer-deps --no-audit
$env:NODE_OPTIONS = '--openssl-legacy-provider'
npm run build:dll
npm run build

cd C:\Users\chan\FcuQduOJ\fcu-qduoj-fork
Copy-Item fcu\.env.example fcu\.env
notepad fcu\.env
```

在 `fcu\.env` 中將三個 `REPLACE_WITH_...` 全部改成**不同的長隨機值**。`FCU_TEACHER_USER` 是教師帳號；`FCU_TEACHER_PASSWORD` 是初始教師密碼，至少 12 字元。此檔不可放進 Git、寄給學生，亦不可放到前端程式。啟動程式會拒絕範例密碼與上游的 `root/rootroot` 預設密碼。

```powershell
docker compose --env-file fcu\.env -f fcu\compose.yaml config --quiet
docker compose --env-file fcu\.env -f fcu\compose.yaml up --build -d
docker compose --env-file fcu\.env -f fcu\compose.yaml ps
```

第一次 JudgeServer 建置會下載並編譯 Judger。以 `http://127.0.0.1:5482/admin/` 在伺服器本機測試教師登入，以 `http://127.0.0.1:5482/classroom` 測試課堂。評測器需出現在管理頁的 Judge Server 清單並顯示正常；`oj-syntax` 是獨立的 C 語法與自訂輸入 Run 服務，不會登錄成正式評測器。正式得分由 `oj-judge` 使用私人測資計算。

## 80 埠與真實學生 IP

Compose 預設只把網站綁定到主機 `127.0.0.1:5482`。在既有 Caddy 的 HTTP 80 站台，驗證新版本後將上游改為：

```caddyfile
http://192.168.1.8, http://100.69.46.95 {
    reverse_proxy 127.0.0.1:5482 {
        header_up X-Real-IP {remote_host}
    }
}
```

若另有 HTTPS/Tailscale 站台，使用相同 `reverse_proxy` 與 `X-Real-IP` 設定。Caddy **必須覆寫**使用者自行提供的 `X-Real-IP`；Compose 中的 QDU nginx 會將這個值轉交 Django。不要把 `FCU_BIND` 改成 `0.0.0.0`，否則學生可能繞過代理自行送出假 IP。只有實際的 Caddy/容器內代理位址可加入 `FCU_TRUSTED_PROXY_CIDRS`。

網站地址以實際 Caddy 設定為準：校內可從 `http://192.168.1.8/` 的 80 埠進站並由 Caddy 轉至 HTTPS，Tailscale 可用 `http://100.69.46.95/`；若站台已啟用 HTTPS，就使用它提供的 `https://` 地址。不要假設學生可從校內直連 Tailscale 位址。

從兩台不同的學生電腦測試：同一學號在第一台登入後，第二台必須被拒絕；教師重設該生 IP 後，第二台才可登入。若兩台都顯示同一個 IP，先修正 Caddy 與 nginx 的轉送設定，不可把 IP 鎖視為有效。

## 教師建立課堂與測驗

1. 教師進入 `/admin/classroom`，建立每個班級的代碼，例如 `fcu-c-a`、`fcu-c-b`；在頁面頂端切換正在編輯的班級。目前同一學號帳號只可屬於一個班級，因此不同班的名冊學號不能重複。
2. 各班分別匯入 UTF-8 CSV，欄位必須是 `login,name,class,password`。若使用「學號:學號」，每列的 `login` 與 `password` 填同一學號。匯入 API 會雜湊密碼；教師應妥善保管或銷毀原始 CSV。重匯且密碼未變會保留現有 IP 綁定；密碼改變會登出該學生並清除其 IP 綁定。
   部署代理可用同一套驗證規則執行私有檔案匯入：`docker compose --env-file fcu\.env -f fcu\compose.yaml exec -T oj-backend python manage.py provision_fcu_class --class-slug fcu-c-2026 --class-name "FCU C 2026" --roster /tmp/fcu-roster.csv`。先用 `docker cp` 把受保護的 CSV 複製到後端容器 `/tmp/fcu-roster.csv`，匯入後立即刪除容器內副本。指令不輸出密碼，班級與名冊匯入會在同一筆資料庫交易中完成。
3. 建立 `practice` 練習或 `exam` 測驗，設定開放時間及截止時間。每題可直接跳轉，不必完成前一題。
4. 選擇題在課堂頁輸入題幹、選項、正解與配分。C 題先在 QingdaoU 的同一 Contest 建立 **OI、C-only** Problem 並上傳測資，再於課堂題目填入該 Problem 的**數值資料庫 ID**及課堂配分。不要把測資或標準答案放到題幹。
5. 教師可在 **Quizzes** → **Open** → **Quiz settings** 直接改測驗標題、截止時間與發布狀態；在題目列按 **Edit** 直接改題幹、排序、發布狀態及配分。第一筆提交後題目內容與評測連結會鎖定，配分仍可修改並重算既有成績；要更換測資請建立新題。
6. 在 **Scores** 看教師可見的全班成績、程式與提交歷史，必要時匯出 CSV。練習模式的學生可看同班其他人的**姓名、分數、提交狀態**與排行榜；不能看其他人的程式。限時測驗的同學資料及排行仍不向學生公開。
7. 班級擁有者可在 **Assistants** 建立或指派班級限定的助教帳號；助教只管理獲指派的班級，不取得全站管理權。請由教師設定助教初始密碼，勿把助教當作一般學生匯入名冊。

若要在其他班級建立既有 FCU 的 C 與選擇題題庫，先建班，再依[題庫匯入手冊](IMPORT_C_BANK_zh-TW.md)在容器內對每班執行一次。本機使用已移除 **Add Two Numbers** 的私人版本，包含 19 題 C 與 15 題選擇題；新匯入的 6 份練習預設未發布，教師需驗收後逐份開放。

2026-11-06 已在 Windows 主機的 `fcu-c-2026` 班級匯入並發布 **Professor C Practice**，包含教授提供的 8 題英文 C 練習及 62 組伺服器測資；學生現在可登入練習。42 位名冊學生已匯入，登入名稱與初始密碼皆為學號。班級限定助教帳號為 `ta`，教師帳號為 `chen`；密碼不記載於本文件或 Git。教師可在 **Quizzes** 檢查題面、配分及輸入輸出規格，或在 **Quiz settings** 取消發布。期中考若採用這些題，須另建限時 **Exam** 題組、使用不同測資、設定截止時間並驗收。私人題庫與測資不可放進 Git、前端或可下載目錄；但 Practice 學生送出後，可透過受權限控管的 API 查看自己的每組評測輸入及預期輸出，故這些測資不應再視為考試秘密。涉及「必須使用函式」的要求仍需教師審閱學生程式碼。題目待確認事項見[教授草稿說明](PROFESSOR_C_DRAFTS_zh-TW.md)。

同班另已匯入並發布 Chapter 2、3、4 的 C Lab 與 C Quiz 共 6 份練習：19 題 C 實作與 15 題選擇題，**不含 Add Two Numbers**。因此學生目前可見 7 份練習、42 題。Windows 私人篩選題庫位於 `C:\Users\chan\FcuQduOJ\private\fcu-chapter-bank-stage`，可供日後另一班匯入；不要改用未篩選的原始 `cLabs.json`。

同日 Windows 主機以 22 位隔離測試學生並行送出 C 程式：11 位滿分、11 位部分得分、0 個系統錯誤；評測完成時間中位數 5.47 秒、95 百分位數 6.14 秒。測試班級已移除。Caddy 的 80 埠已切換至新服務；Tailscale 網址為 `http://100.69.46.95/classroom`，校內網路應使用既有 `https://192.168.1.8/classroom`，並從實際學生電腦驗證可達性與來源 IP 鎖定。

### 外網臨時驗收

Windows 主機已安裝官方簽章的 `cloudflared`，以排程工作 `FCU-C-Public-Quick-Tunnel` 將本機 `127.0.0.1:5482` 暫時連到 Cloudflare。依班級需求，**沒有 Cloudflare 電子郵件驗證**；持有網址者可打開登入頁，學生仍須輸入學號及密碼。從 Windows 私有日誌 `C:\Users\chan\FcuQduOJ\private\cloudflared-quick.log` 找 `https://...trycloudflare.com` 即為當前網址。排程工作可用 Windows 工作排程器啟動或停止；停止後外網入口立即失效。Quick Tunnel 重新建立時網址可能變更，且沒有可用性保證。2026-11-06 應使用者要求，`fcu-c-2026` 的整班 IP 綁定已關閉，因此同一帳號可從不同網路登入。**學號亦為初始密碼，公開外網入口使知道學號的人可直接登入該學生帳號；正式使用前應改發每人不同的密碼。**若需固定網址與可靠外網服務，應改用具固定網域的正式 Tunnel。

## 驗收與備份

先用隔離測試帳號驗證：正確 C、錯誤 C、編譯錯誤、逾時、多組測資、部分得分、截止後拒收、關閉測驗後成績與程式仍可查、學生無法看其他人的程式或考試排行。練習排行只應出現同班姓名、分數及狀態。語法檢查只回傳編譯診斷、不計分；Run 只對學生提供的標準輸入執行、不計分；其獨立容器**不可掛載** `/test_case`。

正式考試前依[壓力測試手冊](STRESS_zh-TW.md)至少模擬 22 位學生並行送出，檢查佇列時間、JudgeServer 健康狀態、資料庫成績與重新啟動後的程式碼。Compose 命名卷 `postgres_data`、`backend_data`、`test_cases` 必須一起備份；不要只備份 Redis。更改課堂配分會重新計算既有成績，教師應先用測試班級確認；已提交的 QDU 題目評測規則被鎖定，改測資時請建立新題。

更新時先備份，再對三個 fork 執行 `git pull`、重建前端 `npm run build:dll`/`npm run build`，最後 `docker compose ... up --build -d`。若需要回復舊版，保留原先的 Caddy 上游設定與資料卷備份；不可把新舊應用同時寫入同一資料庫。
