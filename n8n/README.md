# n8n orchestration — "upload a sheet, get an emailed report"

This folder contains the n8n Cloud workflow that drives the cooling-tower pipeline
end to end:

```
Webhook → Google Sheet → Grok LLM chain (normalize) → Collect → POST /api/run (Railway)
        → Gmail (email the HTML report) → Slack (confirmation)
```

n8n only orchestrates. The ML (YOLO + dual-VLM) runs on the Railway Flask app via the
`/api/*` endpoints in `api_analyze.py` — n8n cannot run PyTorch/YOLO itself (its Python
node is a sandboxed Pyodide runtime). The single `POST /api/run` call sends the whole
list of addresses and gets the finished, self-contained HTML report back, so n8n never
has to loop per row.

### Node walk-through
1. **Webhook** — `POST` to its URL starts a run; it replies `{"status":"started"}`
   immediately (so it never times out on a long batch) and the rest runs async.
2. **Read addresses** (Google Sheets) — reads every row.
3. **Normalize (Grok)** — a Basic LLM Chain with an **xAI Grok** chat model rewrites
   each row's address into a clean `number street, city, STATE ZIP` line.
4. **Collect addresses** (Aggregate) — gathers the normalized lines into one list
   (the only "glue" node; turns N rows into one batch payload).
5. **Analyze on Railway** (HTTP) — one `POST /api/run` with `{addresses, title}` →
   returns the HTML report as text (`$json.data`).
6. **Email report** (Gmail) — inlines the HTML report.
7. **Slack confirmation** — posts "analysis complete — N addresses, report emailed".

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
   - **Google Sheets** (OAuth2) → *Read addresses* node.
   - **xAI** (`xAiApi`) → *Grok Chat Model* node.
   - **Gmail** (OAuth2) → *Email report* node.
   - **Slack** (OAuth2 or API token) → *Slack confirmation* node.
3. Replace the placeholders:
   - *Read addresses*: set the Google Sheet (`documentId`) and tab (`sheetName`).
   - *Analyze on Railway*: set the URL to `https://YOUR-APP.up.railway.app/api/run`
     and the `X-API-Key` header to the `ANALYZE_API_KEY` you created above.
   - *Email report*: set `sendTo`.
   - *Slack confirmation*: set the channel.
   > Tip: instead of pasting the key, create an n8n **Header Auth** credential
   > (`X-API-Key: <key>`) and switch the HTTP node to "Generic Credential → Header Auth".

### 3. The sheet
One row per address. Column headers (case-sensitive):
- `Address` — **required**, the full street address.
- `Boro_Area` — optional (borough / city / area).
- `Zip` — optional.

## Running
`POST` to the Webhook URL (n8n shows it on the *Webhook* node — there's a test URL
and a production URL; activate the workflow to use the production one). It replies
`{"status":"started"}` immediately, then analyzes the whole sheet, emails the report,
and posts a Slack confirmation. A single bad address does not abort the run (the
analyzer encodes the failure in that address's card instead).

## The Grok normalization step
*Normalize (Grok)* is a Basic LLM Chain wired to an **xAI Grok** chat model; it
rewrites each row into a clean `number street, city, STATE ZIP` line before geocoding,
which helps with messy sales-CSV formats. If your n8n version doesn't have the xAI
Grok chat-model node, swap *Grok Chat Model* for any other chat-model sub-node
(OpenAI, Gemini, etc.) — the chain itself is model-agnostic. To skip normalization
entirely, disable *Normalize (Grok)* and point *Collect addresses* at the raw
`Address` field instead of `text`.

## Notes / limits
- **Cost:** each address is ~$0.10–$0.30 of VLM spend (Gemini + Grok), plus one small
  Grok call per row for normalization. Spend is proportional to sheet size.
- **Large runs (>~200 addresses):** the report embeds every annotated image as base64,
  so very large reports get heavy for email clients, and `/api/run` is one long
  request. For big batches, split the sheet, or have the *Email report* step write to
  Google Drive instead of inlining. (A chunked/streamed report can be added if this
  becomes routine.)
- **Secrets:** all model/API keys live on the Railway service, not in the workflow
  JSON. n8n holds only the Railway URL + the shared `X-API-Key` (+ its own Sheets/
  xAI/Gmail/Slack credentials).

## Endpoint reference (`api_analyze.py`)
| Method | Path | Auth | Body | Returns |
|--------|------|------|------|---------|
| GET | `/api/health` | none | — | `{"status":"ok"}` |
| POST | `/api/run` | `X-API-Key` | `{addresses:[str\|{address,boro_area,zip}], title?}` | `text/html` audit report (**the n8n path**) |
| POST | `/api/analyze` | `X-API-Key` | `{address, boro_area?, zip?}` | one web_entry JSON (images as `data:` URIs) |
| POST | `/api/report` | `X-API-Key` | `{results:[web_entry,...], title?}` | `text/html` audit report |
