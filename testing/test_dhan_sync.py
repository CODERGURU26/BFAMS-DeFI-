import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import httpx

from app.integrations.dhan.client import (
    DHAN_HOLDINGS_URL,
    DhanAPIError,
    DhanClient,
    DhanConfigurationError,
)
from app.integrations.dhan.holdings import (
    DhanHoldingsValidationError,
    build_holding_rows,
)
from app.models import InvestmentHolding


class TestDhanClient(unittest.TestCase):
    def test_uses_only_the_holdings_get_with_required_headers(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=[])

        with DhanClient(
            "test-client",
            "test-access-token",
            transport=httpx.MockTransport(handler),
        ) as client:
            self.assertEqual(client.get_holdings(), [])

        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(str(request.url), DHAN_HOLDINGS_URL)
        self.assertEqual(request.headers["client-id"], "test-client")
        self.assertEqual(request.headers["access-token"], "test-access-token")
        self.assertEqual(request.content, b"")

    def test_does_not_follow_redirects(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={"Location": DHAN_HOLDINGS_URL})

        with DhanClient(
            "test-client",
            "test-access-token",
            transport=httpx.MockTransport(handler),
        ) as client:
            with self.assertRaises(DhanAPIError) as raised:
                client.get_holdings()

        self.assertEqual(raised.exception.status_code, 302)
        self.assertEqual(len(requests), 1)

    def test_sanitizes_http_errors(self):
        def handler(request):
            return httpx.Response(401, text="private response body")

        with DhanClient(
            "private-client",
            "private-token",
            transport=httpx.MockTransport(handler),
        ) as client:
            with self.assertRaises(DhanAPIError) as raised:
                client.get_holdings()

        self.assertEqual(raised.exception.status_code, 401)
        self.assertNotIn("private", str(raised.exception))

    def test_rejects_missing_credentials(self):
        with self.assertRaises(DhanConfigurationError):
            DhanClient("", "test-token")

    def test_rejects_non_list_response(self):
        def handler(request):
            return httpx.Response(200, json={"holdings": []})

        with DhanClient(
            "test-client",
            "test-access-token",
            transport=httpx.MockTransport(handler),
        ) as client:
            with self.assertRaises(DhanAPIError):
                client.get_holdings()


class TestDhanHoldingMapping(unittest.TestCase):
    def setUp(self):
        self.observed_at = datetime(2026, 10, 21, tzinfo=timezone.utc)

    def test_reuses_existing_metadata_and_does_not_invent_prices(self):
        existing = InvestmentHolding(
            account_id="account-id",
            asset_name="Known Security",
            asset_identifier="INE509F01029",
            asset_type="EQUITY",
            quantity=Decimal("1"),
            unit_price=Decimal("10"),
            market_value=Decimal("10"),
            currency="INR",
            observed_at=self.observed_at,
        )
        holdings = build_holding_rows(
            [
                {
                    "tradingSymbol": "KNOWN",
                    "isin": "INE509F01029",
                    "totalQty": "12.5",
                }
            ],
            "account-id",
            self.observed_at,
            {"INE509F01029": existing},
        )

        self.assertEqual(len(holdings), 1)
        self.assertEqual(holdings[0].asset_name, "Known Security")
        self.assertEqual(holdings[0].quantity, Decimal("12.5"))
        self.assertIsNone(holdings[0].unit_price)
        self.assertIsNone(holdings[0].market_value)

    def test_infers_metadata_when_isin_is_not_known(self):
        holdings = build_holding_rows(
            [
                {
                    "tradingSymbol": "NIFTYBEES",
                    "isin": "INF204KB14I2",
                    "totalQty": 3,
                }
            ],
            "account-id",
            self.observed_at,
            {},
        )

        self.assertEqual(holdings[0].asset_name, "NIFTYBEES")
        self.assertEqual(holdings[0].asset_type, "ETF")
        self.assertEqual(holdings[0].asset_identifier, "INF204KB14I2")

    def test_rejects_duplicate_isin(self):
        payload = [
            {
                "tradingSymbol": "FIRST",
                "isin": "INE509F01029",
                "totalQty": 1,
            },
            {
                "tradingSymbol": "SECOND",
                "isin": "INE509F01029",
                "totalQty": 2,
            },
        ]
        with self.assertRaises(DhanHoldingsValidationError):
            build_holding_rows(payload, "account-id", self.observed_at, {})

    def test_rejects_invalid_or_negative_quantity(self):
        for quantity in ("not-a-number", "-1"):
            with self.subTest(quantity=quantity):
                with self.assertRaises(DhanHoldingsValidationError):
                    build_holding_rows(
                        [
                            {
                                "tradingSymbol": "KNOWN",
                                "isin": None,
                                "totalQty": quantity,
                            }
                        ],
                        "account-id",
                        self.observed_at,
                        {},
                    )

    def test_requires_aware_timestamp(self):
        with self.assertRaises(DhanHoldingsValidationError):
            build_holding_rows(
                [],
                "account-id",
                datetime(2026, 10, 21),
                {},
            )

    def test_persistence_adds_only_holdings_and_one_audit_row(self):
        from app.integrations.dhan.holdings import persist_dhan_holdings
        from app.models import DataSyncRun, FinancialAccount

        session = MagicMock()
        session.scalars.return_value.all.return_value = []
        account = FinancialAccount(id=uuid.uuid4(), connection_id=uuid.uuid4())
        payload = [
            {
                "tradingSymbol": "KNOWN",
                "isin": "INE509F01029",
                "totalQty": "4",
            }
        ]

        records_processed = persist_dhan_holdings(session, account, payload)

        self.assertEqual(records_processed, 1)
        session.add_all.assert_called_once()
        holding_rows = session.add_all.call_args.args[0]
        self.assertEqual(len(holding_rows), 1)
        self.assertIsInstance(holding_rows[0], InvestmentHolding)
        self.assertIsNone(holding_rows[0].unit_price)
        self.assertIsNone(holding_rows[0].market_value)
        session.add.assert_called_once()
        self.assertIsInstance(session.add.call_args.args[0], DataSyncRun)
        self.assertEqual(session.add.call_args.args[0].records_processed, 1)
        session.commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
