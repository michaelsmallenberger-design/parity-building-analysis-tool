# n8n Orchestration

This folder has two ways to run the cooling-tower analyzer from n8n.

## Recommended: Excel Upload

Import:

```text
n8n/parity_excel_upload.workflow.json
```

Flow:

```text
Form upload -> POST /api/run-file on Render -> Gmail sends HTML report -> optional Slack confirmation
```

Use this when someone wants to upload an `.xlsx`, `.xls`, or `.csv` file and get an emailed report without maintaining a Google Sheet.

First test file:

```text
n8n/address_upload_template.xlsx
```

Setup notes:

- Render URL in the HTTP node: `https://YOUR-SERVICE.onrender.com/api/run-file`
- Header in the HTTP node: `X-API-Key: <ANALYZE_API_KEY from Render>`
- Gmail credentials are required.
- Slack is optional and disabled by default in the import file.

Detailed steps are in `n8n/EXCEL_UPLOAD_WORKFLOW.md` and `RUNBOOK.md`.

## Optional: Google Sheet + Grok Cleanup

Import:

```text
n8n/parity_cooling_tower.workflow.json
```

Flow:

```text
Webhook -> Google Sheet -> Grok normalize -> aggregate -> POST /api/run on Render -> Gmail -> Slack
```

Use this when the source list is messy and you want Grok in n8n to normalize each row before the analyzer sees it.

Setup notes:

- Render URL in the HTTP node: `https://YOUR-SERVICE.onrender.com/api/run`
- Header in the HTTP node: `X-API-Key: <ANALYZE_API_KEY from Render>`
- Google Sheets, xAI/Grok, Gmail, and Slack credentials are needed.

## What n8n Does

n8n only orchestrates file intake, optional cleanup, and notifications. YOLO, imagery, geocoding, Gemini, and Grok verification all run on the Render Flask service through `api_analyze.py`.

## Endpoints

| Method | Path | Auth | Body | Returns |
| --- | --- | --- | --- | --- |
| `GET` | `/api/health` | none | none | health JSON |
| `POST` | `/api/run-file` | `X-API-Key` | multipart file upload | `text/html` report |
| `POST` | `/api/run` | `X-API-Key` | `{addresses:[...]}` | `text/html` report |
| `POST` | `/api/analyze` | `X-API-Key` | one address | one result JSON |
| `POST` | `/api/report` | `X-API-Key` | result entries | `text/html` report |
