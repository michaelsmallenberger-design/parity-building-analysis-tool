# Parity Building Analyzer Runbook

This is the shortest path to run the Excel upload workflow.

## 1. Render

Create the Render service from `render.yaml`, then add these environment values:

| Key | Where it comes from |
| --- | --- |
| `GOOGLE_MAPS_API_KEY` | Google Maps / Static Maps |
| `MAPBOX_API_KEY` | Mapbox fallback imagery/geocoding |
| `GEMINI_API_KEY` | Google AI Studio |
| `XAI_API_KEY` | xAI / Grok |
| `ANALYZE_API_KEY` | Make this a long random password and reuse it in n8n |

Render should show `/api/health` as healthy after deploy.

## 2. n8n

Import:

```text
n8n/parity_excel_upload.workflow.json
```

Then edit these placeholders:

| Node | What to set |
| --- | --- |
| `Analyze on Render` | Replace the URL with `https://YOUR-SERVICE.onrender.com/api/run-file` |
| `Analyze on Render` | Replace `REPLACE_WITH_ANALYZE_API_KEY` with the Render `ANALYZE_API_KEY` |
| `Email report` | Connect Gmail credentials |
| `Slack confirmation` | Optional. Enable it only after setting a Slack channel and credentials |

Activate the workflow. The n8n form URL becomes the upload page.

## 3. First Test

Use:

```text
n8n/address_upload_template.xlsx
```

Submit the file through the n8n form with your email address. The workflow responds immediately, Render runs the analysis, and Gmail sends the finished HTML report.

## 4. Spreadsheet Rules

Required address column:

```text
Address
```

Also accepted:

```text
Property Address
Street Address
Building Address
```

Optional:

```text
City
Boro_Area
Zip
Zip Code
Postal Code
```

## 5. If Something Fails

- `401 unauthorized`: the n8n `X-API-Key` does not match Render's `ANALYZE_API_KEY`.
- `503 ANALYZE_API_KEY not configured`: Render is missing `ANALYZE_API_KEY`.
- `missing uploaded file field named 'file'`: the n8n HTTP node is not sending the binary field as `file`.
- no email: check the Gmail node first, then the `Analyze on Render` node output.
- huge run: split the spreadsheet. The default safety limit is `250` rows via `ANALYZE_FILE_MAX_ROWS`.
