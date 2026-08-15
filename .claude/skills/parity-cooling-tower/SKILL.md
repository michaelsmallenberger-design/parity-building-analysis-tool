---
name: parity-cooling-tower
description: Run Parity cooling-tower analysis from an Excel workbook, CSV, Google Sheet, or address list; preserve multi-tab workbook structure and dropdowns; monitor the resumable run; and hand off the grouped human-review page and live Sheet. Use for cooling-tower analysis, Parity building runs, HVAC review packets, regional-tab workbooks, and reviewed HVAC/Fit write-back.
---

# Parity cooling-tower operator runbook

Run one source through the hosted Parity analyzer and hand the operator the live
Google Sheet plus the human-review page. AI output is guidance; only human
HVAC, Optimizer Fit, and Periscope Fit decisions are written into the Sheet.

## Fixed production details

- Base URL: `https://building-analyzer.onrender.com`
- Render service: `srv-d92qip4vikkc73avfb90`
- Authenticated API calls use `X-API-Key: <ANALYZE_API_KEY>`.
- Never print API keys, service-account JSON, or customer cells.
- Check `/health` and `/api/health` before starting.

## Route the input

- `.xls`: stop and ask Sales to use **Save As `.xlsx`**. Do not convert it
  silently because dropdown-preservation cannot be guaranteed.
- `.xlsx`, `.csv`, or a Google Sheet URL: use the asynchronous workbook API
  below. This is required for workbooks with multiple regional tabs.
- A plain list of addresses: `POST /api/run` remains compatible for small,
  stateless runs.
- Legacy `POST /api/run-file` is single-table compatibility only. If it returns
  `409 workbook_endpoint_required`, submit the same file to the workbook API;
  never select the first tab yourself.

## Start a workbook run

For a file:

```bash
curl -sS -X POST "$BASE/api/v2/workbook-runs" \
  -H "X-API-Key: $KEY" \
  -F "file=@<path>"
```

For an existing Google Sheet:

```bash
curl -sS -X POST "$BASE/api/v2/workbook-runs" \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  --data '{"sheet_url":"https://docs.google.com/spreadsheets/d/.../edit"}'
```

The response includes:

- `run_id`, `status`, and versioned `status_url`
- full `tab_inventory` with headers, visibility, mapping, and row counts
- exact `address_count` and unique `analysis_count`
- `sheet_url`, then `review_url` when analysis reaches review
- `setup_url`, `approval_url`, and `retry_url`

Persist the `run_id` and `status_url` immediately.

## Handle preflight states

The workbook engine is fail-closed:

- `preflight` plus `confirmation_required=true`: open `setup_url`. The operator
  must classify every hidden or ambiguous tab in one screen. No building
  analysis has run.
- `approval_required`: show exact per-tab/overall counts and the configured cost
  range, then open `approval_url`. Never auto-approve spend.
- `queued` or `analyzing`: poll `status_url` about every 30 seconds.
- `needs_attention` without a `review_url`: use `retry_url`. Completed row
  checkpoints are skipped and the workbook is not reserved twice.
- `review_open` or `needs_attention` with a `review_url`: hand off the review
  page. Machine-error rows remain visible for rerun or human disposition.
- `complete`: every eligible row has a terminal machine result, all required
  human fields, and no outstanding Sheet write-back error.

A `409` from workbook creation is usually an intentional preflight stop. Read
the returned state and action URL; do not treat it as a partial analysis.

## Workbook guarantees

- Every eligible visible address tab is processed in workbook order.
- Grok only receives tab names, headers, and redacted value-shape counts for
  ambiguous schemas. It never receives raw customer addresses for intake.
- Excel conversion creates one Google Sheet copy in the configured Shared
  Drive. The source workbook is unchanged.
- Existing tab names, visibility, and every Excel dropdown rule are verified
  before analysis. A mismatch trashes the converted copy and stops the run.
- Parity columns are appended only to the right of selected tabs. Existing
  columns, formatting, formulas, and dropdowns are not changed.
- Row identity includes the Sheet, tab/grid ID, and original physical row, so
  equal row numbers on different tabs cannot collide.
- Exact duplicate normalized addresses may reuse one machine result, but each
  source row keeps its own review and write-back target.

## Human review and recovery

Open `review_url` in the authenticated browser. The page is grouped by original
tab and shows per-tab and overall analyzed/reviewed counts.

Each card requires:

- at least one HVAC system or `None`
- Optimizer Fit: `Good`, `Bad`, or `Not Sure`
- Periscope Fit: `Good`, `Bad`, or `Not Sure`

Each Submit saves locally first, then writes to the exact source tab and row.
If Sheets is temporarily unavailable, the decision remains saved and the card
shows a retryable write-back error.

For analyzed rows that failed, use:

```text
GET  /api/batch/<batch_id>/failures
POST /api/batch/<batch_id>/rerun
```

Reruns merge into the same review page. A reviewer may instead human-disposition
an unresolved row by completing all required fields.

## Address-list compatibility

For a plain address list, `POST /api/run` returns audit HTML. Capture
`X-Review-URL` and optional `X-Sheet-URL` from response headers. This path does
not represent a multi-tab workbook.

## Spend and privacy rules

- Do not run paid test addresses unless the operator explicitly requests it.
- More than 250 eligible workbook rows requires one recorded approval and is
  never truncated.
- If measured/configured per-address cost rates are missing, large-workbook
  approval remains blocked. Never substitute a guessed dollar estimate.
- Customer workbooks, manifests, checkpoints, reviews, and SQLite queue state
  live on the Render persistent disk. Private artifacts are served with
  `Cache-Control: private, no-store`.
- Never add AI verdict, confidence, or reasoning columns to the customer Sheet;
  write human review values only.
