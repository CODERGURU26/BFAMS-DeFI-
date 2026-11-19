import uuid
from decimal import Decimal

from app.models import PortfolioAllocationTarget
from app.routes.testing_dashboard import _allocation_target_summary


def test_allocation_target_summary_calculates_percentage_point_drift():
    account_id = uuid.uuid4()
    targets = [
        PortfolioAllocationTarget(
            account_id=account_id,
            asset_type="EQUITY",
            target_percent=Decimal("50"),
        ),
        PortfolioAllocationTarget(
            account_id=account_id,
            asset_type="MUTUAL_FUND",
            target_percent=Decimal("40"),
        ),
        PortfolioAllocationTarget(
            account_id=account_id,
            asset_type="ETF",
            target_percent=Decimal("10"),
        ),
    ]

    summary = _allocation_target_summary(
        [
            {"name": "EQUITY", "value": "600", "share": "60"},
            {"name": "MUTUAL_FUND", "value": "400", "share": "40"},
        ],
        {"EQUITY", "MUTUAL_FUND", "ETF"},
        targets,
        unpriced_count=2,
    )

    assert summary["configured"] is True
    assert summary["target_total_percent"] == "100"
    assert summary["unpriced_holdings"] == 2
    assert summary["items"] == [
        {
            "asset_type": "EQUITY",
            "actual_percent": "60",
            "target_percent": "50",
            "drift_percentage_points": "10",
        },
        {
            "asset_type": "ETF",
            "actual_percent": "0",
            "target_percent": "10",
            "drift_percentage_points": "-10",
        },
        {
            "asset_type": "MUTUAL_FUND",
            "actual_percent": "40",
            "target_percent": "40",
            "drift_percentage_points": "0",
        },
    ]


def test_allocation_target_summary_does_not_show_drift_before_targets_are_set():
    summary = _allocation_target_summary(
        [{"name": "EQUITY", "value": "100", "share": "100"}],
        {"EQUITY"},
        [],
        unpriced_count=0,
    )

    assert summary["configured"] is False
    assert summary["items"][0]["target_percent"] is None
    assert summary["items"][0]["drift_percentage_points"] is None
