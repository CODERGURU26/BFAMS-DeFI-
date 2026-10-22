import importlib
import os
import sys
import types
import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx

from app.integrations.dhan.client import (
    DHAN_HOLDINGS_URL,
    DHAN_ORDERS_URL,
    DHAN_POSITIONS_URL,
    DhanAPIError,
    DhanClient,
    DhanConfigurationError,
)
from app.integrations.dhan.holdings import (
    DhanHoldingsValidationError,
    analyze_dhan_holdings,
    build_holding_rows,
)
from app.integrations.dhan.valuation import (
    DhanValuationError,
    build_live_dhan_portfolio,
    fetch_live_dhan_portfolio,
)
from app.integrations.dhan.settings import demo_sync_enabled
from app.models import InvestmentHolding


def _portfolio_module():
    dependencies = types.ModuleType("app.dependencies")
    dependencies.get_mf_db = lambda: None
    with patch.dict(sys.modules, {"app.dependencies": dependencies}):
        return importlib.import_module("app.routes.portfolio")


portfolio = _portfolio_module()


class FakeResult:
    def scalar(self):
        return "BFAMS-DeFI-test"


class FakeSession:
    def __init__(self, *, cooling_down=False):
        self.cooling_down = cooling_down
        self.scalar_calls = 0
        self.investment_holdings_count = 9
        self.added = []
        self.add_all_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.events = []

    def execute(self, statement):
        self.events.append("database-check")
        return FakeResult()

    def scalar(self, statement):
        self.scalar_calls += 1
        if self.cooling_down and self.scalar_calls == 1:
            return uuid.uuid4()
        return None

    def scalars(self, statement):
        result = MagicMock()
        result.all.return_value = []
        return result

    def add(self, row):
        self.events.append("insert")
        self.added.append(row)

    def add_all(self, rows):
        self.add_all_calls += 1
        self.added.extend(rows)

    def commit(self):
        self.events.append("commit")
        self.commit_calls += 1

    def rollback(self):
        self.events.append("rollback")
        self.rollback_calls += 1

    def count_investment_holdings(self):
        return self.investment_holdings_count


class FakeDhanClient:
    def __init__(
        self,
        payload=None,
        error=None,
        positions=None,
        orders=None,
    ):
        self.payload = payload or []
        self.error = error
        self.positions = positions or []
        self.orders = orders or []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def get_holdings(self):
        if self.error:
            raise self.error
        return self.payload

    def get_positions(self):
        if self.error:
            raise self.error
        return self.positions

    def get_orders(self):
        if self.error:
            raise self.error
        return self.orders


class TestDhanClient(unittest.TestCase):
    def test_uses_only_holdings_get_with_required_headers(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=[])

        with DhanClient(
            "test-client",
            "test-access-token",
            transport=httpx.MockTransport(handler),
        ) as client:
            self.assertTrue(client.get_holdings() == [])

        self.assertTrue(len(requests) == 1)
        request = requests[0]
        self.assertTrue(request.method == "GET")
        self.assertTrue(str(request.url) == DHAN_HOLDINGS_URL)
        self.assertTrue(request.headers["client-id"] == "test-client")
        self.assertTrue(request.headers["access-token"] == "test-access-token")
        self.assertTrue(request.content == b"")

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

        self.assertTrue(raised.exception.status_code == 302)
        self.assertTrue(len(requests) == 1)

    def test_error_message_contains_only_status_and_fixed_phrase(self):
        response_body = '{"errorCode":"401","errorMsg":"Invalid access token"}'

        def handler(request):
            return httpx.Response(401, text=response_body)

        with DhanClient(
            "header-client-secret",
            "access-token-secret",
            transport=httpx.MockTransport(handler),
        ) as client:
            with self.assertRaises(DhanAPIError) as raised:
                client.get_holdings()

        message = str(raised.exception)
        self.assertTrue("Dhan holdings request failed (HTTP 401)." in message)
        self.assertTrue("Invalid access token" in message)
        self.assertTrue(raised.exception.response_body == response_body)
        self.assertTrue("header-client-secret" not in message)
        self.assertTrue("access-token-secret" not in message)

    def test_rejects_missing_credentials_and_malformed_response(self):
        with self.assertRaises(DhanConfigurationError):
            DhanClient("", "test-token")

        def handler(request):
            return httpx.Response(200, json={"holdings": []})

        with DhanClient(
            "test-client",
            "test-access-token",
            transport=httpx.MockTransport(handler),
        ) as client:
            with self.assertRaises(DhanAPIError):
                client.get_holdings()

    def test_reads_positions_and_orders_with_required_headers(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=[])

        with DhanClient(
            "test-client",
            "test-access-token",
            transport=httpx.MockTransport(handler),
        ) as client:
            with self.assertLogs(
                "app.integrations.dhan.client",
                level="INFO",
            ) as captured:
                positions = client.get_positions()
                orders = client.get_orders()

        self.assertEqual(positions, [])
        self.assertEqual(orders, [])
        self.assertEqual(len(requests), 2)
        self.assertEqual(
            [(request.method, str(request.url)) for request in requests],
            [("GET", DHAN_POSITIONS_URL), ("GET", DHAN_ORDERS_URL)],
        )
        for request in requests:
            self.assertEqual(request.headers["client-id"], "test-client")
            self.assertEqual(request.headers["access-token"], "test-access-token")
        log_output = "\n".join(captured.output)
        self.assertIn(DHAN_POSITIONS_URL, log_output)
        self.assertIn(DHAN_ORDERS_URL, log_output)
        self.assertIn("status_code=200", log_output)
        self.assertIn("client_id_present=True", log_output)
        self.assertIn("access_token_present=True", log_output)
        self.assertNotIn("test-access-token", log_output)


class TestDhanHoldingPlanning(unittest.TestCase):
    def test_reuses_metadata_without_copying_older_prices(self):
        observed_at = datetime(2026, 10, 21, tzinfo=timezone.utc)
        previous = InvestmentHolding(
            account_id=uuid.uuid4(),
            asset_name="Private name",
            asset_identifier="INE509F01029",
            asset_type="EQUITY",
            quantity=Decimal("1"),
            unit_price=Decimal("10"),
            market_value=Decimal("10"),
            currency="INR",
            observed_at=observed_at,
        )
        rows = build_holding_rows(
            [
                {
                    "tradingSymbol": "SYMBOL",
                    "isin": "INE509F01029",
                    "totalQty": "12",
                }
            ],
            uuid.uuid4(),
            observed_at,
            {"INE509F01029": previous},
        )
        self.assertTrue(rows[0].asset_name == previous.asset_name)
        self.assertTrue(rows[0].unit_price is None)
        self.assertTrue(rows[0].market_value is None)

    def test_zero_quantity_is_skipped_and_negative_or_invalid_rejected(self):
        session = MagicMock()
        session.scalar.return_value = None
        session.scalars.return_value.all.return_value = []
        account = MagicMock(id=uuid.uuid4())
        plan = analyze_dhan_holdings(
            session,
            account,
            [{"tradingSymbol": "SYMBOL", "isin": None, "totalQty": "0"}],
        )
        self.assertTrue(plan.fetched == 1)
        self.assertTrue(plan.to_insert == 0)
        self.assertTrue(plan.skipped_zero_qty == 1)
        for quantity in ("-1", "not-a-number"):
            with self.subTest(quantity_type=type(quantity).__name__):
                with self.assertRaises(DhanHoldingsValidationError):
                    build_holding_rows(
                        [{"tradingSymbol": "SYMBOL", "totalQty": quantity}],
                        uuid.uuid4(),
                        datetime.now(timezone.utc),
                        {},
                    )

    def test_reconciles_identifiers_and_symbol_only_metadata(self):
        observed_at = datetime(2026, 10, 20, tzinfo=timezone.utc)
        previous_rows = [
            InvestmentHolding(
                account_id=uuid.uuid4(),
                asset_name="Fixture",
                asset_identifier="INE509F01029",
                asset_type="EQUITY",
                quantity=Decimal("3"),
                unit_price=None,
                market_value=None,
                currency="INR",
                observed_at=observed_at,
            ),
            InvestmentHolding(
                account_id=uuid.uuid4(),
                asset_name="Fixture",
                asset_identifier="INE758T01015",
                asset_type="EQUITY",
                quantity=Decimal("1"),
                unit_price=None,
                market_value=None,
                currency="INR",
                observed_at=observed_at,
            ),
            InvestmentHolding(
                account_id=uuid.uuid4(),
                asset_name="Fixture",
                asset_identifier="OLD-SYMBOL",
                asset_type="EQUITY",
                quantity=Decimal("7"),
                unit_price=None,
                market_value=None,
                currency="INR",
                observed_at=observed_at,
            ),
        ]
        session = MagicMock()
        session.scalar.return_value = observed_at
        session.scalars.side_effect = [
            MagicMock(**{"all.return_value": previous_rows}),
            MagicMock(**{"all.return_value": previous_rows[:2]}),
        ]
        plan = analyze_dhan_holdings(
            session,
            MagicMock(id=uuid.uuid4()),
            [
                {
                    "tradingSymbol": "SAME",
                    "isin": "INE509F01029",
                    "totalQty": "3",
                },
                {
                    "tradingSymbol": "DIFFERENT",
                    "isin": "INE758T01015",
                    "totalQty": "2",
                },
                {
                    "tradingSymbol": "NEW-SYMBOL",
                    "isin": None,
                    "totalQty": "4",
                },
            ],
        )
        self.assertTrue(plan.same_quantity == 1)
        self.assertTrue(plan.different_quantity == 1)
        self.assertTrue(plan.new_in_api == 1)
        self.assertTrue(plan.absent_from_api == 1)
        self.assertTrue(plan.inferred_from_symbol_only == 1)
        self.assertTrue(plan.without_isin == 1)

    def test_apply_persists_positive_rows_and_audit_in_one_commit(self):
        from app.integrations.dhan.holdings import persist_dhan_holdings
        from app.models import DataSyncRun, FinancialAccount

        session = FakeSession()
        account = FinancialAccount(id=uuid.uuid4(), connection_id=uuid.uuid4())
        result = persist_dhan_holdings(
            session,
            account,
            [
                {"tradingSymbol": "POSITIVE", "isin": None, "totalQty": "2"},
                {"tradingSymbol": "ZERO", "isin": None, "totalQty": "0"},
            ],
        )
        holding_rows = [
            row for row in session.added if isinstance(row, InvestmentHolding)
        ]
        audit_rows = [row for row in session.added if isinstance(row, DataSyncRun)]
        self.assertTrue(result["fetched"] == 2)
        self.assertTrue(result["to_insert"] == 1)
        self.assertTrue(result["skipped_zero_qty"] == 1)
        self.assertTrue(len(holding_rows) == 1)
        self.assertTrue(holding_rows[0].unit_price is None)
        self.assertTrue(holding_rows[0].market_value is None)
        self.assertTrue(len(audit_rows) == 1)
        self.assertTrue(audit_rows[0].records_processed == 1)
        self.assertTrue(session.commit_calls == 1)

    def test_requires_timezone_aware_timestamp(self):
        with self.assertRaises(DhanHoldingsValidationError):
            build_holding_rows(
                [],
                uuid.uuid4(),
                datetime(2026, 10, 21),
                {},
            )


class TestDhanLiveValuation(unittest.TestCase):
    def test_uses_positions_pnl_and_returns_only_open_positions(self):
        result = build_live_dhan_portfolio(
            holdings=[{}, {}],
            positions=[
                {
                    "tradingSymbol": "FIXTURE",
                    "positionType": "LONG",
                    "productType": "CNC",
                    "netQty": 4,
                    "realizedPnl": "12.5",
                    "unrealizedPnl": "7.5",
                    "dayPnl": "3",
                },
                {
                    "tradingSymbol": "CLOSED-FIXTURE",
                    "positionType": "CLOSED",
                    "netQty": 0,
                    "realizedProfit": "2",
                    "unrealizedProfit": "0",
                    "dayPnl": "0",
                },
            ],
            orders=[{}, {}, {}],
        )

        self.assertEqual(result["holdings_count"], 2)
        self.assertEqual(result["position_count"], 2)
        self.assertEqual(result["orders_count"], 3)
        self.assertEqual(result["realized_pnl"], Decimal("14.5"))
        self.assertEqual(result["unrealized_pnl"], Decimal("7.5"))
        self.assertEqual(result["day_pnl"], Decimal("3"))
        self.assertEqual(len(result["open_positions"]), 1)
        self.assertEqual(
            result["open_positions"][0]["trading_symbol"],
            "FIXTURE",
        )

    def test_uses_documented_pnl_names_and_leaves_missing_day_pnl_unavailable(self):
        result = build_live_dhan_portfolio(
            holdings=[],
            positions=[
                {
                    "positionType": "SHORT",
                    "netQty": -2,
                    "realizedProfit": "8",
                    "unrealizedProfit": "-3",
                }
            ],
            orders=[],
        )

        self.assertEqual(result["realized_pnl"], Decimal("8"))
        self.assertEqual(result["unrealized_pnl"], Decimal("-3"))
        self.assertIsNone(result["day_pnl"])
        self.assertEqual(result["open_positions"][0]["day_pnl"], None)

    def test_returns_zero_totals_for_no_positions(self):
        result = build_live_dhan_portfolio([], [], [])
        self.assertEqual(result["realized_pnl"], Decimal(0))
        self.assertEqual(result["unrealized_pnl"], Decimal(0))
        self.assertEqual(result["day_pnl"], Decimal(0))

    def test_rejects_non_object_api_items(self):
        with self.assertRaises(DhanValuationError):
            build_live_dhan_portfolio([{}], [None], [])

    def test_live_fetch_calls_only_holdings_positions_and_orders(self):
        client = MagicMock(spec=["get_holdings", "get_positions", "get_orders"])
        client.get_holdings.return_value = [{}]
        client.get_positions.return_value = []
        client.get_orders.return_value = [{}]

        result = fetch_live_dhan_portfolio(client)

        self.assertEqual(result["holdings_count"], 1)
        self.assertEqual(result["position_count"], 0)
        self.assertEqual(result["orders_count"], 1)
        client.get_holdings.assert_called_once_with()
        client.get_positions.assert_called_once_with()
        client.get_orders.assert_called_once_with()
        self.assertFalse(hasattr(client, "get_market_quote"))


class TestDhanSyncRoute(unittest.TestCase):
    def setUp(self):
        from fastapi import HTTPException
        from starlette.requests import Request

        self.session = FakeSession()
        self.account = MagicMock(id=uuid.uuid4(), connection_id=uuid.uuid4())
        self.http_exception = HTTPException
        self.request_type = Request

    def _request(self, *, headers=None, host="localhost:8000", origin=None):
        request_headers = (
            [(b"x-bfams-sync", b"1")]
            if headers is None
            else []
        )
        if headers is not None:
            request_headers = [
                (key.lower().encode(), value.encode())
                for key, value in headers.items()
            ]
        request_headers.append((b"host", host.encode()))
        if origin is not None:
            request_headers.append((b"origin", origin.encode()))
        return self.request_type(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/portfolio/dhan/sync",
                "raw_path": b"/portfolio/dhan/sync",
                "query_string": b"",
                "headers": request_headers,
                "server": (host.split(":")[0], 8000),
                "client": ("127.0.0.1", 50000),
                "root_path": "",
            }
        )

    def _post(self, *, headers=None, host="localhost:8000", origin=None, dry_run=True):
        request = self._request(headers=headers, host=host, origin=origin)
        with (
            patch.object(portfolio, "demo_sync_enabled", return_value=True),
            patch.object(portfolio, "_get_dhan_account", return_value=self.account),
        ):
            try:
                return portfolio.sync_dhan_holdings(
                    request,
                    dry_run=dry_run,
                    session=self.session,
                )
            except BaseException as exc:
                return exc

    def test_disabled_sync_returns_403(self):
        request = self._request()
        caught = []
        with patch.object(portfolio, "demo_sync_enabled", return_value=False):
            try:
                portfolio.sync_dhan_holdings(request, session=self.session)
            except BaseException as exc:
                caught.append(exc)
            else:
                self.fail("Disabled sync was not rejected.")
        response = caught[0]
        self.assertTrue(response.status_code == 403)

    def test_missing_header_returns_400(self):
        response = self._post(headers={})
        self.assertTrue(response.status_code == 400)

    def test_wrong_host_returns_403(self):
        response = self._post(host="example.invalid:8000")
        self.assertTrue(response.status_code == 403)

    def test_foreign_origin_returns_403(self):
        response = self._post(origin="http://foreign.invalid:8000")
        self.assertTrue(response.status_code == 403)

    def test_cooldown_returns_429(self):
        self.session.cooling_down = True
        response = self._post()
        self.assertTrue(response.status_code == 429)

    def test_dry_run_returns_only_counts_and_writes_nothing(self):
        before = self.session.count_investment_holdings()
        mock_client = FakeDhanClient(
            [{"tradingSymbol": "SYMBOL", "isin": None, "totalQty": "2"}]
        )
        with patch.object(
            portfolio.DhanClient,
            "from_environment",
            return_value=mock_client,
        ):
            response = self._post()

        body = response.model_dump()
        self.assertTrue(body["fetched"] == 1)
        self.assertTrue(body["to_insert"] == 1)
        after = self.session.count_investment_holdings()
        self.assertTrue(after == before)
        self.assertTrue(self.session.added == [])
        self.assertTrue(self.session.add_all_calls == 0)
        self.assertTrue(self.session.commit_calls == 0)
        self.assertTrue(set(body) == {
            "fetched",
            "to_insert",
            "skipped_zero_qty",
            "same_quantity",
            "different_quantity",
            "new_in_api",
            "absent_from_api",
            "inferred_from_symbol_only",
            "without_isin",
        })

    def test_failed_apply_records_only_exception_type_and_sanitizes_response(self):
        mock_client = FakeDhanClient(
            [{"tradingSymbol": "SYMBOL", "isin": None, "totalQty": "2"}]
        )
        with (
            patch.object(
                portfolio.DhanClient,
                "from_environment",
                return_value=mock_client,
            ),
            patch.object(
                portfolio,
                "persist_dhan_holdings",
                side_effect=ValueError("secret and holding values"),
            ),
        ):
            response = self._post(dry_run=False)

        self.assertTrue(response.status_code == 502)
        self.assertTrue("secret" not in response.detail)
        self.assertTrue("holding values" not in response.detail)
        self.assertTrue(len(self.session.added) == 1)
        failed_run = self.session.added[0]
        self.assertTrue(failed_run.status == "failed")
        self.assertTrue(failed_run.records_processed == 0)
        self.assertTrue(failed_run.error_message == "ValueError")
        self.assertTrue(self.session.commit_calls == 1)
        self.assertTrue(
            self.session.events[-4:]
            == ["rollback", "database-check", "insert", "commit"]
        )

    def test_dhan_api_error_response_has_no_secrets_or_body(self):
        mock_client = FakeDhanClient(error=DhanAPIError(401))
        with patch.object(
            portfolio.DhanClient,
            "from_environment",
            return_value=mock_client,
        ):
            response = self._post()
        self.assertTrue(response.status_code == 502)
        self.assertTrue(
            response.detail == "Dhan holdings request failed (HTTP 401)."
        )
        self.assertTrue("token" not in response.detail.lower())
        self.assertTrue("isin" not in response.detail.lower())
        self.assertTrue("quantity" not in response.detail.lower())

    def test_live_valuation_returns_aggregate_only_and_writes_nothing(self):
        mock_client = FakeDhanClient(
            payload=[{}, {}],
            positions=[
                {
                    "tradingSymbol": "FIXTURE",
                    "positionType": "LONG",
                    "productType": "CNC",
                    "netQty": 2,
                    "realizedPnl": "10",
                    "unrealizedPnl": "5",
                    "dayPnl": "3",
                }
            ],
            orders=[{}],
        )
        with (
            patch.object(
                portfolio.DhanClient,
                "from_environment",
                return_value=mock_client,
            ),
            patch.object(portfolio, "_verify_test_db"),
            patch.object(portfolio, "_get_dhan_account", return_value=self.account),
            patch.object(portfolio, "demo_sync_enabled", return_value=True),
        ):
            response = portfolio.get_dhan_live_valuation(
                self._request(),
                session=self.session,
            )

        body = response.model_dump()
        self.assertEqual(body["holdings_count"], 2)
        self.assertEqual(body["position_count"], 1)
        self.assertEqual(body["orders_count"], 1)
        self.assertEqual(body["realized_pnl"], "10")
        self.assertEqual(body["unrealized_pnl"], "5")
        self.assertEqual(body["day_pnl"], "3")
        self.assertEqual(body["open_positions"][0]["trading_symbol"], "FIXTURE")
        self.assertEqual(
            set(body),
            {
                "retrieved_at",
                "holdings_count",
                "position_count",
                "orders_count",
                "realized_pnl",
                "unrealized_pnl",
                "day_pnl",
                "open_positions",
            },
        )
        self.assertEqual(self.session.added, [])
        self.assertEqual(self.session.commit_calls, 0)

    def test_live_valuation_handles_missing_day_pnl_without_market_feed(self):
        mock_client = FakeDhanClient(
            positions=[
                {
                    "positionType": "LONG",
                    "netQty": 2,
                    "realizedProfit": "4",
                    "unrealizedProfit": "-1",
                }
            ],
        )
        with (
            patch.object(
                portfolio.DhanClient,
                "from_environment",
                return_value=mock_client,
            ),
            patch.object(portfolio, "_verify_test_db"),
            patch.object(portfolio, "_get_dhan_account", return_value=self.account),
            patch.object(portfolio, "demo_sync_enabled", return_value=True),
        ):
            response = portfolio.get_dhan_live_valuation(
                self._request(),
                session=self.session,
            )

        self.assertEqual(response.realized_pnl, "4")
        self.assertEqual(response.unrealized_pnl, "-1")
        self.assertIsNone(response.day_pnl)
        self.assertEqual(self.session.added, [])
        self.assertEqual(self.session.commit_calls, 0)
        self.assertEqual(self.session.added, [])
        self.assertEqual(self.session.commit_calls, 0)


class TestDhanSettings(unittest.TestCase):
    def test_demo_sync_flag_defaults_to_disabled(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(demo_sync_enabled())
            os.environ["DEMO_SYNC_ENABLED"] = "true"
            self.assertTrue(demo_sync_enabled())


class TestDashboardPortfolioInsights(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dependencies = types.ModuleType("app.dependencies")
        dependencies.get_mf_db = lambda: None

        with patch.dict(
            sys.modules,
            {"app.dependencies": dependencies},
        ):
            cls.dashboard = importlib.import_module(
                "app.routes.testing_dashboard"
            )

    def test_unpriced_and_concentrated_positions_get_informational_prompts(self):
        suggestions = self.dashboard._portfolio_suggestions(
            [
                {
                    "name": "Fixture A",
                    "asset_type": "EQUITY",
                    "market_value": "80",
                    "stale": False,
                },
                {
                    "name": "Fixture B",
                    "asset_type": "ETF",
                    "market_value": "20",
                    "stale": True,
                },
                {
                    "name": "Fixture C",
                    "asset_type": "EQUITY",
                    "market_value": None,
                    "stale": False,
                },
            ]
        )
        titles = {item["title"] for item in suggestions}
        self.assertTrue("Check price coverage" in titles)
        self.assertTrue("Review stale market data" in titles)
        self.assertTrue("Review largest holding exposure" in titles)
        self.assertTrue("Review asset-type mix" in titles)
        self.assertTrue(
            all("buy" not in item["detail"].lower() for item in suggestions)
        )

    def test_breakdowns_group_by_asset_type_and_limit_holdings_chart(self):
        holdings = [
            {
                "name": f"Fixture {index}",
                "asset_type": "EQUITY" if index < 6 else "ETF",
                "market_value": str(20 - index),
            }
            for index in range(7)
        ]
        holdings.append(
            {
                "name": "Unpriced fixture",
                "asset_type": "MUTUAL_FUND",
                "market_value": None,
            }
        )

        asset_types, largest_holdings = self.dashboard._portfolio_breakdowns(
            holdings
        )

        self.assertEqual(
            [row["name"] for row in asset_types],
            ["EQUITY", "ETF"],
        )
        self.assertEqual(len(largest_holdings), 6)
        self.assertEqual(largest_holdings[0]["name"], "Fixture 0")
        self.assertEqual(largest_holdings[-1]["name"], "Other holdings")
        self.assertAlmostEqual(
            sum(Decimal(row["share"]) for row in asset_types),
            Decimal(100),
        )

    def test_prompts_top_three_concentration_and_complexity_without_trade_advice(self):
        holdings = [
            {
                "name": f"Fixture {index}",
                "asset_type": "EQUITY",
                "market_value": "15" if index < 3 else "1",
                "stale": False,
            }
            for index in range(20)
        ]

        suggestions = self.dashboard._portfolio_suggestions(holdings)
        titles = {item["title"] for item in suggestions}

        self.assertIn("Check top-three concentration", titles)
        self.assertIn("Review portfolio complexity", titles)
        self.assertTrue(
            all(
                phrase not in item["detail"].lower()
                for item in suggestions
                for phrase in ("buy", "sell", "should invest")
            )
        )

    def test_snapshot_change_uses_only_common_priced_identifiers(self):
        result = self.dashboard._comparable_snapshot_change(
            {
                "shared": Decimal("100"),
                "old-only": Decimal("50"),
                "unpriced": None,
            },
            {
                "shared": Decimal("125"),
                "new-only": Decimal("75"),
                "unpriced": Decimal("40"),
            },
        )
        self.assertTrue(result["count"] == 1)
        self.assertTrue(result["previous_value"] == "100")
        self.assertTrue(result["current_value"] == "125")
        self.assertTrue(result["amount"] == "25")
        self.assertTrue(Decimal(result["percentage"]) == Decimal("25"))


if __name__ == "__main__":
    unittest.main()
