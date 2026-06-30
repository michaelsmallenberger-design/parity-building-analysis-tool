# Render Deployment

This repo is configured for Render with `render.yaml`.

## What Is Already Set

The blueprint creates one Docker web service:

- Service name: `building-analyzer`
- Dockerfile: `Dockerfile.railway`
- Docker context: repo root
- Health check: `/api/health`
- Instance count: `1`
- Plan: `standard`
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

After the Render service is live, open `n8n/parity_cooling_tower.workflow.json` in n8n and update the analyzer HTTP node:

- URL: `https://YOUR-SERVICE.onrender.com/api/run`
- Header: `X-API-Key: <same ANALYZE_API_KEY from Render>`

The workflow still needs its own Google Sheets, xAI, Gmail, and optional Slack credentials.
