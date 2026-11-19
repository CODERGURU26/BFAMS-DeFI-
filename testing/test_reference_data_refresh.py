from datetime import date

import pytest

from scripts.refresh_reference_data import (
    freshness_warning,
    nse_date_window,
    parse_args,
    parse_amfi_response,
)


def test_nse_date_window_includes_lookback_days():
    assert nse_date_window(date(2026, 11, 19), 7) == (
        date(2026, 11, 13),
        date(2026, 11, 19),
    )


def test_nse_date_window_rejects_nonpositive_lookback():
    with pytest.raises(ValueError):
        nse_date_window(date(2026, 11, 19), 0)


def test_refresh_target_defaults_to_test_and_can_select_staging():
    assert parse_args([]).target == "test"
    assert parse_args(["--target", "main"]).target == "main"


def test_freshness_warning_only_reports_data_past_threshold():
    assert freshness_warning(
        "NSE prices",
        date(2026, 11, 16),
        date(2026, 11, 19),
        3,
    ) is None
    assert "stale" in freshness_warning(
        "NSE prices",
        date(2026, 11, 15),
        date(2026, 11, 19),
        3,
    )
    assert "has no data" in freshness_warning(
        "AMFI NAV",
        None,
        date(2026, 11, 19),
        4,
    )


def test_amfi_response_requires_valid_nav_data():
    content = b"""\
Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date
Open Ended Schemes(Equity Scheme - Large Cap Fund)
Example Mutual Fund
100001;INF000000001;-;Example Fund;Direct Plan;Growth;10.2500;18-Nov-2026
"""
    schemes, navs, rejected, stats, latest_date = parse_amfi_response(
        content,
        date(2026, 11, 19),
    )

    assert len(schemes) == 1
    assert len(navs) == 1
    assert rejected == []
    assert latest_date == date(2026, 11, 18)


def test_amfi_response_rejects_html():
    with pytest.raises(ValueError, match="HTML"):
        parse_amfi_response(b"<html>Unavailable</html>", date(2026, 11, 19))
