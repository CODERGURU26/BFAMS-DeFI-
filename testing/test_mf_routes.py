"""
testing/test_mf_routes.py
=========================
FastAPI endpoint integration tests for Mutual Fund Analytics:
- GET /mf/schemes
- GET /mf/schemes/{code}
- GET /mf/schemes/{code}/navs
- GET /mf/schemes/{code}/returns
- GET /mf/schemes/{code}/rolling-returns
- GET /mf/schemes/{code}/drawdown
- POST /mf/compare
"""

import unittest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from main import app
from app.dependencies import get_mf_db
from database import get_test_engine


class TestMfAnalyticsRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = get_test_engine()

        def override_get_mf_db():
            with Session(cls.engine) as session:
                yield session

        app.dependency_overrides[get_mf_db] = override_get_mf_db
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()

    def test_route_schemes_search(self):
        response = self.client.get("/mf/schemes?q=100033&limit=5")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("schemes", data)
        self.assertGreaterEqual(data["total"], 1)
        self.assertEqual(data["schemes"][0]["scheme_code"], "100033")

    def test_route_scheme_details(self):
        response = self.client.get("/mf/schemes/100033")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["scheme_code"], "100033")
        self.assertIn("Large & Mid Cap", data["scheme_name"])

    def test_route_scheme_details_404(self):
        response = self.client.get("/mf/schemes/NONEXISTENT_999999")
        self.assertEqual(response.status_code, 404)

    def test_route_scheme_navs(self):
        response = self.client.get("/mf/schemes/100033/navs?start_date=2021-10-14&end_date=2021-10-25&limit=10")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["scheme_code"], "100033")
        self.assertGreaterEqual(len(data["navs"]), 2)
        self.assertEqual(data["navs"][0]["nav_date"], "2021-10-14")

    def test_route_returns_cagr(self):
        response = self.client.get("/mf/schemes/100033/returns?start_date=2021-10-14&end_date=2022-10-14")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["scheme_code"], "100033")
        self.assertEqual(data["elapsed_days"], 365)
        self.assertTrue(data["annualized"])
        self.assertIsNotNone(data["cagr_pct"])

    def test_route_returns_invalid_dates(self):
        response = self.client.get("/mf/schemes/100033/returns?start_date=2023-10-14&end_date=2021-10-14")
        self.assertEqual(response.status_code, 400)

    def test_route_drawdown(self):
        response = self.client.get("/mf/schemes/100033/drawdown?start_date=2021-10-14&end_date=2023-10-14")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertLess(float(data["max_drawdown_pct"]), 0)
        self.assertIn("peak_date", data)
        self.assertIn("trough_date", data)

    def test_route_rolling_returns(self):
        response = self.client.get("/mf/schemes/100051/rolling-returns?start_date=2021-10-14&end_date=2023-10-14&window_days=365&step_days=60")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertGreaterEqual(data["total_periods"], 5)
        self.assertTrue(data["annualized"])

    def test_route_compare(self):
        payload = {
            "scheme_codes": ["100033", "100051"],
            "start_date": "2022-01-01",
            "end_date": "2023-01-01",
            "max_staleness_days": 7,
        }
        response = self.client.post("/mf/compare", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["schemes_evaluated"], 2)
        self.assertEqual(len(data["results"]), 2)

    def test_route_navs_reversed_dates_rejected(self):
        """Fix B: GET /mf/schemes/{code}/navs rejects start_date > end_date with HTTP 400."""
        response = self.client.get("/mf/schemes/100033/navs?start_date=2023-10-14&end_date=2021-10-14")
        self.assertEqual(response.status_code, 400)
        self.assertIn("start_date", response.json()["detail"])

    def test_route_navs_equal_dates_allowed(self):
        """Fix B: GET /mf/schemes/{code}/navs allows start_date == end_date for single-day lookup."""
        response = self.client.get("/mf/schemes/100033/navs?start_date=2021-10-14&end_date=2021-10-14")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total_observations"], 1)
        self.assertEqual(data["navs"][0]["nav_date"], "2021-10-14")

    def test_route_rolling_returns_reversed_dates_rejected(self):
        """Fix B: GET /mf/schemes/{code}/rolling-returns rejects start_date > end_date with HTTP 400."""
        response = self.client.get("/mf/schemes/100033/rolling-returns?start_date=2024-01-01&end_date=2023-01-01")
        self.assertEqual(response.status_code, 400)
        self.assertIn("start_date", response.json()["detail"])

    def test_route_rolling_returns_equal_dates_rejected(self):
        """Fix B: GET /mf/schemes/{code}/rolling-returns rejects start_date == end_date with HTTP 400."""
        response = self.client.get("/mf/schemes/100033/rolling-returns?start_date=2023-01-01&end_date=2023-01-01")
        self.assertEqual(response.status_code, 400)
        self.assertIn("start_date", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
