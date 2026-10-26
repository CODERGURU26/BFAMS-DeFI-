import unittest
from unittest.mock import Mock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.deps import get_current_user
from app.auth.schemas import UserResponse
from app.dependencies import get_mf_db
from app.routes.transactions import router


class TransactionEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.db = Mock()
        self.db.execute.return_value.scalar_one.return_value = "BFAMS-DeFI-test"
        self.db.scalars.return_value.all.return_value = []

        def override_get_mf_db():
            yield self.db

        self.app.dependency_overrides[get_mf_db] = override_get_mf_db
        self.client = TestClient(self.app)

    def tearDown(self):
        self.app.dependency_overrides.clear()

    def test_lists_transactions_for_resolved_user(self):
        user = UserResponse(user_id=uuid4(), email="test@example.com")
        self.app.dependency_overrides[get_current_user] = lambda: user

        response = self.client.get("/transactions/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
        self.db.scalars.assert_called_once()

    def test_requires_authentication(self):
        response = self.client.get("/transactions/")

        self.assertEqual(response.status_code, 401)
