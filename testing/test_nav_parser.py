from datetime import date
from decimal import Decimal

from app.nav_parser import (
    detect_amfi_format,
    parse_daily_nav_lines,
    parse_historical_nav_lines,
    parse_nav_lines,
)


def test_daily_format_parsing():
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
        "200001;-;-;FTP Series 1;;;12.34567891;05-Jan-2020",
        "200002;-;-;Bad Date Fund;Direct;Growth;15.0000;32-Oct-2026",
    ]

    schemes, navs, rejected, stats = parse_nav_lines(lines)
    by_code = {s["scheme_code"]: s for s in schemes}

    # Schemes registered (including zero NAV scheme)
    assert len(schemes) == 4, f"Expected 4 schemes, got {len(schemes)}"
    # Valid positive NAVs
    assert len(navs) == 3, f"Expected 3 navs, got {len(navs)}"
    # Rejected lines: 3 (before cat/AMC), 9 (N.A. NAV), 10 (short row), 11 (duplicate code), 15 (bad date)
    assert sorted(r["line"] for r in rejected) == [3, 9, 10, 11, 15], rejected

    # Scheme metadata & whitespace check
    assert by_code["108557"]["scheme_name"] == "Bandhan Gilt Fund"
    assert by_code["108557"]["scheme_type"] == "open_ended"
    assert by_code["108557"]["amc_name"] == "Bandhan Mutual Fund"
    assert by_code["108557"]["isin_primary"] == "INF194K01EH8"
    assert by_code["108557"]["isin_reinvestment"] == "INF194K01EG0"

    # Blank plan and option, odd ISIN cleaned to None
    assert by_code["129287"]["isin_primary"] is None
    assert by_code["129287"]["plan"] is None
    assert by_code["129287"]["option_name"] is None

    # Zero NAV fund retained in schemes but omitted from navs
    assert "148304" in by_code
    assert "148304" not in {n["scheme_code"] for n in navs}

    # 8-decimal precision preservation
    high_prec = next(n for n in navs if n["scheme_code"] == "200001")
    assert high_prec["nav"] == Decimal("12.34567891")
    assert high_prec["nav_date"] == date(2020, 1, 5)

    assert stats["nonpositive_nav_skipped"] == 1
    assert stats["isin_cleaned"] == 1
    assert stats["name_whitespace_cleaned"] == 1
    assert stats["nav_over_6_decimals"] == 1


def test_historical_format_parsing():
    lines = [
        "Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Net Asset Value;Date",
        " ",
        "Open Ended Schemes ( Equity Scheme - Large Cap Fund )",
        "Aditya Birla Sun Life Mutual Fund",
        "100033;Aditya Birla Sun Life Frontline Equity Fund;Regular Plan;Growth;INF209K01157;-;305.1200;14-Oct-2021",
        "100033;Aditya Birla Sun Life Frontline Equity Fund;Regular Plan;Growth;INF209K01157;-;306.4500;15-Oct-2021",
        "100034;Aditya Birla Sun Life Frontline Equity Fund;Regular Plan;IDCW;INF209K01165;INF209K01173;35.12345678;14-Oct-2021",
        "100035;Some Legacy Fund;;;;;10.5000;14-Oct-2021",
        "100036;Zero NAV Fund;Direct;Growth;INF209K01181;-;0.0000;14-Oct-2021",
        "100033;Aditya Birla Sun Life Frontline Equity Fund;Regular Plan;Growth;INF209K01157;-;305.1200;14-Oct-2021",
        "100037;Bad NAV Scheme;Direct;Growth;INF209K01199;-;INVALID;14-Oct-2021",
        "100038;Bad Date Scheme;Direct;Growth;INF209K01207;-;25.0000;99-XYZ-2021",
        "100039;Short Row;Direct;Growth;5",
    ]

    schemes, navs, rejected, stats = parse_nav_lines(lines)
    by_code = {s["scheme_code"]: s for s in schemes}

    # Schemes: 100033, 100034, 100035, 100036 (zero nav is kept in schemes)
    assert len(schemes) == 4, f"Expected 4 schemes, got {len(schemes)}"
    # NAVs: 100033 on 14-Oct, 100033 on 15-Oct, 100034 on 14-Oct, 100035 on 14-Oct = 4 NAV rows
    assert len(navs) == 4, f"Expected 4 navs, got {len(navs)}"

    # Rejected lines: 10 (duplicate pair), 11 (invalid nav), 12 (invalid date), 13 (unexpected fields)
    assert sorted(r["line"] for r in rejected) == [10, 11, 12, 13], rejected

    # Verify historical field mapping:
    # Scheme Name is in field 1, Plan in field 2, Option in field 3, ISINs in fields 4 & 5
    s33 = by_code["100033"]
    assert s33["scheme_name"] == "Aditya Birla Sun Life Frontline Equity Fund"
    assert s33["plan"] == "Regular Plan"
    assert s33["option_name"] == "Growth"
    assert s33["isin_primary"] == "INF209K01157"
    assert s33["isin_reinvestment"] is None
    assert s33["amc_name"] == "Aditya Birla Sun Life Mutual Fund"
    assert s33["scheme_type"] == "open_ended"

    # Both ISINs and 8-decimal NAV
    s34 = by_code["100034"]
    assert s34["isin_primary"] == "INF209K01165"
    assert s34["isin_reinvestment"] == "INF209K01173"
    nav34 = next(n for n in navs if n["scheme_code"] == "100034")
    assert nav34["nav"] == Decimal("35.12345678")

    # Blank plan and option
    s35 = by_code["100035"]
    assert s35["plan"] is None
    assert s35["option_name"] is None
    assert s35["isin_primary"] is None

    # Multi-date NAV observations for scheme 100033
    navs_33 = [n for n in navs if n["scheme_code"] == "100033"]
    assert len(navs_33) == 2
    assert {n["nav_date"] for n in navs_33} == {date(2021, 10, 14), date(2021, 10, 15)}
    assert {n["nav"] for n in navs_33} == {Decimal("305.1200"), Decimal("306.4500")}

    # Zero NAV skipped
    assert "100036" in by_code
    assert "100036" not in {n["scheme_code"] for n in navs}
    assert stats["nonpositive_nav_skipped"] == 1
    assert stats["nav_over_6_decimals"] == 1


def test_format_detection_and_routing():
    h_daily = "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date"
    h_hist = "Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;Net Asset Value;Date"

    assert detect_amfi_format(h_daily) == "daily"
    assert detect_amfi_format(h_hist) == "historical"

    # Invalid header raises ValueError
    try:
        detect_amfi_format("Invalid;Header;Format;With;Eight;Fields;Here;Now")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass

    # Empty lines return empty lists without error
    s, n, r, st = parse_nav_lines([])
    assert s == [] and n == [] and r == []

    s, n, r, st = parse_nav_lines(["   \n", "\n"])
    assert s == [] and n == [] and r == []


if __name__ == "__main__":
    test_daily_format_parsing()
    test_historical_format_parsing()
    test_format_detection_and_routing()
    print("ALL TESTS PASSED SUCCESSFULLY!")