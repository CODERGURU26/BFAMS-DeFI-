import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import SecretStr

from app.integrations.setu.config import SetuSettings
from app.routes.setu import setu_health


def settings(*, environment="sandbox", configured=True):
    values = {
        "environment": environment,
        "client_id": None,
        "client_secret": None,
        "product_instance_id": None,
        "_env_file": None,
    }
    if configured:
        values.update(
            client_id=SecretStr("test-client-id"),
            client_secret=SecretStr("test-client-secret"),
            product_instance_id=SecretStr("test-product-id"),
        )
    return SetuSettings(**values)


class TestSetuHealth(unittest.TestCase):
    def test_sandbox_configuration_is_ready_without_claiming_connectivity(self):
        with patch("app.routes.setu.SetuSettings", return_value=settings()):
            result = setu_health()

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["environment"], "sandbox")
        self.assertIs(result["configured"], True)
        self.assertEqual(result["connectivity"], "not_tested")

    def test_non_sandbox_environment_is_forbidden(self):
        with patch(
            "app.routes.setu.SetuSettings",
            return_value=settings(environment="production"),
        ):
            with self.assertRaises(HTTPException) as raised:
                setu_health()

        self.assertEqual(raised.exception.status_code, 403)

    def test_missing_credentials_return_controlled_unavailable(self):
        with patch(
            "app.routes.setu.SetuSettings",
            return_value=settings(configured=False),
        ):
            with self.assertRaises(HTTPException) as raised:
                setu_health()

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail, "Setu sandbox configuration is incomplete."
        )
