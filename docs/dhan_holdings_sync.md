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

## Live Dhan positions and P&L

The dashboard's **Fetch from Dhan** action calls the read-only
`POST /portfolio/dhan/live-valuation` route. It uses only the Dhan Holdings,
Positions, and Orders APIs. It requires the same local-only guards as holdings
sync and verifies the TEST database, but does not write holdings, prices, or
audit rows. Orders are read only to report today's order count; the integration
never places, modifies, or cancels an order.

Realized and unrealized P&L are summed from the position rows. The adapter
prefers `realizedPnl` and `unrealizedPnl` when present and also supports the
documented Dhan v2 field names `realizedProfit` and `unrealizedProfit`.
`dayPnl` is reported only when Dhan supplies it; the documented v2 Positions
schema does not currently list that field, so the application does not
calculate a substitute. Open positions are returned as the small set of
instrument, position/product type, net quantity, and provider P&L fields
needed by the dashboard.

No Market Feed/Market Quote endpoint or instrument-master download is used.
The current-value total and holdings cost basis are not included in this
summary because the permitted APIs used here do not provide a guaranteed
complete current market valuation for the demat holdings.

The API client, response validation, and row mapping can be checked without
network or database access with:

```powershell
.\.venv\Scripts\python.exe -m unittest testing.test_dhan_sync
```

No live Dhan request is made by these tests.
