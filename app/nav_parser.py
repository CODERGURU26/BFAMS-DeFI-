import itertools
import re
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation

CATEGORY_PREFIXES = {
    "Open Ended": "open_ended",
    "Close Ended": "close_ended",
    "Interval": "interval",
}
ISIN_RE = re.compile(r"^INF[0-9A-Z]{9}$")
MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ],
        1,
    )
}


def _clean(text: str) -> str:
    return " ".join(text.split())


def _isin(value: str, stats: Counter) -> str | None:
    if value in ("-", ""):
        return None
    if ISIN_RE.match(value):
        return value
    stats["isin_cleaned"] += 1
    return None


def _parse_date(text: str) -> date:
    day, mon, year = text.split("-")
    return date(int(year), MONTHS[mon.capitalize()], int(day))


def detect_amfi_format(header_line: str) -> str:
    """Detect whether an AMFI report header is in 'daily' or 'historical' format."""
    parts = [p.strip().lower() for p in header_line.split(";")]
    if len(parts) >= 8 and parts[0] == "scheme code":
        if parts[1].startswith("isin"):
            return "daily"
        if parts[1] in ("nav name", "scheme name") and (
            parts[4].startswith("isin") or parts[2] == "plan"
        ):
            return "historical"
    raise ValueError(f"Unrecognized AMFI header format: {header_line.strip()!r}")


def parse_daily_nav_lines(lines):
    stats = Counter()
    schemes, navs, rejected = [], [], []
    seen = set()
    category = scheme_type = amc = None

    def reject(n, reason):
        rejected.append({"line": n, "reason": reason})

    for n, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s or s.startswith("Scheme Code"):
            continue

        count = s.count(";")
        if count == 0:
            prefix = next((p for p in CATEGORY_PREFIXES if s.startswith(p)), None)
            if prefix:
                category, scheme_type = s, CATEGORY_PREFIXES[prefix]
            else:
                amc = s
            continue
        if count != 7:
            reject(n, "unexpected number of fields")
            continue

        # Daily format:
        # Scheme Code;ISIN Growth;ISIN Reinv;Scheme Name;Plan;Option;NAV;Date
        code, isin1, isin2, name, plan, option, nav_text, date_text = [
            p.strip() for p in s.split(";")
        ]

        if category is None or amc is None:
            reject(n, "row before category or fund house")
            continue
        if not code.isdigit():
            reject(n, "non-numeric scheme code")
            continue
        if code in seen:
            reject(n, "duplicate scheme code")
            continue
        clean_name = _clean(name)
        if not clean_name:
            reject(n, "empty scheme name")
            continue
        try:
            nav = Decimal(nav_text)
        except InvalidOperation:
            reject(n, "invalid NAV")
            continue
        if not nav.is_finite():
            reject(n, "invalid NAV")
            continue
        try:
            nav_date = _parse_date(date_text)
        except (ValueError, KeyError, IndexError):
            reject(n, "invalid date")
            continue

        seen.add(code)
        if clean_name != name:
            stats["name_whitespace_cleaned"] += 1
        schemes.append({
            "scheme_code": code,
            "isin_primary": _isin(isin1, stats),
            "isin_reinvestment": _isin(isin2, stats),
            "scheme_name": clean_name,
            "plan": _clean(plan) or None,
            "option_name": _clean(option) or None,
            "amc_name": amc,
            "scheme_type": scheme_type,
            "scheme_category": category,
        })

        if nav <= 0:
            stats["nonpositive_nav_skipped"] += 1
        else:
            if nav.as_tuple().exponent < -6:
                stats["nav_over_6_decimals"] += 1
            navs.append({"scheme_code": code, "nav_date": nav_date, "nav": nav})

    return schemes, navs, rejected, stats


def parse_historical_nav_lines(lines):
    stats = Counter()
    schemes_by_code = {}
    navs, rejected = [], []
    seen_pairs = set()
    category = scheme_type = amc = None

    def reject(n, reason):
        rejected.append({"line": n, "reason": reason})

    for n, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s or s.startswith("Scheme Code"):
            continue

        count = s.count(";")
        if count == 0:
            prefix = next((p for p in CATEGORY_PREFIXES if s.startswith(p)), None)
            if prefix:
                category, scheme_type = s, CATEGORY_PREFIXES[prefix]
            else:
                amc = s
            continue
        if count != 7:
            reject(n, "unexpected number of fields")
            continue

        # Historical report format:
        # Scheme Code;NAV Name;Plan;Option;ISIN Growth;ISIN Reinv;NAV;Date
        code, name, plan, option, isin1, isin2, nav_text, date_text = [
            p.strip() for p in s.split(";")
        ]

        if category is None or amc is None:
            reject(n, "row before category or fund house")
            continue
        if not code.isdigit():
            reject(n, "non-numeric scheme code")
            continue

        clean_name = _clean(name)
        if not clean_name:
            reject(n, "empty scheme name")
            continue

        try:
            nav = Decimal(nav_text)
        except InvalidOperation:
            reject(n, "invalid NAV")
            continue
        if not nav.is_finite():
            reject(n, "invalid NAV")
            continue

        try:
            nav_date = _parse_date(date_text)
        except (ValueError, KeyError, IndexError):
            reject(n, "invalid date")
            continue

        pair = (code, nav_date)
        if pair in seen_pairs:
            reject(n, "duplicate (scheme, date) pair")
            continue
        seen_pairs.add(pair)

        if code not in schemes_by_code:
            if clean_name != name:
                stats["name_whitespace_cleaned"] += 1
            schemes_by_code[code] = {
                "scheme_code": code,
                "isin_primary": _isin(isin1, stats),
                "isin_reinvestment": _isin(isin2, stats),
                "scheme_name": clean_name,
                "plan": _clean(plan) or None,
                "option_name": _clean(option) or None,
                "amc_name": amc,
                "scheme_type": scheme_type,
                "scheme_category": category,
            }

        if nav <= 0:
            stats["nonpositive_nav_skipped"] += 1
        else:
            if nav.as_tuple().exponent < -6:
                stats["nav_over_6_decimals"] += 1
            navs.append({"scheme_code": code, "nav_date": nav_date, "nav": nav})

    schemes = list(schemes_by_code.values())
    return schemes, navs, rejected, stats


def parse_nav_lines(lines, format=None):
    if format == "daily":
        return parse_daily_nav_lines(lines)
    if format == "historical":
        return parse_historical_nav_lines(lines)
    if format is not None:
        raise ValueError(f"Unknown format: {format!r}. Must be 'daily', 'historical', or None.")

    # Auto-detect format by finding the first non-empty line
    lines_iter = iter(lines)
    leading_lines = []
    first_non_empty = None
    for line in lines_iter:
        leading_lines.append(line)
        if line.strip():
            first_non_empty = line
            break

    if first_non_empty is None:
        return [], [], [], Counter()

    detected_format = detect_amfi_format(first_non_empty)
    stream = itertools.chain(leading_lines, lines_iter)

    if detected_format == "daily":
        return parse_daily_nav_lines(stream)
    else:
        return parse_historical_nav_lines(stream)