# n8n Orchestration

> Inactive reference only. Parity does not currently deploy or require n8n.
> Browser upload, Drive inbox, and the versioned API call the durable workbook
> engine directly.

The exports below are retained only in case an external orchestrator is added
later. The primary operator path is the Claude skill
(`.claude/skills/parity-cooling-tower`), which runs a batch, hands the team a
review page, and writes reviewed picks into a Google Sheet.

## Multi-Tab Workbook Flow

Import:

```text
n8n/parity_workbook_async.workflow.json
```

This is the replacement file-upload path for `.xlsx` workbooks with several
regional tabs. It creates one asynchronous workbook run, then polls the status
URL until the grouped review page is ready. It never normalizes customer
addresses with Grok in n8n; Render uses deterministic headers and invokes Grok
only for redacted, ambiguous schema profiles.

Create one n8n **Header Auth** credential named `Parity Render API Key` with
header name `X-API-Key` and the Render `ANALYZE_API_KEY` as its value. Select
that credential on both HTTP Request nodes. Keep the API key in n8n's encrypted
credential store, not in the workflow JSON.

An `approval_required` or `confirmation_required` result is a deliberate stop,
not a failed analysis. Use the returned `approval_url` or `setup_url`, then keep
polling the same `status_url`.

## Retired Legacy Single-Table Workflow

The older file remains in the repository only for existing single-table
consumers:

```text
n8n/parity_cooling_tower.workflow.json
```

Do not use it for new workbook automation. It sends rows through a legacy
single-table path and does not provide multi-tab accounting, resumable chunks,
or dropdown-preservation checks. New automation must use
`parity_workbook_async.workflow.json`. Grok schema assistance happens inside
Render with redacted column profiles; n8n must not send raw customer addresses
to an LLM.

## What n8n Does

n8n only orchestrates intake, optional cleanup, and notifications. YOLO, imagery, geocoding,
Gemini, and Grok verification all run on the Render Flask service through `api_analyze.py`.

## Endpoints

| Method | Path | Auth | Body | Returns |
| --- | --- | --- | --- | --- |
| `GET` | `/api/health` | none | none | health JSON |
| `POST` | `/api/run` | `X-API-Key` | `{addresses:[...]}` | `text/html` report (+ `X-Review-URL` header) |
| `POST` | `/api/run-file` | `X-API-Key` | multipart file upload | JSON `{review_url, count}` (drives the review flow; used by the Claude skill) |
| `POST` | `/api/v2/workbook-runs` | `X-API-Key` | `.xlsx`/`.csv` file or `sheet_url` | asynchronous run metadata |
| `GET` | `/api/v2/workbook-runs/<run_id>` | `X-API-Key` | none | durable run status and denominators |
| `POST` | `/api/v2/workbook-runs/<run_id>/approval` | `X-API-Key` | none | records one large-workbook approval |
| `POST` | `/api/v2/workbook-runs/<run_id>/retry` | `X-API-Key` | none | resumes a failed run from checkpoints |
| `POST` | `/api/analyze` | `X-API-Key` | one address | one result JSON |
| `POST` | `/api/report` | `X-API-Key` | result entries | `text/html` report |
| `GET` | `/review/<batch_id>` | site password | none | interactive review page |
| `GET` | `/api/batch/<batch_id>` | `X-API-Key` | none | original table + human review decisions |
