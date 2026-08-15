---
name: parity-cleanup-failed
description: Diagnose and re-run the failed buildings in a parity analysis batch (no imagery, geocode miss, analyzer error) and merge fresh results back into the same review page and Google Sheet. Use when a batch has error rows, a building shows "No Images"/"Image Download Failed"/"Geocoding Failed", or someone says "clean up the failures", "fix the failed buildings", "re-run the misses".
---

# Parity cleanup — fix and re-run failed buildings

A batch's failed rows (imagery blips, geocode misses, bad addresses) get diagnosed,
fixed, and re-run **in place**: the fresh results merge into the same batch, so the
team's existing `/review/<batch_id>` page and the batch's Google Sheet show them on
reload. No new batch, no new links.

## Prerequisites
- **Analyzer API key (`KEY`).** Same as the main `parity-cooling-tower` skill: try
  `.env` `ANALYZE_API_KEY` / `PARITY_ANALYZE_KEY`; on `401`, pull the live value from
  the Render API (`RENDER_API_KEY`, service `srv-d92qip4vikkc73avfb90`) — the local
  `.env` is often stale.
- **Base URL:** `https://building-analyzer.onrender.com`
- **Input:** a batch id (`b-xxxxxxxxxx`) or a review URL containing one.

## Step 1 — list the failures

```
GET /api/batch/BATCH_ID/failures      (header X-API-Key: KEY)
```

Returns `{count, failures: [{row_id, address, error, verdict, notes, has_image}]}`.
Failure kinds you will see in `error`: `Empty Address`, `No Address`,
`Geocoding Failed`, `Image Download Failed`, `Analyzer Error`,
`Unresolved transient failure`. If `count` is 0, say so and stop.

## Step 2 — diagnose each row

- **`Image Download Failed` / `Analyzer Error` / `Unresolved transient failure`** —
  almost always a transient provider blip. Plain re-run, no address change.
- **`Geocoding Failed` / `Empty Address` / `No Address`** — the address is the
  problem. Build a corrected, **fully-qualified** address (street, city, state, ZIP):
  check the original sheet row for boro/zip fragments, fix obvious typos, expand
  abbreviations. If the address is genuinely ambiguous (e.g. a street name that
  exists in both Arlington VA and Washington DC), pick the best candidate but SAY SO
  in your report — geographic assumptions must be surfaced, not silent.
- A row that already looks unfixable (no address anywhere in the row) → skip it and
  report it as needs-human-input.

## Step 3 — re-run

```
POST /api/batch/BATCH_ID/rerun        (header X-API-Key: KEY, JSON body)
{"rows": [{"row_id": 3}, {"row_id": 7, "address": "1100 Wilson Blvd, Arlington, VA 22209"}]}
```

- Include `address` ONLY for rows you corrected; omit it for plain re-runs.
- An empty body re-runs every currently-failed row as-is — fine when everything
  looked transient.
- **Runtime warning:** each building takes roughly 30–40s. More than 3 rows will
  outlive a default 2-minute foreground call — run the request in the background
  (or raise the timeout to ≥600s). Do not let a shell timeout kill a paid run.
- The response's `rerun` list gives per-row `status`/`verdict`/`error`, plus
  `remaining_failures` for the whole batch.

Notes on what the server does: replaced rows drop any stale human decision (it was
made against the failed result, so those buildings need a fresh look on the review
page); corrected addresses are also written into the batch table and, when the batch
has a live sheet, into the sheet's address cell. Avoid re-running rows the team is
actively reviewing at that moment — last write wins.

## Step 4 — verify and iterate

Re-check `GET /api/batch/BATCH_ID/failures`. If rows still fail, retry ONCE more
(step 2 diagnosis again — a repeat imagery failure on the same building usually means
no provider has a usable tile there, not a blip). Two rounds maximum; after that,
report the survivors as needs-human-input rather than burning more spend.

## Step 5 — report

Tell the operator, in plain sentences:
- before → after failure counts, and what was wrong (transient vs bad address);
- every address you corrected, original → corrected, flagging any ambiguity calls;
- which rows still fail and why;
- that the review page and sheet already show the fresh rows — reviewers should
  re-check the re-run buildings (their earlier picks on failed rows were cleared).
