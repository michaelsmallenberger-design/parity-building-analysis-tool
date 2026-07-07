---
name: parity-cooling-tower
description: Run the Parity cooling-tower / HVAC analysis on a list of buildings, generate the team review packet, then write the reviewed results into a Google Sheet in the operator's Drive. Use when someone wants to analyze buildings for cooling towers / rooftop HVAC equipment and produce a reviewed, filled-out sheet. Triggers on "run the cooling tower analysis", "analyze these buildings", "parity run", "cooling tower review".
---

# Parity Cooling Tower — operator runbook

You are running this on behalf of a Parity team member (the "operator"). The job:
take a list of buildings, produce a **review packet** the team fills out, then write
the reviewed **HVAC Systems + Fit** results into a **Google Sheet in the operator's
Drive**. The AI is guidance only — the finished sheet holds the HUMAN picks.

There is NO Apps Script, NO service account, NO terminal for reviewers. The analysis
runs on the hosted service; you (Claude) write the sheet directly via the operator's
connected Google Drive.

## Prerequisites (check first)
- **Analyzer API key.** Read it from the `PARITY_ANALYZE_KEY` env var, or the repo's
  `.env` (`ANALYZE_API_KEY=`), or ask the operator for it. Call it `KEY` below.
- **Google Drive connected to Claude** (the `Google_Drive` create_file tool must be
  available). If it isn't, tell the operator to connect Google in their Claude
  settings — that's the only setup they need.
- Base URL: `https://building-analyzer.onrender.com`

## Steps

### 1. Get the buildings
Ask the operator for the input, accept either:
- a **spreadsheet/CSV file** of buildings (any columns/format — their columns are
  preserved), or
- a plain **list of addresses**.
Pick a short `TITLE` for this run (e.g. "Washington Gas MD 2026-07-08").

### 2. Run the analysis + build the review packet
For a **file**:
```
curl -s -X POST "https://building-analyzer.onrender.com/api/run-file" \
  -H "X-API-Key: KEY" -F "file=@<path>" -F "title=TITLE"
```
For a **plain address list**, POST JSON to `/api/run` instead:
`{"title":"TITLE","addresses":["addr1","addr2",...]}` (send the body from a UTF-8
file with `--data @file.json` — an em-dash in the title through the shell breaks it).

Both return JSON: `{ "review_url": "...", "count": N }`. It runs ~30-40s per
building (serial), so a 20-building sheet takes several minutes — tell the operator
to expect that. Extract the `batch_id` from the review_url (the `b-xxxx` at the end).

### 3. Hand off the review link
Give the operator the **review_url** and tell them: open it, and share it with whoever
is reviewing. Each reviewer clicks through the buildings (imagery carousel + AI
guidance), checks the **HVAC systems** they see and picks a **Fit**, and hits Submit
per building. Then **wait** — ask the operator to tell you when the review is done
(or "done with the ones we care about").

### 4. Pull the picks + write the Google Sheet
When the operator says done:
1. Fetch the batch data + human picks:
   ```
   curl -s "https://building-analyzer.onrender.com/api/batch/BATCH_ID" -H "X-API-Key: KEY"
   ```
   Returns `{title, headers, rows, decisions:[{row_id,hvac_systems,fit,note}], count, reviewed}`.
   `headers`/`rows` are the operator's ORIGINAL table (all their columns preserved);
   `HVAC Systems`, `Fit`, `Notes`, `Row_ID` columns are guaranteed present.
2. **Merge** each decision into its row by matching `row_id` to the `Row_ID` column:
   write `hvac_systems`→`HVAC Systems`, `fit`→`Fit`, `note`→`Notes`.
3. Build a **CSV** of headers + merged rows using proper CSV quoting (values like
   "Cooling Tower, Exhaust Fan" contain commas — they MUST stay one cell). Drop the
   hidden `Row_ID` column from the final sheet if the operator prefers it clean.
4. Create the Google Sheet in the operator's Drive:
   - tool: `Google_Drive` **create_file**
   - `title`: TITLE
   - `mimeType`: `application/vnd.google-apps.spreadsheet`
   - `contentMimeType`: `text/csv`
   - `textContent`: the CSV
5. Give the operator the resulting **sheet link** (`viewUrl`).

### 5. Report
Tell the operator: how many buildings, how many reviewed, and the sheet link. Note
that un-reviewed rows have blank HVAC/Fit (they can re-run step 4 after more review).

## Notes & gotchas
- **The review link is per-run and lives on the hosted disk** — if the service
  redeploys mid-review the link can break (the finished sheet, once written, is safe
  in Drive). For a long review, pull step 4 sooner rather than later.
- Use Python's `csv` module for the CSV, never a naive comma-join.
- Never write AI verdict/confidence/reasoning columns into the sheet — human picks only.
- If `create_file` is unavailable, fall back to writing an `.xlsx` locally and telling
  the operator to drop it into Google Drive.
