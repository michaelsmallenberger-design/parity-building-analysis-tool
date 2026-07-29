# Market Runway Dashboard

This module turns a private Google Sheet into the working data source for the
React market-runway dashboard without giving Render access to the Sheet.

## How it works

1. An editor updates the five `Dashboard ...` tabs in the private Sheet.
2. The editor chooses **Dashboard -> Publish dashboard**.
3. The bound Apps Script validates the workbook, signs the exact JSON body with
   HMAC-SHA256, and sends it to the existing Render service.
4. Render validates the signature and business totals, writes an immutable
   version to the persistent disk, and atomically promotes it to `current.json`.
5. The password-protected `/market-runway` page loads the current version.

The public internet never receives a Sheet credential, the Sheet does not need
to be shared outside the Workspace, and an invalid update cannot replace the
last good version.

## Production configuration

- Render secret: `MARKET_DASHBOARD_PUBLISH_SECRET` (at least 32 random
  characters).
- Persistent path: `MARKET_DASHBOARD_STORAGE_DIR`, configured as
  `/var/data/storage/market_dashboard`.
- Bound Sheet source: copy `apps_script/Code.gs` and `apps_script/appsscript.json`
  into an Apps Script project owned by the Sheet.
- The same publish secret must be stored in the bound script's Script
  Properties through **Dashboard -> Configure secure publishing**.

Dashboard viewers continue to use the analyzer's existing password gate. The
publish endpoint is separate: it requires a fresh timestamp, a one-use nonce,
and a matching body signature.

## Updating and recovery

- Sheet changes are drafts until an editor explicitly publishes.
- Publishing identical data is idempotent and does not create noise.
- `/market-runway/versions` lists retained versions for authenticated viewers.
- `POST /market-runway/rollback/<version_id>` creates a new version pointing at
  the validated prior data; it does not erase history.

## Verification

Run the network-free checks:

```powershell
python -m unittest test_market_dashboard.py
python -m compileall app_railway.py market_dashboard
```

Build the browser bundle:

```powershell
Set-Location market_dashboard/frontend
npm ci
npm run build
```
