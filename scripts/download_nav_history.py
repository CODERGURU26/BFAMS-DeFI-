import sys
import time
from datetime import date, timedelta
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx

from app.nav_parser import parse_historical_nav_lines

URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
START = date(2021, 10, 14)
END = date(2026, 10, 14)
WINDOW_DAYS = 80
PAUSE_SECONDS = 5
MAX_RETRIES = 3
BATCH_LIMIT = None  # None = download all remaining missing windows
OUT_DIR = Path("data/amfi/history")

MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"
]


def fmt(d: date) -> str:
    return f"{d.day}-{MONTHS[d.month - 1]}-{d.year}"


def generate_windows(start_date: date, end_date: date, window_days: int) -> list[tuple[date, date]]:
    windows, d = [], start_date
    while d <= end_date:
        end_w = min(d + timedelta(days=window_days - 1), end_date)
        windows.append((d, end_w))
        d += timedelta(days=window_days)
    return windows


def fetch_window_content(client: httpx.Client, start: date, end: date) -> bytes:
    params = {"tp": 1, "frmdt": fmt(start), "todt": fmt(end)}
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"    Fetching attempt {attempt}/{MAX_RETRIES} (frmdt={fmt(start)}, todt={fmt(end)})...")
            resp = client.get(URL, params=params)

            if resp.status_code != 200:
                print(f"    [Attempt {attempt}] HTTP status {resp.status_code}. Retrying in {attempt * 5}s...")
                time.sleep(attempt * 5)
                continue

            body = resp.content
            if len(body) < 1000:
                print(f"    [Attempt {attempt}] Content too small ({len(body)} bytes). Retrying in {attempt * 5}s...")
                time.sleep(attempt * 5)
                continue

            if body.lstrip()[:1] == b"<":
                print(f"    [Attempt {attempt}] Received HTML error response instead of data. Retrying in {attempt * 5}s...")
                time.sleep(attempt * 5)
                continue

            # Check header signature in body
            first_line = body.split(b"\n", 1)[0].decode("utf-8", errors="ignore").strip()
            if not first_line.startswith("Scheme Code;"):
                print(f"    [Attempt {attempt}] Unexpected header: {first_line[:80]}. Retrying in {attempt * 5}s...")
                time.sleep(attempt * 5)
                continue

            return body

        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError) as exc:
            last_error = exc
            print(f"    [Attempt {attempt}] Network error ({type(exc).__name__}). Retrying in {attempt * 5}s...")
            time.sleep(attempt * 5)

    raise RuntimeError(f"Failed to fetch window {start} to {end} after {MAX_RETRIES} attempts. Last error: {last_error}")


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    all_windows = generate_windows(START, END, WINDOW_DAYS)

    # Filter to only missing windows
    missing_windows = []
    for start, end in all_windows:
        target_path = OUT_DIR / f"NAVHistory_{start}_{end}.txt"
        if target_path.exists() and target_path.stat().st_size > 1000:
            print(f"Skipping already completed window: {target_path.name} ({target_path.stat().st_size / (1024*1024):.2f} MB)")
            continue
        missing_windows.append((start, end))

    print(f"\nTotal planned windows: {len(all_windows)}")
    print(f"Already completed: {len(all_windows) - len(missing_windows)}")
    print(f"Missing windows remaining: {len(missing_windows)}")

    if not missing_windows:
        print("All windows are already downloaded!")
        return

    batch = missing_windows[:BATCH_LIMIT]
    print(f"\nProcessing batch of {len(batch)} windows (BATCH_LIMIT={BATCH_LIMIT}):")
    for i, (s, e) in enumerate(batch, 1):
        print(f"  {i}. {s} to {e} ({(e - s).days + 1} days)")

    results = []

    with httpx.Client(
        timeout=120.0,
        follow_redirects=True,
        headers={"User-Agent": "BFAMS-DeFi research project"},
    ) as client:
        for idx, (start, end) in enumerate(batch, 1):
            filename = f"NAVHistory_{start}_{end}.txt"
            target_path = OUT_DIR / filename
            temp_path = OUT_DIR / f"{filename}.tmp"

            print(f"\n--- [{idx}/{len(batch)}] Downloading: {filename} ---")
            t0 = time.perf_counter()

            try:
                body = fetch_window_content(client, start, end)
                temp_path.write_bytes(body)

                # Validate parseability with existing historical NAV parser
                print("    Validating content with tested historical NAV parser...")
                with temp_path.open("r", encoding="utf-8") as f:
                    schemes, navs, rejected, stats = parse_historical_nav_lines(f)

                if not navs:
                    raise ValueError("Validation failed: Zero valid NAV observations parsed")

                dates = [n["nav_date"] for n in navs]
                earliest_date = min(dates)
                latest_date = max(dates)
                distinct_dates = len(set(dates))

                # Check date boundary sanity
                if earliest_date < start or latest_date > end:
                    print(f"    Notice: Observed dates ({earliest_date} to {latest_date}) slightly differ from query window ({start} to {end})")

                # Atomically replace temporary file with final file
                temp_path.replace(target_path)
                file_size_bytes = target_path.stat().st_size
                elapsed = time.perf_counter() - t0

                invalid_nav_count = sum(1 for r in rejected if r.get("reason") == "invalid NAV")
                nonpositive_nav_count = stats.get("nonpositive_nav_skipped", 0)
                other_rejected_count = len(rejected) - invalid_nav_count

                res = {
                    "filename": filename,
                    "requested_start": start,
                    "requested_end": end,
                    "size_bytes": file_size_bytes,
                    "size_mb": file_size_bytes / (1024 * 1024),
                    "schemes_count": len(schemes),
                    "nav_count": len(navs),
                    "rejected_count": len(rejected),
                    "invalid_nav_count": invalid_nav_count,
                    "nonpositive_nav_count": nonpositive_nav_count,
                    "other_rejected_count": other_rejected_count,
                    "distinct_dates": distinct_dates,
                    "earliest_date": earliest_date,
                    "latest_date": latest_date,
                    "elapsed_sec": elapsed,
                    "stats": dict(stats),
                }
                results.append(res)

                print(f"    SUCCESS: Saved {filename}")
                print(f"    Size: {res['size_mb']:.2f} MB | Schemes: {res['schemes_count']:,} | Observations: {res['nav_count']:,}")
                print(f"    Unavailable (N.A./non-numeric) NAVs: {invalid_nav_count:,} | Zero/Non-positive NAVs: {nonpositive_nav_count:,} | Other rejections: {other_rejected_count}")
                print(f"    Date Range Found: {earliest_date} to {latest_date} ({distinct_dates} distinct dates)")
                print(f"    Elapsed: {elapsed:.2f}s")

            except Exception as exc:
                if temp_path.exists():
                    temp_path.unlink()
                print(f"\nFATAL: Failed on window {start} to {end}: {exc}")
                print("Stopping safely to allow resumption. Previously completed files remain intact.")
                raise

            # Courtesy delay between sequential requests
            if idx < len(batch):
                print(f"    Pausing {PAUSE_SECONDS}s before next request...")
                time.sleep(PAUSE_SECONDS)

    print("\n" + "=" * 60)
    print("BATCH DOWNLOAD COMPLETED SUCCESSFULLY")
    print("=" * 60)
    for r in results:
        print(f"File: {r['filename']}")
        print(f"  Dates Requested: {r['requested_start']} to {r['requested_end']}")
        print(f"  Dates Observed:  {r['earliest_date']} to {r['latest_date']} ({r['distinct_dates']} dates)")
        print(f"  Size: {r['size_mb']:.2f} MB ({r['size_bytes']:,} bytes)")
        print(f"  Schemes: {r['schemes_count']:,} | Valid NAVs: {r['nav_count']:,}")
        print(f"  Unavailable NAVs (N.A.): {r['invalid_nav_count']} | Zero NAVs: {r['nonpositive_nav_count']} | Other Rejections: {r['other_rejected_count']}")
        print("-" * 60)


if __name__ == "__main__":
    main()