# Current design — 2026-10-25

This supersedes the initial visual direction below. User reference: Pocket Rhino Chapters. Neutral grey #ececeb, charcoal #24262a, blue #315ea8 and muted purple #74568d. Numbered chapters 01 / 爺爺 and 02 / 嬷嬷, thin rules, restrained corners, left chapter index and horizontal content tabs. No green background and no combined-patient entry. Chapter switching clears details, editor and filters. Mobile returns to chapter home to choose another person.

Two entries: operator (`?view=manage`, default localhost) handles inputs, drafts, review and backups; family retrieval (`?view=family`) has only confirmed visits, medicines, source photos and appointments. Remote hosting always selects family retrieval. Local family preview uses a positive-allowlist endpoint without draft data. Cloud authorization remains the enforcement boundary; the localhost preview is not a second user account.

Medication rows have clearly linked photos; visit-level fallback is explicitly labelled. Reviewed-only JPEG previews have max edge 960px and quality 86. Opening a photo retains access to the full-resolution reviewed original. Unreviewed pictures never render automatically. No synthetic medication imagery.

---

# 安康簿 design

Mode: Operate for local management; Read for family viewer. Standalone project, not an extension of the unrelated Hi & Seek app in the parent folder. The user authorized direct implementation after the product discussion. Choices are resolved from that briefing without another approval round.

Visual direction: a family care ledger. Compared grounded structures: chronological notebook, pharmacy label index, appointment diary, library catalogue, calendar agenda, family noticeboard, and care ledger. The ledger direction puts an upcoming visit strip ahead of a clearly ruled visit table, with a patient index alongside it. No fabricated health scores or analytics. The design seed ran without remote challengers; no generated board is used.

Palette: off-white #f6f7f3, paper #ffffff, ink #243c39, muted #596c66, teal #24685f, pale mint #e7f1eb, warm sand #f5eddd, warning #885c18. Teal for actions, amber for pending review. Soft illustration-free patient monograms and one thin-line SVG icon set. Medical text uses locally installed Chinese system faces for clarity and offline operation. Titles 28–34px, body 16px, labels >=13px. Touch controls >=44px. Tables use tabular figures.

Desktop: slim left navigation with product identity, patient switch and management links; content has a readable header, appointment strip and full-width ledger. Mobile: horizontal profile filter, navigation across bottom, visit list becomes structured rows, sticky search/filter; no clipped tables. Detail/editor takes full content pane and keeps original source review reachable. Redaction needs a focused dialog with zoomed image and obvious irreversible masking action. No automatic animation beyond focus/hover and a short subtle panel entrance; respect reduced motion.

Real archive starts empty with concrete instructions. Explicit sample mode is transient and never saved, published or backed up. Cloud missing-config, signed-out, denied-membership, expired-session and empty-published-snapshot states are distinct. UI copy uses Traditional Chinese by default, Simplified switch maps labels/search only; original medical wording is never translated automatically.

Final retrieval navigation: only visits and medicines; chapter defaults to visits. Overview and appointment management are operator-only. Medicines group by visit date newest first; left category index filters the current patient, with a horizontal mobile equivalent. Optional medicine.category is user-confirmed text, empty values show Uncategorized; no inferred clinical categories.
