# Attach the one-click publisher

The publisher is deliberately bound to the existing private Google Sheet. It
does not share the Sheet with Render and does not use a service account.

1. Open the working Google Sheet.
2. Open **Extensions → Apps Script**.
3. Replace `Code.gs` with this folder's `Code.gs`.
4. In **Project Settings**, enable the manifest file and replace it with
   `appsscript.json`.
5. Save, reload the Sheet, and use **Dashboard → Set up dashboard tabs**.
6. Use **Dashboard → Configure secure publishing** once. The URL defaults to
   `https://parity-market-dashboard.onrender.com/api/market-dashboard/publish`.
   Paste the same secret stored in Render as
   `MARKET_DASHBOARD_PUBLISH_SECRET`.
7. Edit the normalized dashboard tabs and choose
   **Dashboard → Publish dashboard**.

The script never overwrites an existing tab. A failed validation or failed
Render request leaves the prior published dashboard version live.
