import re
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation

HISTORY = "data/amfi/history/NAVHistory_2021-10-14_2022-01-01.txt"
DAILY = "data/amfi/NAVAll.txt"
EXPECTED_HEADER = (
    "Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;"
    "ISIN Div Reinvestment;Net Asset Value;Date"
)
PREFIXES = ("Open Ended", "Close Ended", "Interval")
ISIN_RE = re.compile(r"^INF[0-9A-Z]{9}$")
MONTHS = {
    m: i
    for i, m in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        1,
    )
}

daily_codes = set()
with open(DAILY, encoding="utf-8") as f:
    for line in f:
        s = line.strip()
        if s.count(";") == 7 and not s.startswith("Scheme Code"):
            daily_codes.add(s.split(";")[0].strip())

counts = Counter()
categories = Counter()
bad_nav, bad_date, bad_isin = Counter(), Counter(), Counter()
odd, codes, pairs, dates = [], set(), set(), Counter()
dup_pairs, max_decimals, header = 0, 0, None

with open(HISTORY, encoding="utf-8") as f:
    for n, line in enumerate(f, 1):
        s = line.strip()
        if not s:
            counts["blank"] += 1
            continue
        if header is None:
            header = s
            continue
        k = s.count(";")
        if k == 0:
            if s.startswith(PREFIXES):
                counts["category line"] += 1
                categories[s] += 1
            else:
                counts["fund house line"] += 1
            continue
        if k != 7:
            counts["other"] += 1
            odd.append((n, s[:120]))
            continue

        counts["data row"] += 1
        code, name, plan, option, isin1, isin2, nav_text, date_text = [
            p.strip() for p in s.split(";")
        ]
        codes.add(code)
        for v in (isin1, isin2):
            if v not in ("", "-") and not ISIN_RE.match(v):
                bad_isin[v] += 1
        try:
            nav = Decimal(nav_text)
            if not nav.is_finite():
                raise InvalidOperation
            if nav <= 0:
                counts["nav <= 0"] += 1
            max_decimals = max(max_decimals, -nav.as_tuple().exponent)
        except InvalidOperation:
            bad_nav[nav_text] += 1
        try:
            day, mon, year = date_text.split("-")
            d = date(int(year), MONTHS[mon.capitalize()], int(day))
        except (ValueError, KeyError):
            bad_date[date_text] += 1
            continue
        dates[d] += 1
        if (code, d) in pairs:
            dup_pairs += 1
        pairs.add((code, d))

print("header matches expected:", header == EXPECTED_HEADER)
if header != EXPECTED_HEADER:
    print("header found:", header)
print(dict(counts))
print("distinct categories:", len(categories))
print(*list(categories)[:15], sep="\n")
print("distinct scheme codes:", len(codes))
missing = sorted(codes - daily_codes)
print("scheme codes not in today's file:", len(missing), missing[:10])
print("date range:", min(dates), "to", max(dates), "| distinct dates:", len(dates))
print("duplicate (scheme, date) pairs:", dup_pairs)
print("max NAV decimals:", max_decimals)
print("bad NAV values:", sum(bad_nav.values()), dict(bad_nav.most_common(10)))
print("bad dates:", sum(bad_date.values()), dict(bad_date.most_common(10)))
print("bad ISIN values:", sum(bad_isin.values()), dict(bad_isin.most_common(10)))
print("odd lines (first 10):", odd[:10])