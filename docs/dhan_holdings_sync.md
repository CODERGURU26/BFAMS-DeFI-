# Dhan holdings sync

The local portfolio dashboard at `/testing/dashboard` has an explicit
**Sync Dhan holdings** action. `DEMO_SYNC_ENABLED` must be `true` in the
process environment; the default is disabled. The route also requires
`X-BFAMS-Sync: 1`, a `localhost:<port>` or `127.0.0.1:<port>` Host, and (if
present) a same-origin Origin.

The endpoint requires the TEST database (`BFAMS-DeFI-test`) and the existing
`DHAN-DEMAT-1` account attached to provider `dhan_manual`. It makes only a
`GET https://api.dhan.co/v2/holdings` request, with `DHAN_CLIENT_ID` and
`DHAN_ACCESS_TOKEN` supplied in the process environment. It does not request
or store trades.

The default `dry_run=true` fetches and validates the holdings, reads the latest
stored snapshot for reconciliation, and returns counts without inserting
anything. The dashboard shows those counts and requires a separate explicit
confirmation before it sends `dry_run=false`. An apply writes positive-
quantity holdings and one audit row in a single commit. Zero-quantity items
are skipped. The route limits attempts to one per connection per 60 seconds.
Failed apply attempts are rolled back and recorded in a fresh transaction with
only the exception type in the audit error field. No older snapshot price is
copied into a new snapshot.

Because this API response does not provide valuation, new rows store NULL unit
prices and market values. Equity valuation can use the stored
`InvestmentHolding.unit_price` as its fallback when no NSE close exists; this
sync intentionally leaves that value NULL.

## Live Dhan valuation

The dashboard's **Fetch from Dhan** action calls the read-only
`POST /portfolio/dhan/live-valuation` route. It requires the same local-only
guards as holdings sync and verifies the TEST database, but it does not write
holdings, prices, or audit rows.

The calculation uses Dhan's documented `avgCostPrice` and `totalQty` fields
for invested amount, then the Dhan market-feed quote endpoint's `last_price`
and `net_change` fields for current value, overall P&L, and today's P&L. The
official detailed instrument master is matched on security ID and ISIN to
determine the NSE/BSE quote segment. Individual holding data is not returned
by this route. Current value and P&L remain unavailable unless every positive-
quantity holding has a valid quote; a partial quote set is never shown as the
account total.

The Dhan access token must be authorized for Market Quote API access in
addition to Holdings. A market-feed HTTP 401 is surfaced with its status only;
the provider response body and account data are not exposed. `GET /positions`
is not used for demat valuation because it covers open positions for the day
and may be empty when there are no open positions.

The API client, response validation, and row mapping can be checked without
network or database access with:

```powershell
.\.venv\Scripts\python.exe -m unittest testing.test_dhan_sync
```

No live Dhan request is made by these tests.
