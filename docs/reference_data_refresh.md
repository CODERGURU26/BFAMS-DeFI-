# Reference-data refresh

Run the combined refresh manually from the repository root:

```powershell
.\.venv\Scripts\python.exe scripts\refresh_reference_data.py
```

The job refreshes AMFI NAV data from the public AMFI feed and reprocesses the
most recent seven calendar days of NSE `EQ`, `BE`, and `RR` bhavcopy data.
The rolling window allows a delayed NSE file to be picked up on a later run;
the existing manifest and upsert logic make reprocessing safe. It writes the
daily AMFI snapshot to the ignored `data\amfi\NAVAll.txt` file.

The command is strictly restricted to `BFAMS-DeFI-test`. It reports the latest
stored date for each NSE series and AMFI NAV data, plus the priced/total count
for latest portfolio snapshots. Stale source data or partial portfolio
coverage is printed as a warning. A source/database failure returns a nonzero
exit code.

For Windows Task Scheduler, use:

- **Program:** `powershell.exe`
- **Arguments:** `-NoProfile -ExecutionPolicy Bypass -File "C:\path\to\BFAMS-DeFI\scripts\run_reference_data_refresh.ps1"`
- **Start in:** `C:\path\to\BFAMS-DeFI`

Schedule it for weekdays after the NSE close (for example, 9:15 PM local time).
The wrapper writes timestamped logs under `data_private\logs`. Keep the machine
awake at the scheduled time and configure Task Scheduler to run missed tasks
as soon as possible.
