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
$env:AUTH_REGISTRATION_ENABLED = "false"
$env:AUTH_COOKIE_SECURE = "false"
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8003
```

The non-secure cookie setting is only for this loopback HTTP instance; use
secure cookies when serving staging over HTTPS.

The TEST process remains the default. Refresh staging reference data explicitly
before importing holdings:

```powershell
.\.venv\Scripts\python.exe scripts\refresh_reference_data.py --target main
```

The existing scheduled refresh task remains TEST-only.
The staging process serves the portfolio view at `http://127.0.0.1:8003/dashboard`;
the TEST diagnostic page remains at `/testing/dashboard`.
