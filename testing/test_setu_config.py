import unittest
from unittest.mock import patch

from app.integrations.setu.config import SetuSettings


class TestSetuSettings(unittest.TestCase):
    def test_missing_configuration_is_optional_and_defaults_to_sandbox(self):
        settings = SetuSettings(_env_file=None)

        self.assertIsNone(settings.client_id)
        self.assertIsNone(settings.client_secret)
        self.assertIsNone(settings.product_instance_id)
        self.assertIsNone(settings.vua)
        self.assertEqual(settings.environment, "sandbox")
        self.assertFalse(settings.has_credentials)

    def test_configuration_loads_from_setu_environment_variables(self):
        environment = {
            "SETU_CLIENT_ID": "  test-client  ",
            "SETU_CLIENT_SECRET": " test-secret ",
            "SETU_PRODUCT_INSTANCE_ID": " test-product ",
            "SETU_ENVIRONMENT": "sandbox",
            "SETU_VUA": " private-vua@setu ",
        }
        with patch.dict("os.environ", environment, clear=True):
            settings = SetuSettings(_env_file=None)

        self.assertTrue(settings.has_credentials)
        self.assertEqual(settings.environment, "sandbox")
        self.assertEqual(settings.client_id.get_secret_value(), "test-client")
        self.assertEqual(settings.client_secret.get_secret_value(), "test-secret")
        self.assertEqual(
            settings.product_instance_id.get_secret_value(),
            "test-product",
        )
        self.assertEqual(settings.vua.get_secret_value(), "private-vua@setu")
        self.assertNotIn("test-secret", repr(settings))
        self.assertNotIn("test-client", repr(settings))
        self.assertNotIn("test-product", repr(settings))
        self.assertNotIn("private-vua@setu", repr(settings))

    def test_blank_environment_credentials_are_treated_as_missing(self):
        environment = {
            "SETU_CLIENT_ID": " ",
            "SETU_CLIENT_SECRET": "\t",
            "SETU_PRODUCT_INSTANCE_ID": "",
            "SETU_VUA": " ",
        }
        with patch.dict("os.environ", environment, clear=True):
            settings = SetuSettings(_env_file=None)

        self.assertIsNone(settings.client_id)
        self.assertIsNone(settings.client_secret)
        self.assertIsNone(settings.product_instance_id)
        self.assertIsNone(settings.vua)
        self.assertFalse(settings.has_credentials)
