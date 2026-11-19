# Main database staging preparation

`BFAMS-DeFI` is selected as a **staging target**, not as a production endpoint.
The application still uses `BFAMS-DeFI-test` for authentication, portfolio
routes, and the dashboard. No records are copied from TEST to staging by this
schema migration.

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
until environment selection, authentication, and ownership checks are
configured and tested for staging.
