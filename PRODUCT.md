# 安康簿 · Family Health

Leonard manages grandparents' medical archives from this local Windows workspace and Codex. Family members in different locations view confirmed records and source photos through a private authenticated website. They cannot edit. The local archive remains the master, with complete backup and restore. GitHub hosts application source and the empty static interface only; patient data and credentials must never enter Git history or site build output.

Photos are placed in separate grandpa/grandma inbox folders. Codex reads new photos on request and prepares structured draft visits. Important uncertain fields are marked for review. Confirmed visit dates, doctor/clinic, prescriptions, appointments and images are searchable. Historical prescriptions are not presumed currently taken. Originals are preserved; archive names become human readable. UI supports Traditional/Simplified Chinese without rewriting original medical content. In-app appointment reminders are sufficient; no push/calendar integration is requested.

The owner approved implementation on 2026-10-25 after discussion. Interface choices can be made autonomously: accessible large Chinese text, calm readable light surfaces, clear table/list hierarchy, mobile family view and desktop management. No invented patient data in the real archive. Demonstration data must be explicitly separated.

Privacy clarification: only aliases 爺爺 / 嬷嬷 identify patients. Do not retain patient real names, identity numbers, birth dates, contact or address fields. Preserve doctors and their diagnoses. Source photos must be reviewed and identifying regions irreversibly masked before archive/cloud publication. Raw inbound originals are temporary user-supplied processing material, never cloud content or part of the distributable archive. No original filename is exposed remotely. Record and attachment privacy approval is explicit; changing content invalidates approval. Use diagnosis and review_notes fields rather than guessing clinical information.

The user explicitly permits patient age and sex (nullable age, unknown/male/female sex); no date of birth needed. GitHub is signed in in the user's Chrome; the connector failed, so use normal browser/CLI workflow without blocking local development.


2026-10-25: separate operator and family retrieval entries. Both start with numbered 爺爺 / 嬷嬷 chapters, referencing Pocket Rhino Chapters with neutral grey surfaces. Family retrieval uses confirmed-only data and has no management controls. Medication rows retain source photos with compressed previews and access to full-resolution reviewed originals.

Final retrieval navigation: only visits and medicines; chapter defaults to visits. Overview and appointment management are operator-only. Medicines group by visit date newest first; left category index filters the current patient, with a horizontal mobile equivalent. Optional medicine.category is user-confirmed text, empty values show Uncategorized; no inferred clinical categories.
