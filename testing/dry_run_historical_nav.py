import time
from collections import Counter
from datetime import date
from pathlib import Path

from app.nav_parser import parse_nav_lines

PATH = Path("data/amfi/history/NAVHistory_2021-10-14_2022-01-01.txt")

if not PATH.exists():
    print(f"Error: {PATH} not found")
    raise SystemExit(1)

start_t = time.perf_counter()

with open(PATH, "r", encoding="utf-8") as f:
    schemes, navs, rejected, stats = parse_nav_lines(f)

duration = time.perf_counter() - start_t

print("=" * 60)
print("AMFI HISTORICAL NAV DRY RUN RESULTS")
print("=" * 60)
print(f"File: {PATH}")
print(f"Parsing time: {duration:.2f} seconds")
print(f"Total schemes extracted: {len(schemes):,}")
print(f"Total valid NAV records: {len(navs):,}")
print(f"Total rejected rows: {len(rejected):,}")
print(f"Parser stats: {dict(stats)}")

dates = [n["nav_date"] for n in navs]
distinct_dates = sorted(set(dates))
print(f"Distinct NAV dates: {len(distinct_dates)}")
print(f"Earliest NAV date: {distinct_dates[0]}")
print(f"Latest NAV date: {distinct_dates[-1]}")

distinct_amcs = {s["amc_name"] for s in schemes}
distinct_categories = {s["scheme_category"] for s in schemes}
print(f"Distinct fund houses (AMCs): {len(distinct_amcs)}")
print(f"Distinct SEBI categories: {len(distinct_categories)}")

# Check field mapping integrity across all parsed schemes
bad_name_count = sum(1 for s in schemes if s["scheme_name"] in ("Growth", "IDCW", "Regular Plan", "Direct Plan"))
bad_plan_count = sum(1 for s in schemes if s["plan"] and s["plan"].startswith("INF"))
print(f"Schemes with suspicious swapped names ('Growth'/'IDCW'): {bad_name_count}")
print(f"Schemes with suspicious swapped plans (starting with 'INF'): {bad_plan_count}")

# Distribution of observations per scheme
obs_per_scheme = Counter(n["scheme_code"] for n in navs)
vals = list(obs_per_scheme.values())
print(f"Min observations per scheme: {min(vals)}")
print(f"Max observations per scheme: {max(vals)}")
print(f"Schemes with only 1 observation: {sum(1 for v in vals if v == 1)}")

print("\n--- REPRESENTATIVE SAMPLE RECORDS ---")
by_code = {s["scheme_code"]: s for s in schemes}

samples_to_show = ["100033", "145488", "100034"]
for code in samples_to_show:
    if code in by_code:
        s = by_code[code]
        scheme_navs = [n for n in navs if n["scheme_code"] == code]
        print(f"\nScheme Code: {code}")
        print(f"  Name: {s['scheme_name']}")
        print(f"  AMC: {s['amc_name']}")
        print(f"  Category: {s['scheme_category']}")
        print(f"  Plan: {s['plan']} | Option: {s['option_name']}")
        print(f"  ISIN Primary: {s['isin_primary']} | Reinvestment: {s['isin_reinvestment']}")
        print(f"  Total NAV observations in window: {len(scheme_navs)}")
        if scheme_navs:
            print(f"  Earliest observation: {scheme_navs[0]['nav_date']} -> NAV: {scheme_navs[0]['nav']}")
            print(f"  Latest observation: {scheme_navs[-1]['nav_date']} -> NAV: {scheme_navs[-1]['nav']}")

if rejected:
    print(f"\nFirst 5 rejections: {rejected[:5]}")
else:
    print("\nRejections: None (0 rows rejected)")
