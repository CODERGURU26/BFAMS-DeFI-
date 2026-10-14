import csv
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
import urllib.request
import urllib.error

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import SecurityDailyPrice

logger = logging.getLogger("nse_market_data")

# Official NSE archive endpoints
EQUITY_BHAVCOPY_URL_TEMPLATE = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
INDEX_DAILY_URL_TEMPLATE = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{ddmmyyyy}.csv"

# Documented series filtering policy:
# - Standard Mainboard ordinary equity: 'EQ' (Normal rolling settlement)
# - Trade-for-Trade Mainboard ordinary equity: 'BE' (T2T settlement)
# Excluded by default:
# - 'GB' (Sovereign Gold Bonds), 'GS' (Govt Securities), 'N1'-'N9' (NCDs), 'P1' (Preference)
# - 'IV' (InvITs), 'RR' (REITs), 'E1'/'X1' (Partly paid)
DEFAULT_ELIGIBLE_EQUITY_SERIES = ("EQ", "BE")

# Broad market indices to track
DEFAULT_BROAD_INDICES = (
    "Nifty 50",
    "Nifty Next 50",
    "Nifty 100",
    "Nifty 200",
    "Nifty 500",
    "Nifty Midcap 50",
    "Nifty Midcap 100",
    "Nifty Smallcap 100",
    "Nifty Bank",
    "Nifty IT",
)

HTTP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class DailyBar:
    symbol: str
    instrument_type: str  # 'equity' or 'index'
    exchange: str
    trading_date: date
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    series: Optional[str] = None
    last_price: Optional[Decimal] = None
    prev_close: Optional[Decimal] = None
    volume: Optional[int] = None
    turnover_lacs: Optional[Decimal] = None
    source: str = "NSE"


class MarketDataValidationError(Exception):
    pass


def validate_daily_bar(bar: DailyBar) -> list[str]:
    """
    Validates OHLCV integrity and boundary rules.
    Returns a list of error messages (empty if valid).
    """
    errors = []

    if not bar.symbol or not bar.symbol.strip():
        errors.append("Symbol cannot be empty")

    if bar.instrument_type not in ("equity", "index"):
        errors.append(f"Invalid instrument_type: {bar.instrument_type}")

    if not isinstance(bar.trading_date, date):
        errors.append(f"Invalid trading_date: {bar.trading_date}")

    for name, val in [
        ("open_price", bar.open_price),
        ("high_price", bar.high_price),
        ("low_price", bar.low_price),
        ("close_price", bar.close_price),
    ]:
        if val is None or val <= Decimal("0"):
            errors.append(f"{name} must be positive, got {val}")

    if bar.open_price and bar.high_price and bar.low_price and bar.close_price:
        if bar.high_price < bar.low_price:
            errors.append(f"High ({bar.high_price}) cannot be below Low ({bar.low_price})")
        if bar.high_price < bar.open_price:
            errors.append(f"High ({bar.high_price}) cannot be below Open ({bar.open_price})")
        if bar.high_price < bar.close_price:
            errors.append(f"High ({bar.high_price}) cannot be below Close ({bar.close_price})")
        if bar.low_price > bar.open_price:
            errors.append(f"Low ({bar.low_price}) cannot be above Open ({bar.open_price})")
        if bar.low_price > bar.close_price:
            errors.append(f"Low ({bar.low_price}) cannot be above Close ({bar.close_price})")

    if bar.volume is not None and bar.volume < 0:
        errors.append(f"Volume cannot be negative, got {bar.volume}")

    return errors


def _parse_decimal(val: Any) -> Optional[Decimal]:
    if val is None:
        return None
    s = str(val).strip().replace(",", "")
    if not s or s in ("-", "NA", "N/A"):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _parse_int(val: Any) -> Optional[int]:
    if val is None:
        return None
    s = str(val).strip().replace(",", "")
    if not s or s in ("-", "NA", "N/A"):
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _parse_bhavcopy_date(dt_str: str) -> date:
    """Parses date from NSE bhavcopy (e.g. '11-Oct-2024' or '11-10-2024')."""
    s = dt_str.strip()
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Cannot parse date: {dt_str}")


def _sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_nse_url_with_retries(
    url: str,
    max_retries: int = 3,
    initial_backoff: float = 1.0,
    timeout: float = 15.0,
    client: Optional[Any] = None,
) -> Tuple[int, bytes]:
    """
    Fetches URL with exponential backoff on transient errors.
    Uses httpx.Client if provided for persistent connection pooling.
    Returns (status_code, content_bytes).
    Returns (404, b'') if file not found (non-trading day).
    """
    headers = {
        "User-Agent": HTTP_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    backoff = initial_backoff
    for attempt in range(1, max_retries + 1):
        try:
            if client is not None:
                resp = client.get(url, timeout=timeout)
                if resp.status_code == 404:
                    return 404, b""
                resp.raise_for_status()
                return resp.status_code, resp.content
            else:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, b""
            if attempt == max_retries:
                raise
            time.sleep(backoff)
            backoff *= 2
        except Exception as e:
            # Handle httpx.HTTPStatusError (404) or transient network exceptions
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            if status_code == 404:
                return 404, b""
            if attempt == max_retries:
                raise
            time.sleep(backoff)
            backoff *= 2

    return 500, b""


def fetch_nse_bhavcopy_content(target_date: date, raw_dir: Optional[Path] = None) -> str:
    """
    Downloads daily equity bhavcopy from official NSE archives.
    Stores raw file in raw_dir if specified for audit provenance.
    """
    ddmmyyyy = target_date.strftime("%d%m%Y")
    url = EQUITY_BHAVCOPY_URL_TEMPLATE.format(ddmmyyyy=ddmmyyyy)

    status, content_bytes = fetch_nse_url_with_retries(url)
    if status == 404:
        raise FileNotFoundError(f"No NSE bhavcopy found for {target_date} (possibly weekend or exchange holiday)")

    content = content_bytes.decode("utf-8", errors="replace")

    if raw_dir:
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_file = raw_dir / f"sec_bhavdata_full_{ddmmyyyy}.csv"
        with open(raw_file, "w", encoding="utf-8") as f:
            f.write(content)

    return content


def fetch_nse_index_content(target_date: date, raw_dir: Optional[Path] = None) -> str:
    """
    Downloads daily index closing file from official NSE archives.
    Stores raw file in raw_dir if specified for audit provenance.
    """
    ddmmyyyy = target_date.strftime("%d%m%Y")
    url = INDEX_DAILY_URL_TEMPLATE.format(ddmmyyyy=ddmmyyyy)

    status, content_bytes = fetch_nse_url_with_retries(url)
    if status == 404:
        raise FileNotFoundError(f"No NSE index data found for {target_date} (possibly weekend or exchange holiday)")

    content = content_bytes.decode("utf-8", errors="replace")

    if raw_dir:
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_file = raw_dir / f"ind_close_all_{ddmmyyyy}.csv"
        with open(raw_file, "w", encoding="utf-8") as f:
            f.write(content)

    return content


def parse_equity_bhavcopy(
    csv_text: str,
    target_symbols: Optional[Sequence[str]] = None,
    series_filter: Optional[Any] = "EQ",
    eligible_series: Optional[Sequence[str]] = None,
) -> list[DailyBar]:
    """
    Parses equities from NSE bhavcopy CSV text.
    Filters by eligible ordinary equity series (e.g. 'EQ', 'BE') and optional symbols.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames:
        reader.fieldnames = [col.strip() for col in reader.fieldnames]

    # Resolve allowed series
    allowed_series: Optional[Set[str]] = None
    if eligible_series:
        allowed_series = {s.upper().strip() for s in eligible_series}
    elif series_filter:
        if isinstance(series_filter, (list, tuple, set)):
            allowed_series = {s.upper().strip() for s in series_filter}
        elif isinstance(series_filter, str):
            allowed_series = {series_filter.upper().strip()}

    symbols_set = {s.upper().strip() for s in target_symbols} if target_symbols else None
    bars = []

    for row in reader:
        clean_row = {k.strip(): (v.strip() if v else "") for k, v in row.items() if k}
        sym = clean_row.get("SYMBOL", "").upper()
        series = clean_row.get("SERIES", "").upper()

        if allowed_series and series not in allowed_series:
            continue

        if symbols_set and sym not in symbols_set:
            continue

        dt = _parse_bhavcopy_date(clean_row.get("DATE1", ""))
        o = _parse_decimal(clean_row.get("OPEN_PRICE"))
        h = _parse_decimal(clean_row.get("HIGH_PRICE"))
        l = _parse_decimal(clean_row.get("LOW_PRICE"))
        c = _parse_decimal(clean_row.get("CLOSE_PRICE"))
        last = _parse_decimal(clean_row.get("LAST_PRICE"))
        prev = _parse_decimal(clean_row.get("PREV_CLOSE"))
        vol = _parse_int(clean_row.get("TTL_TRD_QNTY"))
        turnover = _parse_decimal(clean_row.get("TURNOVER_LACS"))

        bar = DailyBar(
            symbol=sym,
            series=series,
            instrument_type="equity",
            exchange="NSE",
            trading_date=dt,
            open_price=o,
            high_price=h,
            low_price=l,
            close_price=c,
            last_price=last,
            prev_close=prev,
            volume=vol,
            turnover_lacs=turnover,
            source="NSE_BHAVCOPY",
        )
        bars.append(bar)

    return bars


def parse_index_daily(
    csv_text: str,
    target_indices: Optional[Sequence[str]] = None,
) -> list[DailyBar]:
    """
    Parses indices from NSE index closing CSV text.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames:
        reader.fieldnames = [col.strip() for col in reader.fieldnames]

    target_set = {i.lower().strip() for i in target_indices} if target_indices else None
    bars = []

    for row in reader:
        clean_row = {k.strip(): (v.strip() if v else "") for k, v in row.items() if k}
        name = clean_row.get("Index Name", "").strip()

        if target_set and name.lower() not in target_set:
            continue

        dt_str = clean_row.get("Index Date") or clean_row.get("Historical Date", "")
        dt = _parse_bhavcopy_date(dt_str)
        o = _parse_decimal(clean_row.get("Open Index Value") or clean_row.get("Open"))
        h = _parse_decimal(clean_row.get("High Index Value") or clean_row.get("High"))
        l = _parse_decimal(clean_row.get("Low Index Value") or clean_row.get("Low"))
        c = _parse_decimal(clean_row.get("Closing Index Value") or clean_row.get("Close"))
        vol = _parse_int(clean_row.get("Volume"))
        turnover_cr = _parse_decimal(clean_row.get("Turnover (Rs. Cr.)") or clean_row.get("Turnover"))
        turnover_lacs = (turnover_cr * Decimal("100")) if turnover_cr is not None else None

        bar = DailyBar(
            symbol=name,
            series="IN",
            instrument_type="index",
            exchange="NSE",
            trading_date=dt,
            open_price=o,
            high_price=h,
            low_price=l,
            close_price=c,
            last_price=c,
            prev_close=None,
            volume=vol,
            turnover_lacs=turnover_lacs,
            source="NSE_INDICES",
        )
        bars.append(bar)

    return bars


def upsert_security_daily_prices(
    session: Session,
    bars: Sequence[DailyBar],
    chunk_size: int = 500,
) -> dict[str, int]:
    """
    Validates and upserts daily bars into security_daily_prices using multi-row chunks.
    Idempotent: updates existing records on (symbol, trading_date, instrument_type) conflict.
    """
    valid_bars = []
    rejected = 0

    for b in bars:
        errs = validate_daily_bar(b)
        if errs:
            rejected += 1
            continue
        valid_bars.append(b)

    if not valid_bars:
        return {"processed": len(bars), "inserted_or_updated": 0, "rejected": rejected}

    inserted_or_updated = 0
    # Process in chunks of chunk_size for high performance
    for i in range(0, len(valid_bars), chunk_size):
        chunk = valid_bars[i : i + chunk_size]
        vals = [
            {
                "symbol": bar.symbol,
                "series": bar.series,
                "instrument_type": bar.instrument_type,
                "exchange": bar.exchange,
                "trading_date": bar.trading_date,
                "open_price": bar.open_price,
                "high_price": bar.high_price,
                "low_price": bar.low_price,
                "close_price": bar.close_price,
                "last_price": bar.last_price,
                "prev_close": bar.prev_close,
                "volume": bar.volume,
                "turnover_lacs": bar.turnover_lacs,
                "source": bar.source,
            }
            for bar in chunk
        ]

        stmt = pg_insert(SecurityDailyPrice).values(vals)
        upsert_stmt = stmt.on_conflict_do_update(
            constraint="uq_security_daily_prices_symbol_date_type",
            set_={
                "open_price": stmt.excluded.open_price,
                "high_price": stmt.excluded.high_price,
                "low_price": stmt.excluded.low_price,
                "close_price": stmt.excluded.close_price,
                "last_price": stmt.excluded.last_price,
                "prev_close": stmt.excluded.prev_close,
                "volume": stmt.excluded.volume,
                "turnover_lacs": stmt.excluded.turnover_lacs,
                "source": stmt.excluded.source,
                "series": stmt.excluded.series,
            },
        )
        session.execute(upsert_stmt)
        inserted_or_updated += len(chunk)

    session.commit()

    return {
        "processed": len(bars),
        "inserted_or_updated": inserted_or_updated,
        "rejected": rejected,
    }


class NSEHistoricalImporter:
    """
    Resumable historical bulk importer for NSE equities and indices.
    Manages raw file preservation, manifest checkpoints, validation, and batch loading.
    """

    def __init__(
        self,
        base_dir: Optional[Path] = None,
        manifest_file: Optional[Path] = None,
        eligible_series: Sequence[str] = DEFAULT_ELIGIBLE_EQUITY_SERIES,
        indices_to_track: Sequence[str] = DEFAULT_BROAD_INDICES,
    ):
        self.base_dir = base_dir or Path("data/nse")
        self.raw_equity_dir = self.base_dir / "raw" / "equities"
        self.raw_index_dir = self.base_dir / "raw" / "indices"
        self.manifest_path = manifest_file or (self.base_dir / "manifest.json")
        self.eligible_series = tuple(eligible_series)
        self.indices_to_track = tuple(indices_to_track)

        self.raw_equity_dir.mkdir(parents=True, exist_ok=True)
        self.raw_index_dir.mkdir(parents=True, exist_ok=True)
        self.manifest: Dict[str, Any] = self._load_manifest()

        try:
            import httpx
            self._client = httpx.Client(headers={"User-Agent": HTTP_USER_AGENT}, timeout=15.0)
        except Exception:
            self._client = None

    def close(self):
        if hasattr(self, "_client") and self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _load_manifest(self) -> Dict[str, Any]:
        if self.manifest_path.exists():
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Could not load manifest ({e}), starting new.")
        return {}

    def _save_manifest(self) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.manifest_path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(self.manifest, f, indent=2, default=str)
        for _ in range(5):
            try:
                temp_path.replace(self.manifest_path)
                break
            except PermissionError:
                time.sleep(0.05)

    def is_date_processed(self, target_date: date) -> bool:
        dt_str = target_date.isoformat()
        entry = self.manifest.get(dt_str)
        if entry and entry.get("status") in ("COMPLETED", "NON_TRADING_DAY"):
            return True
        return False

    def process_date(
        self,
        session: Session,
        target_date: date,
        force: bool = False,
    ) -> Dict[str, Any]:
        """
        Processes a single historical date:
        1. Checks manifest; skips if already completed unless force=True.
        2. Retrieves or downloads equity bhavcopy & index closing files.
        3. Identifies non-trading days gracefully on 404 without fabricating prices.
        4. Validates bars and bulk upserts into BFAMS-DeFI-test.
        5. Updates manifest checkpoint.
        """
        dt_str = target_date.isoformat()
        ddmmyyyy = target_date.strftime("%d%m%Y")
        yyyy = target_date.strftime("%Y")

        if not force and self.is_date_processed(target_date):
            return {
                "date": dt_str,
                "status": self.manifest[dt_str]["status"],
                "action": "SKIPPED_EXISTING",
                "equity_rows": self.manifest[dt_str].get("equity_rows_inserted", 0),
                "index_rows": self.manifest[dt_str].get("index_rows_inserted", 0),
            }

        # Subdirectories by year for clean file organization
        eq_year_dir = self.raw_equity_dir / yyyy
        idx_year_dir = self.raw_index_dir / yyyy
        eq_year_dir.mkdir(parents=True, exist_ok=True)
        idx_year_dir.mkdir(parents=True, exist_ok=True)

        eq_file_path = eq_year_dir / f"sec_bhavdata_full_{ddmmyyyy}.csv"
        idx_file_path = idx_year_dir / f"ind_close_all_{ddmmyyyy}.csv"

        # 1. Obtain Equity Bhavcopy
        equity_bytes = b""
        if eq_file_path.exists():
            with open(eq_file_path, "rb") as f:
                equity_bytes = f.read()
        else:
            url = EQUITY_BHAVCOPY_URL_TEMPLATE.format(ddmmyyyy=ddmmyyyy)
            status, equity_bytes = fetch_nse_url_with_retries(url, client=self._client)
            if status == 404:
                # Expected non-trading day (weekend or exchange holiday)
                self.manifest[dt_str] = {
                    "date": dt_str,
                    "status": "NON_TRADING_DAY",
                    "reason": "NSE returned 404 (weekend or holiday)",
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                }
                self._save_manifest()
                return {"date": dt_str, "status": "NON_TRADING_DAY", "action": "RECORDED_HOLIDAY"}

            with open(eq_file_path, "wb") as f:
                f.write(equity_bytes)

        # 2. Obtain Index closing file
        index_bytes = b""
        if idx_file_path.exists():
            with open(idx_file_path, "rb") as f:
                index_bytes = f.read()
        else:
            url = INDEX_DAILY_URL_TEMPLATE.format(ddmmyyyy=ddmmyyyy)
            status, index_bytes = fetch_nse_url_with_retries(url, client=self._client)
            if status == 200:
                with open(idx_file_path, "wb") as f:
                    f.write(index_bytes)

        # 3. Parse equity and index data
        eq_csv_text = equity_bytes.decode("utf-8", errors="replace")
        equity_bars = parse_equity_bhavcopy(
            eq_csv_text,
            eligible_series=self.eligible_series,
        )

        index_bars = []
        if index_bytes:
            idx_csv_text = index_bytes.decode("utf-8", errors="replace")
            index_bars = parse_index_daily(
                idx_csv_text,
                target_indices=self.indices_to_track,
            )

        all_bars = equity_bars + index_bars

        # 4. Bulk upsert into database
        db_res = upsert_security_daily_prices(session, all_bars)

        # 5. Checksum and manifest update
        eq_sha = _sha256_of_bytes(equity_bytes)
        idx_sha = _sha256_of_bytes(index_bytes) if index_bytes else None

        self.manifest[dt_str] = {
            "date": dt_str,
            "status": "COMPLETED",
            "equity_file": str(eq_file_path),
            "equity_sha256": eq_sha,
            "equity_rows_parsed": len(equity_bars),
            "equity_rows_inserted": len(equity_bars),
            "index_file": str(idx_file_path) if index_bytes else None,
            "index_sha256": idx_sha,
            "index_rows_parsed": len(index_bars),
            "index_rows_inserted": len(index_bars),
            "total_inserted": db_res["inserted_or_updated"],
            "rejected_bars": db_res["rejected"],
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        self._save_manifest()

        return {
            "date": dt_str,
            "status": "COMPLETED",
            "action": "IMPORTED",
            "equity_rows": len(equity_bars),
            "index_rows": len(index_bars),
            "total_inserted": db_res["inserted_or_updated"],
            "rejected": db_res["rejected"],
        }

    def import_bounded_batch(
        self,
        session: Session,
        start_date: date,
        end_date: date,
        max_trading_days: Optional[int] = None,
        pause_seconds: float = 0.1,
    ) -> Dict[str, Any]:
        """
        Processes a bounded batch of dates between start_date and end_date.
        Respects safety limits and pauses politely between downloads.
        """
        # Safety check: confirm connected database is BFAMS-DeFI-test
        db_name = session.execute(text("SELECT current_database()")).scalar()
        if db_name != "BFAMS-DeFI-test":
            raise RuntimeError(f"ABORT: Connected DB is '{db_name}', expected 'BFAMS-DeFI-test'")

        curr = start_date
        total_days_inspected = 0
        trading_days_imported = 0
        non_trading_days = 0
        skipped_existing = 0
        total_equity_rows = 0
        total_index_rows = 0
        failed_dates = []

        first_imported_date = None
        last_imported_date = None

        while curr <= end_date:
            if max_trading_days and trading_days_imported >= max_trading_days:
                break

            total_days_inspected += 1
            try:
                res = self.process_date(session, curr)
                action = res.get("action")
                status = res.get("status")

                if action == "SKIPPED_EXISTING":
                    skipped_existing += 1
                elif status == "NON_TRADING_DAY":
                    non_trading_days += 1
                elif status == "COMPLETED":
                    trading_days_imported += 1
                    total_equity_rows += res.get("equity_rows", 0)
                    total_index_rows += res.get("index_rows", 0)
                    if first_imported_date is None:
                        first_imported_date = curr
                    last_imported_date = curr
                    if trading_days_imported % 10 == 0:
                        logger.info(
                            f"[{curr}] Progress: {trading_days_imported} trading days imported "
                            f"(+{total_equity_rows} equities, +{total_index_rows} indices)"
                        )

                # Politeness pause for new external downloads
                if action not in ("SKIPPED_EXISTING",):
                    time.sleep(pause_seconds)

            except Exception as e:
                logger.error(f"Failed to process {curr}: {e}")
                failed_dates.append({"date": curr.isoformat(), "error": str(e)})

            if total_days_inspected % 50 == 0:
                logger.info(
                    f"Date scan: {curr} | Inspected: {total_days_inspected} | "
                    f"Imported: {trading_days_imported} | Skipped: {skipped_existing} | Non-trading: {non_trading_days}"
                )

            curr += timedelta(days=1)

        return {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "days_inspected": total_days_inspected,
            "trading_days_imported": trading_days_imported,
            "non_trading_days": non_trading_days,
            "skipped_existing": skipped_existing,
            "total_equity_rows": total_equity_rows,
            "total_index_rows": total_index_rows,
            "failed_count": len(failed_dates),
            "failed_dates": failed_dates,
            "first_imported_date": first_imported_date.isoformat() if first_imported_date else None,
            "last_imported_date": last_imported_date.isoformat() if last_imported_date else None,
        }
