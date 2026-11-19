import unittest
from unittest.mock import patch

from pydantic import ValidationError

from database import Settings, get_app_engine


def make_settings(**overrides):
    values = {
        "db_name": "BFAMS-DeFI",
        "db_user": "test-user",
        "db_password": "test-password",
        "test_db_name": "BFAMS-DeFI-test",
    }
    values.update(overrides)
    return Settings(**values)


class ApplicationDatabaseTargetTests(unittest.TestCase):
    def test_test_target_selects_test_database(self):
        self.assertEqual(make_settings().app_database_name, "BFAMS-DeFI-test")

    def test_main_target_selects_main_database(self):
        self.assertEqual(
            make_settings(app_database_target="main").app_database_name,
            "BFAMS-DeFI",
        )

    def test_main_target_rejects_non_staging_database(self):
        with self.assertRaises(RuntimeError):
            make_settings(
                db_name="other-database",
                app_database_target="main",
            ).app_database_name

    def test_unknown_target_is_rejected(self):
        with self.assertRaises(ValidationError):
            make_settings(app_database_target="other")

    def test_app_engine_uses_selected_target(self):
        with patch("database.settings.app_database_target", "test"), patch(
            "database.get_test_engine", return_value="test-engine"
        ):
            self.assertEqual(get_app_engine(), "test-engine")

        with patch("database.settings.app_database_target", "main"), patch(
            "database.engine", "main-engine"
        ):
            self.assertEqual(get_app_engine(), "main-engine")


if __name__ == "__main__":
    unittest.main()
