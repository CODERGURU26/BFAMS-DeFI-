from collections import Counter

from app.nav_parser import parse_nav_lines

PATH = "data/amfi/NAVAll.txt"

with open(PATH, encoding="utf-8") as f:
    schemes, navs, rejected, stats = parse_nav_lines(f)

print("schemes:", len(schemes))
print("navs:", len(navs))
print("rejected:", len(rejected))
print("stats:", dict(stats))
print("distinct categories:", len({s["scheme_category"] for s in schemes}))
print("distinct fund houses:", len({s["amc_name"] for s in schemes}))
print("by type:", dict(Counter(s["scheme_type"] for s in schemes)))

latest = max(n["nav_date"] for n in navs)
print("latest nav date:", latest, "| prices on it:",
      sum(1 for n in navs if n["nav_date"] == latest))
print("schemes with both ISINs NULL:",
      sum(1 for s in schemes if not s["isin_primary"] and not s["isin_reinvestment"]))
print("first rejected:", rejected[:10])