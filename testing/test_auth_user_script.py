import unittest
import uuid
from datetime import datetime, timezone
from io import StringIO
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.auth.security import verify_password
from app.models import User, UserCredential
from database import get_test_engine
from scripts import create_auth_user as auth_user_script
from scripts.create_auth_user import create_auth_user


class CreateAuthUserScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = get_test_engine()
        cls.connection = cls.engine.connect()
        cls.outer_transaction = cls.connection.begin()
        database_name = cls.connection.execute(
            text("SELECT current_database()")
        ).scalar_one()
        if database_name != "BFAMS-DeFI-test":
            cls.outer_transaction.rollback()
            cls.connection.close()
            cls.engine.dispose()
            raise AssertionError("Tests require BFAMS-DeFI-test.")
        cls.counts_before = cls._counts()

    @classmethod
    def _counts(cls):
        return {
            table: cls.connection.execute(
                text(f'SELECT count(*) FROM "{table}"')
            ).scalar_one()
            for table in ("users", "user_credentials")
        }

    @classmethod
    def tearDownClass(cls):
        try:
            if cls._counts() != cls.counts_before:
                raise AssertionError("Auth user script tests changed row counts.")
        finally:
            cls.outer_transaction.rollback()
            cls.connection.close()
            cls.engine.dispose()

    def setUp(self):
        self.session = Session(
            bind=self.connection,
            join_transaction_mode="create_savepoint",
        )
        self.session.execute(text("SELECT 1"))
        self.email = f"{uuid.uuid4()}@example.test"
        self.password = "safe-test-password-123"

    def tearDown(self):
        self.session.close()

    def test_new_user_creates_normalized_credential(self):
        user_id = create_auth_user(
            self.session,
            email=f"  {self.email.upper()}  ",
            password=self.password,
        )
        credential = self.session.get(UserCredential, user_id)

        self.assertIsNotNone(self.session.get(User, user_id))
        self.assertEqual(credential.email, self.email)
        self.assertEqual(credential.failed_attempts, 0)
        self.assertIsNone(credential.locked_until)
        self.assertTrue(credential.is_active)
        self.assertTrue(
            verify_password(self.password, credential.password_hash)[0]
        )
        self.session.rollback()

    def test_attach_user_id_creates_credential_without_new_user(self):
        user = User()
        self.session.add(user)
        self.session.flush()
        before = self.session.scalar(select(func.count()).select_from(User))

        returned_id = create_auth_user(
            self.session,
            email=self.email,
            password=self.password,
            attach_user_id=user.id,
        )

        after = self.session.scalar(select(func.count()).select_from(User))
        self.assertEqual(returned_id, user.id)
        self.assertEqual(after, before)
        self.assertIsNotNone(self.session.get(UserCredential, user.id))
        self.session.rollback()

    def test_rejects_existing_credential_and_unknown_user(self):
        user = User()
        self.session.add(user)
        self.session.flush()
        create_auth_user(
            self.session,
            email=self.email,
            password=self.password,
            attach_user_id=user.id,
        )

        with self.assertRaises(ValueError):
            create_auth_user(
                self.session,
                email=self.email,
                password=self.password,
                attach_user_id=user.id,
            )
        with self.assertRaises(ValueError):
            create_auth_user(
                self.session,
                email=f"other-{self.email}",
                password=self.password,
                attach_user_id=uuid.uuid4(),
            )
        self.session.rollback()

    def test_rejects_invalid_email_and_password_policy(self):
        with self.assertRaises(ValueError):
            create_auth_user(
                self.session,
                email="not-an-email",
                password=self.password,
            )
        with self.assertRaises(ValueError):
            create_auth_user(
                self.session,
                email=self.email,
                password=self.email,
            )
        self.session.rollback()

    def test_cli_dry_run_rolls_back_and_emits_only_id_and_status(self):
        before = self.session.scalar(select(func.count()).select_from(User))
        output = StringIO()
        engine = MagicMock()

        def test_session(_engine):
            return Session(
                bind=self.connection,
                join_transaction_mode="create_savepoint",
            )

        with (
            patch.object(auth_user_script, "get_test_engine", return_value=engine),
            patch.object(auth_user_script, "Session", side_effect=test_session),
            patch.object(
                auth_user_script.getpass,
                "getpass",
                side_effect=[self.password, self.password],
            ),
            redirect_stdout(output),
        ):
            exit_code = auth_user_script.run(
                ["--new", "--email", self.email]
            )

        after = self.session.scalar(select(func.count()).select_from(User))
        lines = output.getvalue().splitlines()
        self.assertEqual(exit_code, 0)
        self.assertEqual(after, before)
        self.assertEqual(len(lines), 2)
        self.assertEqual(uuid.UUID(lines[0]).version, 4)
        self.assertEqual(lines[1], "created")
        self.assertNotIn(self.email, output.getvalue())
        self.assertNotIn(self.password, output.getvalue())
        engine.dispose.assert_called_once()


if __name__ == "__main__":
    unittest.main()
