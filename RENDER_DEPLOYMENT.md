# Render Deployment

This repo is configured for Render with `render.yaml`.

## What Is Already Set

The blueprint creates one Docker web service:

- Service name: `building-analyzer`
- Dockerfile: `Dockerfile.railway`
- Docker context: repo root
- Health check: `/api/health`
- Instance count: `1`
- Plan: `free` (no card required; spins down after 15 min idle, ~30-60s cold start on
  the next request — fine for occasional/interim use, but if n8n calls start timing
  out or a batch OOMs from the two-model YOLO ensemble, upgrade to `starter` or
  `standard` in render.yaml)
- Auto deploy: enabled

The Dockerfile already binds Gunicorn to Render's `PORT` env var:

```bash
gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 0 app:app
```

## What You Still Need To Add

When Render creates the service from the blueprint, it should ask for these secret values because they are marked `sync: false`:

- `GOOGLE_MAPS_API_KEY`
- `MAPBOX_API_KEY`
- `GEMINI_API_KEY`
- `XAI_API_KEY`
- `ANALYZE_API_KEY`

Use a long random value for `ANALYZE_API_KEY`; n8n will send it in the `X-API-Key` header.

The blueprint also sets `ANALYZE_FILE_MAX_ROWS=250` for uploaded spreadsheets. Raise it only when you intentionally want larger paid batches.

## Dashboard Steps

1. Push this branch to GitHub.
2. In Render, choose **New**.
3. Choose **Blueprint**.
4. Select this GitHub repo and the branch containing `render.yaml`.
5. Render should detect `render.yaml`.
6. Enter the five secret env vars above.
7. Create/apply the blueprint.
8. Wait for the first deploy to finish.

## Verify

Replace `YOUR-SERVICE` with the Render service URL:

```bash
curl https://YOUR-SERVICE.onrender.com/api/health
```

Expected:

```json
{"status":"ok"}
```

Then run one paid smoke test:

```bash
curl -X POST "https://YOUR-SERVICE.onrender.com/api/run" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $ANALYZE_API_KEY" \
  -d "{\"title\":\"Smoke Test\",\"addresses\":[\"140 West End Ave, New York, NY 10023\"]}" \
  -o smoke_report.html
```

## n8n

The primary operator path is the Claude skill `.claude/skills/parity-cooling-tower`, not n8n.

For a scheduled/triggered n8n run, import `n8n/parity_cooling_tower.workflow.json` (reads addresses from a Google Sheet) and update the analyzer HTTP node:

- URL: `https://YOUR-SERVICE.onrender.com/api/run`
- Header: `X-API-Key: <same ANALYZE_API_KEY from Render>`

The workflow also needs its own Google Sheets, xAI, Gmail, and optional Slack credentials. See `n8n/README.md`.

## Verify the API key

Confirm the analyzer key is live once secrets are set:

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST "https://YOUR-SERVICE.onrender.com/api/run" \
  -H "X-API-Key: $ANALYZE_API_KEY" -H "Content-Type: application/json" \
  -d '{"title":"key check","addresses":["140 West End Ave, New York, NY 10023"]}'
```

`200` means the key matches; `401` means it does not.
