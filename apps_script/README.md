# Sheet writer (Google Apps Script) — setup

This is the *only* manual setup for the review→sheet loop. No service-account key,
no n8n. One team member deploys it once; it runs as them under your Workspace.

## 1. Create the script (~2 min)

1. Go to **script.google.com → New project** (or in a sheet: **Extensions → Apps Script**).
2. Delete the default `Code.gs` contents and paste **`sheet_writer.gs`**.
3. **Project Settings (gear) → Script Properties → Add script property**, add:

   | Property | Value |
   |---|---|
   | `SHARED_TOKEN` | a long random string (you'll give the same value to Render) |
   | `TEAM_EMAILS` | comma-separated emails to auto-share each new sheet with (e.g. `mike@…,joe@…,alex@…`) |
   | `FOLDER_ID` | *(optional)* a Drive folder ID to drop new sheets into — the part of the folder URL after `/folders/` |

## 2. Deploy as a web app (~1 min)

1. **Deploy → New deployment → (gear) Web app**.
2. **Execute as: Me**, **Who has access: Anyone with the link**.
3. **Deploy** → authorize when prompted (it's your own script).
4. Copy the **Web app URL** (ends in `/exec`).

## 3. Give Render two env vars

On the Render service (`building-analyzer`), set:

| Env var | Value |
|---|---|
| `SHEET_WEBHOOK_URL` | the `/exec` URL from step 2 |
| `SHEET_WEBHOOK_TOKEN` | the same `SHARED_TOKEN` from step 1 |

(Also confirm `APP_URL=https://building-analyzer.onrender.com` is set, so the review links are absolute.)

That's it. From then on:

- Every analysis run **creates a new team sheet** (shared with `TEAM_EMAILS`) and returns
  its URL in the `X-Sheet-URL` header + a review link in `X-Review-URL`.
- Reviewers open the review link, check HVAC systems + Fit per building, hit Submit →
  it writes straight into that sheet's row. No key, no n8n.

## Contract (for reference)

`POST` JSON to the web app URL:

```jsonc
// create — Render sends the user's WHOLE uploaded table (all their columns
// preserved, NO AI columns). The tool guarantees HVAC Systems / Fit / Notes /
// Row_ID are present in headers; the script adds dropdowns to HVAC Systems + Fit
// and hides Row_ID.
{ "action":"create", "token":"…", "title":"Washington Gas 2026-07-07",
  "headers":["Property Address","Property Name","HVAC Systems","Fit","Notes","City","Row_ID"],
  "rows":[ ["7333 New Hampshire Ave","Takoma Overlook","","","","Takoma Park","1"], … ] }
// → { "ok":true, "sheet_url":"https://docs.google.com/…", "sheet_id":"…" }

// update — on each reviewer Submit (matched by the hidden Row_ID column)
{ "action":"update", "token":"…", "sheet_url":"https://docs.google.com/…",
  "row_id":"1", "hvac_systems":"Cooling Tower, Exhaust Fan", "fit":"Optimizer", "note":"" }
// → { "ok":true, "row":2 }
```

## Updating the script later

Edit `sheet_writer.gs`, then **Deploy → Manage deployments → (edit) → Version: New version → Deploy**.
The `/exec` URL stays the same, so nothing on Render needs to change.
