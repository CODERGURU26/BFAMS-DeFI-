from collections import Counter

from app.nav_parser import parse_nav_lines

with open("data/amfi/NAVAll.txt", encoding="utf-8") as f:
    schemes, navs, rejected, stats = parse_nav_lines(f)

decimals = Counter(-n["nav"].as_tuple().exponent for n in navs)
print("decimal places -> count:", dict(sorted(decimals.items())))
print("max decimals:", max(decimals))

long_ones = [n for n in navs if -n["nav"].as_tuple().exponent > 6]
print("examples:")
for n in long_ones[:10]:
    print(n["scheme_code"], n["nav"])

amcs = {s["amc_name"] for s in schemes}
print("fund houses:", len(amcs), "| after lowercasing:", len({a.lower() for a in amcs}))