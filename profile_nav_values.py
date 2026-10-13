from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re

PATH = "data/amfi/NAVAll.txt"
CATEGORY_PREFIXES = ("Open Ended", "Close Ended", "Interval")

rows, headings = [], []
with open(PATH, encoding="utf-8") as f:
    for line in f:
        s = line.strip()
        if s.count(";") == 7:
            rows.append([p.strip() for p in s.split(";")])
        elif s and ";" not in s and not s.startswith(CATEGORY_PREFIXES):
            headings.append(s)

header, data = rows[0], rows[1:]
print("header:", header)
print("data rows:", len(data))

codes = Counter(r[0] for r in data)
print("non-numeric scheme codes:", [r[0] for r in data if not r[0].isdigit()][:10])
print("scheme codes appearing more than once:", sum(1 for c in codes.values() if c > 1))

zero, bad_nav = 0, Counter()
for r in data:
    try:
        if Decimal(r[6]) == 0:
            zero += 1
    except InvalidOperation:
        bad_nav[r[6]] += 1
print("zero NAV rows:", zero)
print("non-numeric NAV values:", dict(bad_nav.most_common(10)))

dates, bad_date = Counter(), Counter()
for r in data:
    try:
        dates[datetime.strptime(r[7], "%d-%b-%Y").date()] += 1
    except ValueError:
        bad_date[r[7]] += 1
latest = max(dates)
print("unparseable dates:", dict(bad_date.most_common(10)))
print("latest date:", latest, "| rows on latest date:", dates[latest])
print("rows older than 30 days:", sum(c for d, c in dates.items() if (latest - d).days > 30))

isin = re.compile(r"^INF[0-9A-Z]{9}$")
print("rows with both ISINs missing:", sum(1 for r in data if r[1] == "-" and r[2] == "-"))
print("ISINs with unexpected format:",
      sum(1 for r in data for v in (r[1], r[2]) if v != "-" and not isin.match(v)))
print("empty scheme names:", sum(1 for r in data if not r[3]))
print("names containing a double space:", sum(1 for r in data if "  " in r[3]))

odd_headings = [h for h in headings if not h.endswith("Mutual Fund")]
print("fund-house lines not ending in 'Mutual Fund':", len(odd_headings), odd_headings[:10])