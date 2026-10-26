# 安康簿 · Family Health

Leonard manages grandparents' medical archives from this local Windows workspace and Codex. Family members in different locations view confirmed records and source photos through a private website protected by a revocable capability link (optional existing account login remains). They cannot edit clinical records; the feedback extension below permits separate medication observations. The local archive remains the master, with complete backup and restore. GitHub hosts application source and the empty static interface only; patient data and credentials must never enter Git history or site build output.

Photos are placed in separate grandpa/grandma inbox folders. Codex reads new photos on request and prepares structured draft visits. Important uncertain fields are marked for review. Confirmed visit dates, doctor/clinic, prescriptions, appointments and images are searchable. Historical prescriptions are not presumed currently taken. Originals are preserved; archive names become human readable. UI supports Traditional/Simplified Chinese without rewriting original medical content. In-app appointment reminders are sufficient; no push/calendar integration is requested.

The owner approved implementation on 2026-10-25 after discussion. Interface choices can be made autonomously: accessible large Chinese text, calm readable light surfaces, clear table/list hierarchy, mobile family view and desktop management. No invented patient data in the real archive. Demonstration data must be explicitly separated.

Privacy clarification: only aliases 爺爺 / 嬷嬷 identify patients. Do not retain patient real names, identity numbers, birth dates, contact or address fields. Preserve doctors and their diagnoses. Source photos must be reviewed and identifying regions irreversibly masked before archive/cloud publication. Raw inbound originals are temporary user-supplied processing material, never cloud content or part of the distributable archive. No original filename is exposed remotely. Record and attachment privacy approval is explicit; changing content invalidates approval. Use diagnosis and review_notes fields rather than guessing clinical information.

The user explicitly permits patient age and sex (nullable age, unknown/male/female sex); no date of birth needed. GitHub is signed in in the user's Chrome; the connector failed, so use normal browser/CLI workflow without blocking local development.


2026-10-25: separate operator and family retrieval entries. Both start with numbered 爺爺 / 嬷嬷 chapters, referencing Pocket Rhino Chapters with neutral grey surfaces. Family retrieval uses confirmed-only data and has no management controls. Medication rows retain source photos with compressed previews and access to full-resolution reviewed originals.

Final retrieval navigation: visits, medicines and reports; chapter defaults to visits. Overview and appointment management are operator-only. Medicines group by visit date newest first; left category index filters the current patient, with a horizontal mobile equivalent. Optional medicine.category is user-confirmed text, empty values show Uncategorized; no inferred clinical categories.

2026-10-25 latest: user wants links sent to family to open without login. Use a random 256-bit revocable capability in URL fragment; private Edge function checks its hash and active archive version for every snapshot/image request. Keep all medical tables and storage private. Operator manages create/rotate/revoke locally; share plaintext is local-private only, never Git/build/backups. Bare website retains optional invited-account login.


## 報告章節與容量（2026-10-26）

家人檢索版每個人物有「就診、藥物、報告」。報告按日期由新至舊，支援日期、內容、醫生與機構檢索，保留安全圖片及多頁 PDF。操作版另有草稿、逐頁匿名處理、標題／種類／摘要編輯和容量檢視。標準資料夾是日期加簡要內容；文件名加人物代稱及序號。姓名及身份資料不得保存到安全檔案或雲端。

報告收件根資料夾分爺爺與嬷嬷，支持有界遞迴掃描。大 PDF 有50MB/80頁和像素限制，以清晰可讀為先壓縮。相同媒體跨發布版本重用，不自動刪除舊版本。免費配額僅作參考，80%提示；原始收件和本機備份不算入雲端文件用量。免登入完整連結維持既有安全長度。


## 用藥感受互動（2026-10-26）

檢索版藥物行增加家人用藥感受：有效／無效／待定，另有可留空的500字備註。簡潔按鈕開啟單一小表格，儲存後顯示選擇、備註與記錄時間。未填寫不視為待定。醫療主檔仍只供檢索；只有感受可以新增修訂。共用連結標示家人填寫，無個人作者推斷。無效或不舒服不會自動停藥或改變處方狀態。

每次修改保留歷史、重試防重複、同時修改有衝突提示。感受雲端獨立儲存，本機重新載入／備份時讀回。原有病歷發布不會覆蓋感受。免費文字輸入不收集姓名或聯絡資料；本機預覽與示範頁不會寫入家人雲端。
