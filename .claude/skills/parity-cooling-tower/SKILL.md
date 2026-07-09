---
name: parity-cooling-tower
description: Run the Parity cooling-tower / HVAC analysis on a list of buildings, generate the team review packet, then write the reviewed results into a Google Sheet (with HVAC/Fit dropdowns) in the operator's Drive. Use when someone wants to analyze buildings for cooling towers / rooftop HVAC equipment and produce a reviewed, filled-out sheet. Triggers on "run the cooling tower analysis", "analyze these buildings", "parity run", "cooling tower review".
---

# Parity Cooling Tower — operator runbook

You are running this on behalf of a Parity team member (the "operator"). The job:
take a list of buildings, produce a **review packet** the team fills out, then write
the reviewed **HVAC Systems + Fit** results into a **Google Sheet (with dropdowns) in
the operator's Drive**. The AI is guidance only — the finished sheet holds the HUMAN picks.

**Primary path (server has a Sheets service account):** the run itself creates the
Google Sheet up front and returns its `sheet_url`; every reviewer Submit writes that
row into the sheet LIVE. When review is done, the sheet is already done — you hand
over two links (review page + sheet) at run time and there is nothing to pull or
build afterwards. Steps 4–5 below are the FALLBACK for when `sheet_url` comes back
empty (credential not configured on Render, or sheet creation failed — check Render
logs for "Sheet creation failed").

There is NO Apps Script and NO terminal for reviewers. In the fallback, you (Claude)
write the sheet via the operator's connected Google Drive.

## Prerequisites (check first)
- **Analyzer API key (`KEY`).** Try the repo's `.env` (`ANALYZE_API_KEY=`) or the
  `PARITY_ANALYZE_KEY` env var first. **GOTCHA — the local `.env` key is often stale.**
  If any authenticated call returns `401 {"error":"unauthorized"}`, the live key is the
  one configured on Render. Pull it (do not print the value):
  ```bash
  RKEY=$(grep -iE '^RENDER_API_KEY=' .env | head -1 | cut -d= -f2- | tr -d '\r"'"'"' ')
  KEY=$(curl -s "https://api.render.com/v1/services/srv-d92qip4vikkc73avfb90/env-vars?limit=50" \
    -H "Authorization: Bearer $RKEY" \
    | python -c "import sys,json;d=json.load(sys.stdin);print(next(e['envVar']['value'] for e in d if e['envVar']['key']=='ANALYZE_API_KEY'))")
  ```
- **Google Drive connected to Claude** (the `Google_Drive` create_file tool must be
  available). If it isn't, tell the operator to connect Google in their Claude settings.
- Base URL: `https://building-analyzer.onrender.com`. Service id: `srv-d92qip4vikkc73avfb90`.
- Sanity check before running: `curl -s .../health` and `.../api/health` should be healthy/ok.

## Steps

### 1. Get the buildings
Accept either a **spreadsheet/CSV file** (any columns — they're preserved) or a plain
**list of addresses**. Pick a short `TITLE` (e.g. "Washington Gas MD 2026-07-08").

### 2. Run the analysis + build the review packet
It runs ~30-40s per building (serial), so a 20-building sheet takes several minutes —
tell the operator to expect that. **The two endpoints return DIFFERENT shapes:**

- **File** → `POST /api/run-file` returns **JSON** `{ok, count, review_url, sheet_url}`
  (`sheet_url` is the LIVE Google Sheet when the server has the Sheets credential;
  empty string means fallback mode):
  ```bash
  curl -s -X POST ".../api/run-file" -H "X-API-Key: $KEY" -F "file=@<path>" -F "title=TITLE"
  ```
- **Address list** → `POST /api/run` returns the **audit HTML body**, and the
  **`review_url` is in the `X-Review-URL` response header** (NOT in the body; the
  sheet link, when live, is in `X-Sheet-URL`). Capture
  headers with `-D`. Send the JSON body from a UTF-8 file (an em-dash in the title
  through the shell breaks it):
  ```bash
  printf '%s' '{"title":"TITLE","addresses":["addr1","addr2"]}' > body.json
  curl -s -X POST ".../api/run" -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
    --data @body.json -D headers.txt -o report.html
  grep -i x-review-url headers.txt   # -> https://.../review/b-xxxxxxxx
  ```

Extract the `batch_id` (the `b-xxxxxxxx` at the end of the review_url) either way.

### 3. Hand off the links
Give the operator the **review_url** (and, when present, the **sheet_url**) and open
the review page for them. Each reviewer clicks through the buildings (imagery
carousel + AI guidance), checks the **HVAC systems** they see, picks a **Fit**, and
hits **Submit per building** (Submit posts to `/api/review` → recorded server-side
AND written into the live sheet row when the batch has one).

**If `sheet_url` was returned, you are DONE after this step** — the sheet fills
itself as the team reviews; there is no "tell me when finished" and no step 4/5.
If any buildings failed analysis (error rows, "No Images"), run the
**`parity-cleanup-failed`** skill on the batch — it re-runs the failures in place and
the same review page + sheet pick up the fresh results.

### 4. FALLBACK ONLY — pull the picks + write the Google Sheet (WITH DROPDOWNS)
Only when `sheet_url` came back empty. Ask the operator to tell you when review is
done, then:

1. Fetch the batch + human picks:
   ```bash
   curl -s ".../api/batch/BATCH_ID" -H "X-API-Key: $KEY"
   ```
   Returns `{title, headers, rows, decisions:[{row_id,hvac_systems,fit,note}], count, reviewed}`.
   `headers`/`rows` are the operator's ORIGINAL table; `HVAC Systems`, `Fit`, `Notes`,
   `Row_ID` are guaranteed present.
2. **Merge** each decision into its row by matching `row_id` → `Row_ID`:
   `hvac_systems`→`HVAC Systems`, `fit`→`Fit`, `note`→`Notes`. Drop the hidden `Row_ID`
   column from the final sheet.
3. **Build an `.xlsx` WITH dropdowns** (a plain CSV import gives NO dropdowns — the
   dropdowns are the point). Use `openpyxl`; taxonomy comes from `review_render.py`
   (`HVAC_SYSTEMS` = Cooling Tower, Chiller, Exhaust Fan, RTU, AHU, PTAC, Fan Coil,
   Heat Pump, VRF; `FIT_OPTIONS` = Optimizer, Periscope, Unclear, Bad):
   ```python
   import openpyxl, base64
   from openpyxl.worksheet.datavalidation import DataValidation
   wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Review"
   ws.append(headers)                       # headers WITHOUT Row_ID
   for r in merged_rows: ws.append(r)
   HVAC='"Cooling Tower,Chiller,Exhaust Fan,RTU,AHU,PTAC,Fan Coil,Heat Pump,VRF"'
   FIT='"Optimizer,Periscope,Unclear,Bad"'
   hidx = headers.index("HVAC Systems"); fidx = headers.index("Fit")  # 0-based
   col = lambda i: openpyxl.utils.get_column_letter(i+1)
   hv = DataValidation(type="list", formula1=HVAC, allow_blank=True); hv.showErrorMessage=False  # multi-value: warn only
   ft = DataValidation(type="list", formula1=FIT,  allow_blank=True); ft.showErrorMessage=True
   ws.add_data_validation(hv); hv.add(f"{col(hidx)}2:{col(hidx)}500")
   ws.add_data_validation(ft); ft.add(f"{col(fidx)}2:{col(fidx)}500")
   wb.save("out.xlsx")
   ```
4. **Upload with conversion** so the xlsx list-validations become native Sheets dropdowns:
   - tool: `Google_Drive` **create_file**
   - `title`: TITLE
   - `contentMimeType`: `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`
   - `base64Content`: base64 of `out.xlsx`
   - do NOT set `disableConversionToGoogleType` (conversion → native Google Sheet).
   **GOTCHAS:** (a) a hand-rolled minimal xlsx makes the converter fail with
   `invalid argument` — use a full `openpyxl` workbook (you may strip only
   `xl/theme/theme1.xml` to shrink it). (b) The base64 string is easy to corrupt when
   pasting into the tool arg; keep the file small and let the tool reject bad base64
   rather than guessing. (c) If conversion still 400s, upload with
   `disableConversionToGoogleType: true` (stored as .xlsx; dropdowns still work when
   opened in Sheets) or write the .xlsx locally.
5. Give the operator the resulting **sheet link** (`viewUrl`).

### 5. FALLBACK ONLY — report
Tell the operator: how many buildings, how many reviewed, and the sheet link. Un-reviewed
rows have blank HVAC/Fit (re-run step 4 after more review). To show a reviewer's picks back,
`GET /api/batch/BATCH_ID` returns the `decisions` list (this works in the live-sheet
path too — the local record is kept as the authoritative backup).

## Notes & gotchas
- **Live-sheet path:** the sheet is owned by the server's service account and shared
  per the `SHEET_SHARE_WITH` env var on Render (comma-separated emails, writer
  access). If the operator can't open it, add their email there and redeploy — or
  fall back to step 4.
- **The connected Drive may not be the operator's.** (Fallback path.) The sheet lands in whatever Google
  account is connected to Claude (often an owner/admin, e.g. Alex), and there is **no
  share/permission-write tool** — you cannot grant another person access. If the operator
  can't open it, fall back to a **local `.xlsx`** (same dropdown recipe) in their Downloads
  that they open in Excel or drag into their own Drive.
- **The review link is per-run and on the hosted (ephemeral) disk** — a mid-review
  redeploy can break it (the finished sheet, once written to Drive, is safe). Pull step 4
  sooner rather than later for long reviews.
- Each run spends Gemini + Grok money per non-registry building; NYC buildings already in
  the DOHMH cooling-tower registry short-circuit to `registry_confirmed` (no VLM spend).
- Never write AI verdict/confidence/reasoning columns into the sheet — **human picks only.**
- Use Python's `csv`/`openpyxl` for output, never a naive comma-join (HVAC values like
  "Cooling Tower, Exhaust Fan" contain commas and must stay one cell).
