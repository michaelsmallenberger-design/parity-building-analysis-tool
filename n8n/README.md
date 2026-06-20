# n8n orchestration — "upload a sheet, get an emailed report"

This folder contains the n8n Cloud workflow that drives the cooling-tower pipeline
end to end:

```
Google Sheet → (optional) Gemini address-normalize → per-address POST /api/analyze
            → collect → POST /api/report → email the self-contained HTML report
```

n8n only orchestrates. The ML (YOLO + dual-VLM) runs on the Railway Flask app via the
`/api/*` endpoints in `api_analyze.py` — n8n cannot run PyTorch/YOLO itself (its Python
node is a sandboxed Pyodide runtime).

## One-time setup

### 1. Railway (the analyzer)
1. Deploy the repo to Railway as usual (it already builds from `Dockerfile.railway`).
2. Set a new environment variable on the Railway service:
   - `ANALYZE_API_KEY` = a long random secret (e.g. `openssl rand -hex 24`).
   The `/api/analyze` and `/api/report` routes refuse to run without it (they spend VLM money).
   All the existing keys (`GOOGLE_MAPS_API_KEY`, `MAPBOX_API_KEY`, `GEMINI_API_KEY`,
   `XAI_API_KEY`) stay as they are.
3. Confirm it's live: `curl https://YOUR-APP.up.railway.app/api/health` → `{"status":"ok"}`.

### 2. n8n Cloud
1. **Workflows → Import from File** → choose `parity_cooling_tower.workflow.json`.
2. Add credentials (n8n → Credentials):
   - **Google Sheets** (OAuth2) → attach to the *Read addresses* node.
   - **Gmail** (OAuth2) → attach to the *Send report* node.
3. Replace the placeholders in these nodes:
   - *Read addresses*: set the Google Sheet (`documentId`) and tab (`sheetName`).
   - *Analyze* and *Build report*: set the URL to your Railway host and the
     `X-API-Key` header value to the `ANALYZE_API_KEY` you created above.
   - *Send report*: set `sendTo` to your email.
   > Tip: instead of pasting the key into two nodes, you can create an n8n
   > **Header Auth** credential (`X-API-Key: <key>`) and switch both HTTP nodes to
   > "Generic Credential → Header Auth".

### 3. The sheet
One row per address. Column headers (case-sensitive):
- `Address` — **required**, the full street address.
- `Boro_Area` — optional (borough / city / area).
- `Zip` — optional.

## Running
Open the workflow and click **Execute workflow** (or add a Schedule trigger to run
nightly). Each address is analyzed independently with retry; a single bad address
does not abort the run. When all rows finish, one HTML report is emailed.

## Optional: LLM address normalization
The *Normalize (optional)* node is **disabled** by default, so rows pass straight
through. Enable it (right-click → Enable) when your sheet has messy / inconsistent
address formats — it asks Gemini to rewrite each into a clean
`number street, city, STATE ZIP` line before geocoding. Set the
`x-goog-api-key` header to your `GEMINI_API_KEY`. The *Build address* node already
reads both the normalized output and the raw row, so no other change is needed when
you toggle it.

## Notes / limits
- **Cost:** each address is ~$0.10–$0.30 of VLM spend (Gemini + Grok). The per-row
  loop makes spend proportional to sheet size.
- **Large runs (>~200 addresses):** the emailed HTML embeds every annotated image as
  base64, so very large reports get heavy for email clients. For big batches, split
  the sheet or have the *Build report* step write to Google Drive instead of emailing
  inline. (A chunked/streamed `/api/report` can be added if this becomes routine.)
- **Secrets:** all model/API keys live on the Railway service, not in the workflow
  JSON. n8n holds only the Railway URL + the shared `X-API-Key`.

## Endpoint reference (`api_analyze.py`)
| Method | Path | Auth | Body | Returns |
|--------|------|------|------|---------|
| GET | `/api/health` | none | — | `{"status":"ok"}` |
| POST | `/api/analyze` | `X-API-Key` | `{address, boro_area?, zip?}` | one web_entry JSON (images as `data:` URIs) |
| POST | `/api/report` | `X-API-Key` | `{results:[web_entry,...], title?}` | `text/html` audit report |
