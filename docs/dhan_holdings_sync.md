# Dhan holdings sync

The local portfolio dashboard at `/testing/dashboard` now has an explicit
**Sync Dhan holdings** action. The dashboard never starts a sync on page load;
the action requires confirmation and calls `POST /portfolio/dhan/sync`.

The endpoint requires the TEST database (`BFAMS-DeFI-test`) and the existing
`DHAN-DEMAT-1` account attached to provider `dhan_manual`. It makes one
`GET https://api.dhan.co/v2/holdings` request, with credentials supplied as
`DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` via the app's environment settings.
It does not request or store trades. The sync writes only an
`investment_holdings` snapshot and one `data_sync_runs` audit row in a single
database commit. Since the holdings response provides no valuation, new rows
store NULL unit prices and market values; the existing portfolio valuation
path may resolve prices from local reference data.

The API client, response validation, and row mapping can be checked without
network or database access with:

```powershell
.\.venv\Scripts\python.exe -m unittest testing.test_dhan_sync
```

No live Dhan request is made by these tests.
