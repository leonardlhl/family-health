# 私人家人網站接駁

網站源碼及空介面放 GitHub Pages。醫療紀錄、相片、備份及管理密鑰全部不進 GitHub。家人只可讀取目前已確認版本。GitHub 登入與 Supabase 連線是兩個不同步驟。

## 1. 建立私人資料服務

在自己的 Supabase 帳戶建立一個專用 project。在 SQL Editor 執行 `supabase/001_family_archive.sql`。它會建立家人名單、目前版本、完整版本資料表及私人 `family-health` 相片 bucket；家人角色只有 SELECT 權限。

在 Authentication 設定關閉 **Allow new users to sign up**。由管理人建立家人的 email/password 帳戶，或使用管理流程完成邀請及設定密碼。此網站只提供密碼登入；尚未實作處理 invitation/recovery callback，勿直接把邀請回呼設到網站而期望它自動完成密碼設定。不要共用管理帳戶。每位家人可有自己的登入。

帳戶建立後，在 SQL Editor 把已知 user UUID 加入名單：

```sql
insert into public.family_members (archive_id, user_id)
values ('family', 'REPLACE_WITH_AUTH_USER_UUID');
```

只有此名單內的使用者才可讀取。移除這一行即撤銷之後的讀取權；使用者已下載或截圖的資料不會被遠端刪除。

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

工具先上傳新版本所有私人相片，全部成功才呼叫資料庫交易切換目前版本；失敗時家人仍看上一個完整版本。舊相片及版本由後端保留但對家人不可讀；定期由管理人清理不再需要的版本，避免無限累積。這不代替本機完整備份。

## 4. 發布後驗證

發布前先用合成測試紀錄驗證三種身份：未登入、已登入但不在家人名單、名單內家人。前兩者必須無法讀取資料表及私人相片；家人只能讀，直接 REST 寫入亦必須被拒絕。再驗證管理人更新、撤銷家人、登出、登入過期與相片讀取。

本機自動測試覆蓋篩選、建置隔離、失敗時不切換版本、前端登入/refresh 及僅讀請求。它不能代替對實際 Supabase project 的 RLS 權限驗證。未完成 project 接駁與實測，不能稱為已上線。

官方參考：[GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site)、[Supabase RLS](https://supabase.com/docs/guides/database/postgres/row-level-security)、[私人相片 bucket](https://supabase.com/docs/guides/storage/buckets/fundamentals)。
