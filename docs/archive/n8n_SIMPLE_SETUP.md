# Simple n8n test workflow — setup (for tomorrow's test run with Alex)

File: `parity_cooling_tower_simple.workflow.json`

```
Run test (manual) → Read addresses (Google Sheet) → Collect → POST /api/run (Railway) → Email report (Gmail)
```

This is the stripped-down version: 5 nodes, only **two credentials** (Google Sheets + Gmail)
plus the Railway URL/key. No Grok-normalize, no Slack — add those later from the full
workflow (`parity_cooling_tower.workflow.json`) once the basic loop is proven.

## Prerequisites
1. **Railway is live** with the analyzer (you said you'll spin it up). You need:
   - the app URL, e.g. `https://parity-...up.railway.app`
   - the `ANALYZE_API_KEY` you set on Railway
   - confirm health first: open `https://YOUR-APP.up.railway.app/api/health` → `{"status":"ok"}`
2. A **Google Sheet** with one column header `Address` and 2–3 full addresses, e.g.:
   | Address |
   |---|
   | 140 West End Ave, New York, NY |
   | 22 North 6th St, Brooklyn, NY |

## Import + wire (5 minutes)
1. n8n → **Workflows → Import from File** → pick `parity_cooling_tower_simple.workflow.json`.
2. **Read addresses** node → add/select your **Google Sheets** credential → set the Sheet
   (`documentId`) and tab (`sheetName`).
3. **Analyze on Railway** node → replace:
   - URL: `https://YOUR-APP.up.railway.app/api/run`
   - header `X-API-Key`: your `ANALYZE_API_KEY`
4. **Email report** node → add/select your **Gmail** credential → set **sendTo** to your address.
5. Click **Execute Workflow** on the *Run test* node.

## What you'll see
- Each node lights green in order; *Analyze on Railway* spins for a minute or two
  (each address is ~15–30s), then the report lands in your inbox as the email body.

## Notes
- The Address column should be the FULL address (the analyzer cleans/geocodes it; this
  simple version skips the Grok normalization step). Need messy-sheet handling? Use the
  full workflow which adds the normalize step.
- Want the **Excel attachment** in the email too? That's a follow-up: it needs the
  analyzer to return the tabular data (a small `/api/run` change or a per-row `/api/analyze`
  loop + an n8n "Convert to File / Spreadsheet" node). Out of scope for the first test —
  prove the email loop first.
- One bad address won't abort the run; its failure is shown in that address's card.
