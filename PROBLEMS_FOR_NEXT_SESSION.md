# Problems for next session — streamline the parity run

_Written 2026-07-09, after the live team demo. Alex's feedback: the process isn't
streamlined enough. This is the talk-through list for next time._

> Historical snapshot: the current workbook path is now the versioned,
> resumable multi-tab engine documented in `README.md` and
> `docs/SALES_SHEET_RUNBOOK.md`. References below to dropping n8n, ephemeral
> storage, or a single-sheet flow describe the July 9 state and are not current
> operating instructions.

---

## STATUS UPDATE — built later the same day (2026-07-09)

Decisions made with Alex: **n8n dropped** (it was never actually running — the
"leaning n8n" option assumed wired infrastructure that didn't exist). The Flask app
now owns the sheet via a Google **service account** (`sheets_writer.py`):

- **DONE** — sheet created up front at run time (`_finalize_batch`), real `sheet_url`
  returned, all original columns + HVAC/Fit dropdowns + hidden Row_ID.
- **DONE** — each review Submit fan-outs: local record (authoritative for
  `GET /api/batch`) + live write into the sheet row. Sheets outage never fails a Submit.
- **DONE** — failed-building cleanup: `GET /api/batch/<id>/failures` +
  `POST /api/batch/<id>/rerun` (in-place re-run with optional corrected addresses),
  driven by the new `parity-cleanup-failed` skill. This is the chosen answer to the
  demo's whole-batch imagery failure and the Arlington VA miss (auto-fix-and-rerun
  instead of an in-pipeline retry).
- Everything is env-gated: without `GOOGLE_SERVICE_ACCOUNT_JSON` the app behaves
  exactly as before (operator/skill fallback).

**Remaining to go live:** (1) Alex creates the service account + key
(`RENDER_DEPLOYMENT.md` → "Google Sheets service account", ~15 min); (2) set
`GOOGLE_SERVICE_ACCOUNT_JSON` + `SHEET_SHARE_WITH` on Render; (3) deploy (autoDeploy
webhook still not wired — POST /v1/services/{id}/deploys); (4) paid E2E verify.

**Deliberately not done:** in-pipeline per-tile imagery retry and /api/run vs
/api/run-file intake consolidation (superseded by the cleanup skill for now); the
verify pass over the filled sheet (next phase, unchanged).

The original write-up follows for context.

---

## The goal for next session

One clean pipeline, no human in the middle:

> **Sheet upload → VLM → human review (the blessed review-page output) →
> from that review, immediately update the sheet.**

The blessed review page IS the human interface (the locked `review_render.py` format).
Each human pick on that page writes straight back into the sheet **immediately** — the
sheet updates as reviewers work, driven by the blessed output itself.

Specifically, kill these two manual steps that exist today:
- Alex having to come back and say **"I'm done"**.
- Claude then **pulling the picks and generating the sheet** by hand.

When review is done, the sheet is already done. (Later: a verify pass over that sheet.)

## What I found in the code (the gap is small and concrete)

There are **two review pages**, and the wrong one is being served:

1. `review_render.py` (the *blessed*, locked format) was built so **each Submit POSTs to
   an n8n webhook that writes the pick back into the sheet** — this is the auto-flow we want.
2. The page actually served at `/review/<batch_id>` is the *other* path: Submit →
   `/api/review` → recorded server-side only, **no sheet write**. Then a human (Claude,
   the "operator") pulls picks via `GET /api/batch` and builds the sheet. That's the seam.

`_finalize_batch` in `api_analyze.py` even says it: _"No Google Sheet is created here — the
operator writes the finished sheet from the human picks."_

## The three changes that close it

1. **Create the Google Sheet up front** at run time (rows + dropdowns), return a real
   `sheet_url` instead of the current empty string.
2. **Serve the blessed review page wired to the n8n webhook** (not the record-only page).
3. **Each Submit → n8n → writes HVAC/Fit into that row live.**
4. (Next phase) a **verify pass** over the filled sheet.

## The one decision to make first (talk through this)

**Who owns and auto-writes the sheet?** This gates everything and touches the Google/Render
setup, so it's Alex's call:

- **A. Route through n8n (leaning this way).** Submit → n8n webhook → n8n's Google Sheets
  node writes the row. Uses the stack already wired; the review page was built for it. Sheet
  lives in the Google account n8n is authed to, then shared. No new server credentials.
- **B. Flask writes Sheets directly.** Add a Google service account / OAuth token on Render;
  `/api/review` writes each pick via the Sheets API. More self-contained, but adds a
  server-side Google credential + sharing setup — the thing the runbook deliberately avoided.
- **C. Keep Claude as writer, automate the trigger.** Server pings Claude at 100% reviewed,
  Claude builds/writes the sheet. Removes the "come tell you" step but keeps Claude in the
  loop and the fragile base64 Drive-upload.

## Other friction from the demo (fix alongside)

- **Whole-batch imagery failure.** `/api/run-file` returned ZERO images on all 4 buildings
  (transient); had to re-run the whole batch (~2–3 min) live via `/api/run`. Need per-building
  imagery retry + fail-loud so one blip doesn't force a full re-run, and a batch never saves
  all-empty silently.
- **Endpoint inconsistency.** `/api/run-file` tokenized the `Property Address` header into two
  columns (`Property` + `Address`); `/api/run` kept it clean. Consolidate on one path.
- **Stale local `.env` key → 401** on first call (Render is source of truth). Updated it this
  session, but the flow should always pull fresh from Render or stay synced.
- **Drive upload of the finished sheet corrupts on inline base64** — a big reason the sheet
  doesn't "just show up." The server-written-sheet approach above removes this entirely.
- **Arlington VA address** produced no imagery (geocode/tile miss); reviewer hand-noted
  "No Images". Ambiguous VA-vs-DC address was flagged pre-run but ran anyway.
- **Bash 2-min default timeout** killed a 4-building `/api/run` once (serial ≈ 120–160s).

## Where things stand right now

- Live on Render: `building-analyzer.onrender.com` (srv-d92qip4vikkc73avfb90).
- Local `.env` `ANALYZE_API_KEY` refreshed to the live Render value this session.
- Demo run artifacts: last good review batch `b-fd4a61e5aa`; finished file at
  `Downloads/Team Demo 2026-07-09.xlsx` (3 NYC buildings with imagery, Arlington without).
- Nothing new committed. Branch: `piece4-concurrency`.
