# Main database staging preparation

`BFAMS-DeFI` is selected as a **staging target**, not as a production endpoint.
The application defaults to `BFAMS-DeFI-test`. Set
`APP_DATABASE_TARGET=main` only for a separate staging process; this target
refuses any configured `DB_NAME` other than `BFAMS-DeFI`. Authentication,
ownership checks, portfolio routes, and the dashboard then use `BFAMS-DeFI`.
Setu remains TEST-only.

The read-only migration plan is:

```powershell
.\.venv\Scripts\python.exe scripts\migrate_main_db.py
```

The script refuses to run unless both the configured and connected database
names are exactly `BFAMS-DeFI`. It detects schema mismatches and defaults to a
read-only dry run. Apply the additive schema changes with:

```powershell
.\.venv\Scripts\python.exe scripts\migrate_main_db.py --apply
```

The migration creates model tables that do not exist yet and adds the nullable
`investment_holdings.mf_scheme_id` column and its index. It does not update or
delete application rows. The dashboard must not be pointed at this database
until a fresh staging login and Dhan account are created. The bootstrap is
main-database-only, prompts for the new password, and does not copy TEST
credentials, MFA secrets, connections, or holdings:

```powershell
.\.venv\Scripts\python.exe scripts\bootstrap_staging_user.py --email you@example.com
.\.venv\Scripts\python.exe scripts\bootstrap_staging_user.py --email you@example.com --apply
```

Run the app on a separate port with a process-scoped target selection:

```powershell
$env:APP_DATABASE_TARGET = "main"
$env:AUTH_REGISTRATION_ENABLED = "true"
$env:AUTH_COOKIE_SECURE = "false"
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8003
```

The non-secure cookie setting is only for this loopback HTTP instance; use
secure cookies when serving staging over HTTPS. Registration is enabled here
for local staging signup; keep the server bound to `127.0.0.1` and disable
registration before exposing it beyond this machine.

The TEST process remains the default. To refresh market data and then save a
daily staging valuation, use:

```powershell
.\.venv\Scripts\python.exe scripts\refresh_reference_data.py --target main
.\.venv\Scripts\python.exe scripts\create_daily_portfolio_snapshots.py --target main
```

Register the separate weekday staging job at 9:45 PM (after the existing TEST
refresh) with:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\register_staging_refresh_task.ps1
```

The staging job refreshes AMFI/NSE data first, then snapshots the last synced
holdings without calling Dhan. It records missing/stale-price counts and logs
under `data_private\logs`. The existing scheduled refresh task remains TEST-only.
The staging process serves the portfolio view at `http://127.0.0.1:8003/dashboard`;
the TEST diagnostic page remains at `/testing/dashboard`.

The dashboard also supports account-specific target allocation percentages.
Settings are stored in `portfolio_allocation_targets`, isolated by database and
account. Set targets for each listed asset type to exactly 100% in total;
actual allocation uses priced holdings, with unpriced rows excluded.
The staging table is created by `scripts\migrate_main_db.py --apply`; the
separate TEST table is created by `scripts\migrate_test_db.py`.
