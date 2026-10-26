# 私人家人網站接駁

網站源碼及空介面放 GitHub Pages。醫療紀錄、相片、備份及管理密鑰全部不進 GitHub。家人透過私人分享連結直接查看目前已確認版本，毋須登入；持有連結等於有閱讀權限。所有編輯、整理及同步留在本機管理版。

## 1. 建立私人資料服務

在自己的 Supabase 帳戶建立一個專用 project。在 SQL Editor 依次執行 `supabase/001_family_archive.sql`、`supabase/003_family_share_links.sql`、`supabase/004_reports_storage.sql`。它們建立私人版本資料、分享 token hash、報告／PDF 支援及容量統計。分享表只儲存 SHA-256 hash；更換／撤銷 RPC 僅供本機管理密鑰使用。004 保留 bucket 私人、放寬為最多 50,000,000 bytes 的已匿名 PDF，並容許同一個不可覆寫檔案被多個版本引用。已有分享連結及版本不會被修改。

在 Edge Functions 以 **Via Editor** 建立名稱為 `family-share` 的 function，完整貼入 `supabase/functions/family-share/index.ts`。這是一個無外部依賴的單檔。將此 function 的 **Verify JWT** 關閉；CLI 對應設定已放在 `supabase/config.toml` 的 `verify_jwt = false`。它改為驗證 POST body 內的私人 token，並非開放匿名讀取。這個部署方式與設定見 [Dashboard 官方教學](https://supabase.com/docs/guides/functions/quickstart-dashboard)及 [function 驗證設定](https://supabase.com/docs/guides/functions/auth-headers)。

Function 使用平台的 `SUPABASE_URL` 及 `SUPABASE_SERVICE_ROLE_KEY`。若使用新的 server secret，可在 function secret 設定 `FAMILY_SHARE_SECRET_KEY` 作沒有內建 service-role key 時的替代。這些密鑰不得放網站設定或 GitHub Variables。Function 不記錄 token、request body 或後端錯誤內容，只會以 GET 讀取資料表及私人相片。

私人網址格式為 `https://YOUR_SITE/#share=TOKEN`，token 為 32 個隨機 bytes 編成 43 字元 base64url。token 只放網址 fragment 及 POST body；不放 query string，不儲存在雲端明文。撤銷或更換後，舊 token 的下一次讀取會被拒絕；已下載或截圖的內容無法收回。不要把私人完整連結放進公開訊息、Git 或日誌。

目前允許的瀏覽器 Origin 精確為 `https://leonardlhl.github.io`、`http://127.0.0.1:8767`、`http://localhost:8767`。缺少 Origin、`null`、其他 host/port 均被拒絕。Origin 是瀏覽器額外限制，真正的閱讀權限來自高強度 token。更換網站網域時，需更新 function 的明確名單後重新部署。

Function API：POST JSON `{token,action:"snapshot"}` 回傳 schema v1 資料及 `published_at`；`{token,action:"file",attachmentId,thumbnail:true|false}` 回傳圖片 bytes。它不接受路徑或 archive ID。每次核對未撤銷／未過期連結、目前版本及相片 manifest；相片經 MIME、大小與 checksum 核對後直接傳送，沒有公開／簽名網址。回應 `Cache-Control: no-store`；失效連結回傳一致的 403；讀取期間剛好發布新版本，或舊畫面的相片已不在目前版本，則回傳可重試的 503。

以下 email/password 名單只屬仍保留的舊登入入口，私人分享連結無需建立家人 Auth 帳戶：

在 Authentication 設定關閉 **Allow new users to sign up**。由管理人建立家人的 email/password 帳戶，或使用管理流程完成邀請及設定密碼。此網站只提供密碼登入；尚未實作處理 invitation/recovery callback，勿直接把邀請回呼設到網站而期望它自動完成密碼設定。不要共用管理帳戶。每位家人可有自己的登入。

帳戶建立後，在 SQL Editor 把已知 user UUID 加入名單：

```sql
insert into public.family_members (archive_id, user_id)
values ('family', 'REPLACE_WITH_AUTH_USER_UUID');
```

使用舊登入入口時，只有此名單內使用者才可直接讀取資料表。移除名單會撤銷該登入入口的讀取權；私人分享連結需另外撤銷，兩種授權互不取代。

## 2. 網站公開設定

網站只需 Supabase Project URL 與 **anon / publishable** key。不要填 service_role / secret key。GitHub repository 的 Settings → Secrets and variables → Actions → Variables 加入：

| Variable | Value |
|---|---|
| `HEALTH_SUPABASE_URL` | `https://YOUR_PROJECT.supabase.co` |
| `HEALTH_SUPABASE_ANON_KEY` | Project API settings 的 anon 或 publishable key |

開啟 GitHub Pages，Source 選 GitHub Actions。本專案提供 `.github/workflows/pages.yml`，只部署 `web/` 的明確檔案清單。建置會拒絕 service-role key，亦不會收錄資料檔或 `private/`。初次部署時，兩個公開設定都未填可先發布「尚未接駁」畫面；此時無法登入或讀取任何病歷。只填其中一個或格式不正確會令建置失敗。

本機測試建置：`python tools/build_site.py`。沒有雲端設定時，網站會顯示未接駁並拒絕讀取資料。正式發布用 `python tools/build_site.py --require-cloud`，缺少設定會停止。

GitHub Pages 本身的介面可以公開；它不包含病歷。Private repository 不會自動令一般 Pages 網站私人。是否能從 private repository 發布取決於 GitHub 方案；不應為了發布而把含其他內容的既有 repository 改成公開。建議此 app 使用獨立、只含程式的 repository。

## 3. 本機管理人同步

管理密鑰只放本機環境變數 `HEALTH_SUPABASE_SERVICE_KEY`，或已被忽略的 `private/cloud.json`。不要在對話貼出密鑰、不要放入 `web/config.js`、GitHub Variables 或前端。

`private/cloud.json` 的格式（這只是欄位示例）：

```json
{
  "url": "https://YOUR_PROJECT.supabase.co",
  "anon_key": "OPTIONAL_PUBLIC_PUBLISHABLE_KEY_FOR_LOCAL_STATUS",
  "service_key": "LOCAL_SERVER_CREDENTIAL_ONLY",
  "archive_id": "family"
}
```

同樣可使用 `HEALTH_SUPABASE_URL`、`HEALTH_SUPABASE_SERVICE_KEY`、`HEALTH_ARCHIVE_ID` 環境變數。管理密鑰屬於你自己的專用 project，只用於同步已確認紀錄。

`anon_key` 為選填公開 key，只供本機顯示接駁狀態；網站發布仍由 GitHub Variables 注入公開設定。`service_key` 絕不回傳到本機網頁或家人網站。

先驗證，無網絡寫入：

```text
python tools/publish.py --dry-run
```

完成來源、身份資料遮蓋與診斷／藥物核對後，執行：

```text
python tools/publish.py
```

工具只取 `HealthStore.confirmed_snapshot()`，再逐欄篩選。病人只保留「爺爺／嬷嬷」、年齡與性別；不發布身份欄位、原檔名、原始 OCR 或本機路徑。自由文字仍需管理人核對，程式不能保證從診斷／備註自動找出每種身份資料。相片必須完成私隱檢查，且 checksum 與目前檔案一致。

工具使用包含附件 ID、實際檔案 SHA-256 及格式的固定儲存路徑。新版本可重用已核實的舊相片／PDF；僅缺少的檔案才上傳。重用前串流讀回檔案並核對大小與 SHA-256，不單憑檔名或大小判斷。相片、PDF 與縮圖各自核對。遇到同路徑但 bytes 不符會停止，不覆寫亦不刪除。

所有檔案就緒才以資料庫交易切換目前版本；失敗時家人仍看上一個完整版本。內容完全沒改時，工具核對遠端目前版本、完整 manifest、所有檔案及目前版本指標後跳過發布。這個完整性核對會使用讀取流量；儲存空間與月度流量屬不同限制。

004 之前每版本各自儲存的舊副本會繼續保留及計入容量。新路徑重用只減少之後的重複儲存；新增或修改內容仍會增加容量。程式沒有自動刪除歷史或孤立檔案，也不會更換私人分享 token。家人仍只能透過目前 manifest 讀取，知道舊檔案路徑並不取得讀取權。這不代替本機完整備份。

藥物列表使用獨立壓縮預覽：已完成私隱處理的相片產生最長邊 960 px、JPEG 品質 86 的無 metadata 副本。完整解像度歸檔相片仍保留，點開相片時讀取完整版本。預覽亦存在私人 bucket，與原圖使用同一登入及版本權限，並在整批成功後才公開給已授權家人。未確認／未完成私隱檢查的相片不會產生供雲端發布的預覽。

報告使用 schema v1 的選填 `reports` 陣列，舊版本沒有這個欄位時當作空陣列。只有已確認、已完成私隱檢查、同一人物的來源才可發布。PDF 必須先由本機程式把全部頁面重建為像素頁面，去除隱藏文字、metadata、連結、註解及內嵌附件；單一安全 PDF 上限 50,000,000 bytes，圖片仍為 25 MiB。PDF 首頁 JPEG 縮圖與完整 PDF 分開儲存；兩者都檢查 MIME、大小及 SHA-256。私人服務沒有接受任意路徑的下載入口。

## 4. 容量查看

本機設定頁的 `GET /api/storage` 需要管理版 CSRF；命令列可用 `python tools/storage.py --data-dir YOUR_PRIVATE_DIRECTORY`。它只量度檔案大小與查詢容量，沒有刪除或上傳操作。雲端統計由僅 service_role 可執行的 `family_storage_usage('family')` 回傳。

`bucket_bytes` 包含 `family-health` bucket 所有保留版本及未被 manifest 引用的檔案；`archive_bytes` 是目前家庭前綴，`current_bytes` 是目前版本實際引用的檔案。`retained_versions`、`unreferenced_files` 只作報告，不自動清理。若 Storage metadata 缺少大小，`unknown_size_files` 及 `usage_incomplete` 會標示計數不完整；服務不可用時用量保持 null，不能當成零。

容量條以 Free 方案 1,000,000,000 bytes 檔案及 500,000,000 bytes 資料庫作明確標示的參考，80% 提示。它不是已讀取帳單的真實配額，也不量度其他 buckets、月度流量或帳戶其他 projects。資料庫數字是整個目前 project 的資料庫大小。本機 source 只計已管理的 inbox/staging；archive、縮圖與備份分開計算，外部收相資料夾不會被容量工具掃描。

## 5. 發布後驗證

新 project 執行 `001_family_archive.sql` 後，可在 SQL Editor 以 postgres 身份一次執行完整 `supabase/verify_readonly.sql`。它用可回滾的合成資料及 JWT claims 測試未登入、名單外、名單內身份，並核對舊版本／其他家庭隔離、家人 SELECT-only 權限、發布 RPC 及相片寫入限制。它不建立 Auth 帳戶、不設定密碼、不上傳相片；storage 只有測試用 metadata 行。

為避免建立測試 Auth 帳戶，驗證交易暫時將家人名單的 Auth 外鍵延至交易結束檢查。檔案最後固定 `ROLLBACK`，連外鍵設定一起還原；不要分段執行或改成 COMMIT。若中途報錯，立即單獨執行 `ROLLBACK;`。成功會顯示 `PASS`。這只證明 SQL 權限，仍需下面的真實登入與 HTTP 測試。

尚無正式紀錄時，可執行 `supabase/002_bootstrap_empty.sql` 建立只有兩個稱呼、空紀錄／相片的版本。重複執行會保留已有發布版本；家人透過有效私人連結看到空白章節。沒有有效連結或舊入口的名單授權，即使有空白版本也不能讀取。

私人連結測試：`node --test tests/test_share_function.mjs` 在離線 mock 後端驗證失效／撤銷／過期 token、Origin、其他家庭及舊版本、寫入請求、相片 manifest／MIME／checksum、讀取途中撤銷及發布競態。這不代替已部署 function 的實測；上線時另驗證有效連結、隨機連結、撤銷後的舊連結，以及網站仍無管理操作。Function 測試工具需明確帶 `Origin: https://leonardlhl.github.io` 及 `Content-Type: application/json`。

發布前先用合成測試紀錄驗證三種身份：未登入、已登入但不在家人名單、名單內家人。前兩者必須無法讀取資料表及私人相片；家人只能讀，直接 REST 寫入亦必須被拒絕。再驗證管理人更新、撤銷家人、登出、登入過期與相片讀取。

本機自動測試覆蓋篩選、建置隔離、失敗時不切換版本、前端登入/refresh 及僅讀請求。它不能代替對實際 Supabase project 的 RLS 權限驗證。未完成 project 接駁與實測，不能稱為已上線。

官方參考：[GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site)、[Supabase RLS](https://supabase.com/docs/guides/database/postgres/row-level-security)、[私人相片 bucket](https://supabase.com/docs/guides/storage/buckets/fundamentals)。
