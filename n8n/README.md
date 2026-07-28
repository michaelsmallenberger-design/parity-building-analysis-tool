# n8n Orchestration

n8n is one way to drive the cooling-tower analyzer. The primary operator path is the
Claude skill (`.claude/skills/parity-cooling-tower`), which runs a batch, hands the team a
review page, and writes the reviewed picks into a Google Sheet. Use n8n when you want a
hands-off, scheduled/triggered run that emails a finished report instead.

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

An `approval_required` or `confirmation_required` result is a deliberate stop,
not a failed analysis. Use the returned `approval_url` or `setup_url`, then keep
polling the same `status_url`. Do not make n8n auto-approve spend.

## Google Sheet + Grok Cleanup

Import:

```text
n8n/parity_cooling_tower.workflow.json
```

Flow:

```text
Webhook -> Google Sheet -> Grok normalize -> aggregate -> POST /api/run on Render -> Gmail -> Slack
```

Use this when the source list is messy and you want Grok in n8n to normalize each row before
the analyzer sees it. `POST /api/run` returns a finished `text/html` audit report that the Gmail
node sends.

Setup notes:

- Render URL in the HTTP node: `https://YOUR-SERVICE.onrender.com/api/run`
- Header in the HTTP node: `X-API-Key: <ANALYZE_API_KEY from Render>`
- Google Sheets, xAI/Grok, Gmail, and Slack credentials are needed.

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
