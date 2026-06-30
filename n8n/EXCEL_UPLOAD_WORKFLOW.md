# n8n Excel Upload Workflow

Use this workflow when someone wants to upload an Excel/CSV file directly instead of keeping a Google Sheet.

Import-ready workflow:

```text
n8n/parity_excel_upload.workflow.json
```

Known-good starter spreadsheet:

```text
n8n/address_upload_template.xlsx
```

## Flow

Recommended simple flow:

```text
n8n Form Trigger with file upload
  -> HTTP Request to Render /api/run-file
  -> Gmail sends returned HTML report
  -> optional Slack confirmation
```

The analyzer backend parses the file. n8n does not need to understand the spreadsheet columns beyond passing the file through.

Optional Grok-normalized flow for messy spreadsheets:

```text
n8n Form Trigger with file upload
  -> Extract From File / spreadsheet parser
  -> Grok normalizes each row into one clean address line
  -> Aggregate normalized addresses into one list
  -> HTTP Request to Render /api/run
  -> Gmail sends returned HTML report
  -> optional Slack confirmation
```

Use the Grok path when the uploaded Excel files have inconsistent columns, building names mixed into address fields, missing city/state formatting, or other sales-list noise. Use the simple `/api/run-file` path when the file has a clear address column.

## Required Backend Endpoint

Render must be running a version of the app that includes:

```text
POST /api/run-file
```

Request:

- Header: `X-API-Key: <ANALYZE_API_KEY>`
- Body type: multipart form data
- Form field `file`: uploaded `.xlsx`, `.xls`, or `.csv`
- Form field `title`: optional report title

Response:

- `text/html` audit-card report

## Spreadsheet Format

Required address column. Supported names include:

- `Address`
- `Property Address`
- `Street Address`
- `Building Address`

Optional columns:

- `Boro_Area`, `Borough`, or `City`
- `Zip`, `Zip Code`, or `Postal Code`

## n8n Nodes

### Simple file-pass workflow

The included `parity_excel_upload.workflow.json` implements this path.

### 1. Form Trigger

Create a form with:

- File field named/labelled `file`
- Optional text field named/labelled `title`

Use this form URL as the place where users submit Excel files.

### 2. HTTP Request

Configure:

- Method: `POST`
- URL: `https://YOUR-SERVICE.onrender.com/api/run-file`
- Header:
  - `X-API-Key: <same ANALYZE_API_KEY from Render>`
- Body Content Type: multipart form data
- Add binary file field:
  - Field name: `file`
  - Binary property: the binary property emitted by the Form Trigger for the file upload
- Add text field:
  - Field name: `title`
  - Value: report title, or the title field from the form
- Response format: text
- Timeout: long enough for the batch, for example `1800000` ms

In the imported workflow, replace:

- `https://REPLACE-WITH-RENDER-SERVICE.onrender.com/api/run-file`
- `REPLACE_WITH_ANALYZE_API_KEY`

### 3. Gmail

Configure:

- Email type: HTML
- Message/body: the HTML text returned by the HTTP Request node
- Recipient: whoever should receive the report

### 4. Slack Optional

Post a short completion message after Gmail succeeds.

## Optional Grok-Normalized Workflow

Use this if you want n8n's Grok API step to clean each uploaded spreadsheet row before the analyzer sees it.

### 1. Form Trigger

Same as above: file field named `file`, optional `title`.

### 2. Extract From File / Spreadsheet Parser

Parse the uploaded `.xlsx` or `.csv` into one n8n item per row.

The exact n8n node name can vary by version:

- **Extract From File**
- **Spreadsheet File**
- **Read/Write Files from Disk** plus spreadsheet parse, on self-hosted n8n

The goal is one item per spreadsheet row, preserving columns like `Address`, `Property Address`, `City`, `Zip`, `Boro_Area`, and any building-name fields.

### 3. Grok Normalize

Use your existing xAI/Grok credential in a Basic LLM Chain or AI Agent node.

Prompt:

```text
You normalize US building addresses for geocoding.

Return ONLY valid JSON with this shape:
{"address":"number street, city, STATE ZIP"}

Rules:
- Use the row data only.
- Keep building number, street, city/borough, state, and ZIP when present.
- If city/state/ZIP are separate columns, merge them into the address.
- Remove owner/company names unless they are part of the building name needed for geocoding.
- Do not invent missing street numbers.
- If there is no usable address, return {"address":""}.

Input row:
{{ JSON.stringify($json) }}
```

### 4. Parse Grok Output

Add a small Set/Code/JSON Parse step so each item has:

```json
{"address": "140 West End Ave, New York, NY 10023"}
```

Filter out blank addresses.

### 5. Aggregate Addresses

Aggregate all `address` values into:

```json
{"addresses": ["140 West End Ave, New York, NY 10023", "..."]}
```

### 6. HTTP Request To Render

Call the existing JSON endpoint, not the file endpoint:

- Method: `POST`
- URL: `https://YOUR-SERVICE.onrender.com/api/run`
- Header:
  - `X-API-Key: <ANALYZE_API_KEY>`
- JSON body:

```js
={{ { addresses: $json.addresses, title: $json.title || 'Cooling Tower Analysis' } }}
```

- Response format: text
- Timeout: `1800000` ms

### 7. Gmail / Slack

Same as the simple workflow.

## Smoke Test

Use a two-row `.xlsx` first:

| Address | City | Zip |
|---|---|---|
| 140 West End Ave | New York | 10023 |
| 22 North 6th St | Brooklyn | 11249 |

After the first successful run, increase batch size gradually.
