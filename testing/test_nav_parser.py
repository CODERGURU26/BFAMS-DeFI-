from datetime import date
from decimal import Decimal

from app.nav_parser import parse_nav_lines

lines = [
    "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date",
    " ",
    "135762;INF846K01WO1;-;Axis Children's Fund;Direct Plan;Growth Option;28.9937;12-Oct-2026",
    "Open Ended Schemes(Debt Scheme - Gilt Fund)",
    "Bandhan Mutual Fund",
    "108557;INF194K01EH8;INF194K01EG0;Bandhan Gilt    Fund;Regular Plan;Quarterly IDCW;11.9734;12-Oct-2026",
    "129287;Redeemed;-;Some Redeemed Fund;;;10.5;01-Aug-2016",
    "148304;INF090I01VZ8;-;Franklin Credit Risk;Direct Plan;Growth;0.0000;12-Oct-2026",
    "148305;-;-;Bad Nav Fund;;;N.A.;12-Oct-2026",
    "100001;-;-;Short;Row;5",
    "108557;INF194K01EH8;-;Duplicate Code;;;1;12-Oct-2026",
    "Close Ended Schemes(Income)",
    "Some Fund House",
    "200001;-;-;FTP Series 1;;;12.3456;05-Jan-2020",
]

schemes, navs, rejected, stats = parse_nav_lines(lines)
by_code = {s["scheme_code"]: s for s in schemes}

assert len(schemes) == 4, len(schemes)
assert len(navs) == 3, len(navs)
assert sorted(r["line"] for r in rejected) == [3, 9, 10, 11], rejected

assert by_code["108557"]["scheme_name"] == "Bandhan Gilt Fund"
assert by_code["108557"]["scheme_type"] == "open_ended"
assert by_code["108557"]["amc_name"] == "Bandhan Mutual Fund"
assert by_code["129287"]["isin_primary"] is None
assert by_code["129287"]["plan"] is None
assert by_code["200001"]["scheme_type"] == "close_ended"
assert by_code["200001"]["amc_name"] == "Some Fund House"

nav_codes = {n["scheme_code"] for n in navs}
assert "148304" not in nav_codes
first = next(n for n in navs if n["scheme_code"] == "108557")
assert first["nav"] == Decimal("11.9734")
assert first["nav_date"] == date(2026, 10, 12)

assert stats["nonpositive_nav_skipped"] == 1
assert stats["isin_cleaned"] == 1
assert stats["name_whitespace_cleaned"] == 1

print("schemes:", len(schemes), "navs:", len(navs), "rejected:", len(rejected))
print(dict(stats))
print("all parser checks passed")