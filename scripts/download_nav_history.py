import time
from datetime import date, timedelta
from pathlib import Path

import httpx

URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
START = date(2021, 10, 14)   # edit: how far back
END = date(2026, 10, 13)     # edit: last day wanted
WINDOW_DAYS = 80             # AMFI allows at most 90 days per request
MAX_WINDOWS = 1              # 1 = test run; set to None for the full run
PAUSE_SECONDS = 5
OUT_DIR = Path("data/amfi/history")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt(d):
    return f"{d.day}-{MONTHS[d.month - 1]}-{d.year}"


OUT_DIR.mkdir(parents=True, exist_ok=True)
windows, d = [], START
while d <= END:
    windows.append((d, min(d + timedelta(days=WINDOW_DAYS - 1), END)))
    d += timedelta(days=WINDOW_DAYS)
if MAX_WINDOWS:
    windows = windows[:MAX_WINDOWS]
print("windows to fetch:", len(windows))

with httpx.Client(timeout=120, follow_redirects=True,
                  headers={"User-Agent": "BFAMS-DeFi research project"}) as client:
    for start, end in windows:
        path = OUT_DIR / f"NAVHistory_{start}_{end}.txt"
        if path.exists() and path.stat().st_size > 1000:
            print("skip (already saved):", path.name)
            continue
        resp = client.get(URL, params={"tp": 1, "frmdt": fmt(start), "todt": fmt(end)})
        body = resp.content
        if resp.status_code != 200 or len(body) < 1000 or body.lstrip()[:1] == b"<":
            print("STOP: unexpected response for", start, end,
                  "status", resp.status_code, "bytes", len(body))
            raise SystemExit(1)
        path.write_bytes(body)
        print("saved", path.name, "|", len(body) // 1024, "KB |",
              body.count(b"\n"), "lines")
        time.sleep(PAUSE_SECONDS)