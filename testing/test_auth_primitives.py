import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import config, security, tokens
from app.auth.config import AuthConfigurationError
from app.auth.security import PasswordPolicyError
from app.auth.tokens import AccessTokenError
from database import get_test_engine


class AuthPrimitiveTests(unittest.TestCase):
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

        cls.counts_before = cls._table_counts()

    @classmethod
    def _table_counts(cls):
        return {
            table: cls.connection.execute(
                text(f'SELECT count(*) FROM "{table}"')
            ).scalar_one()
            for table in (
                "users",
                "user_credentials",
                "refresh_tokens",
                "auth_events",
            )
        }

    @classmethod
    def tearDownClass(cls):
        try:
            if cls._table_counts() != cls.counts_before:
                raise AssertionError("Auth test database counts changed.")
        finally:
            cls.outer_transaction.rollback()
            cls.connection.close()
            cls.engine.dispose()

    def setUp(self):
        self.session = Session(
            bind=self.connection,
            join_transaction_mode="create_savepoint",
        )

    def tearDown(self):
        self.session.close()

    def test_password_hash_verifies_and_wrong_password_fails(self):
        encoded = security.hash_password(
            "correct horse battery",
            "person@example.test",
        )

        self.assertTrue(encoded.startswith("$argon2id$"))
        valid, rehashed = security.verify_password(
            "correct horse battery",
            encoded,
        )
        self.assertTrue(valid)
        self.assertIsNone(rehashed)
        invalid, replacement = security.verify_password(
            "incorrect password",
            encoded,
        )
        self.assertFalse(invalid)
        self.assertIsNone(replacement)

    def test_password_policy_length_and_email_equality(self):
        for password in ("short", "person@example.test", "x" * 129):
            with self.subTest(password_length=len(password)):
                with self.assertRaises(PasswordPolicyError):
                    security.hash_password(password, " Person@Example.Test ")

    def test_successful_verification_rehashes_when_needed(self):
        class RehashingHasher:
            def verify(self, encoded_hash, password):
                return True

            def check_needs_rehash(self, encoded_hash):
                return True

            def hash(self, password):
                return "replacement-hash"

        with patch.object(security, "PASSWORD_HASHER", RehashingHasher()):
            verified, replacement = security.verify_password(
                "correct horse battery",
                "old-hash",
            )

        self.assertTrue(verified)
        self.assertEqual(replacement, "replacement-hash")

    def test_email_normalization(self):
        self.assertEqual(
            security.normalize_email("  Person@Example.Test "),
            "person@example.test",
        )

    def test_auth_secret_fails_closed_when_missing_or_short(self):
        for secret in ("", "short-secret"):
            with self.subTest(secret_length=len(secret)):
                with patch.dict("os.environ", {"AUTH_JWT_SECRET": secret}):
                    with self.assertRaises(AuthConfigurationError):
                        config.get_jwt_secret()
                    with self.assertRaises(AuthConfigurationError):
                        tokens.create_access_token(str(uuid.uuid4()))
                    with self.assertRaises(AuthConfigurationError):
                        tokens.decode_access_token("invalid")

    def test_access_token_round_trip_and_required_claims(self):
        with patch.dict(
            "os.environ",
            {"AUTH_JWT_SECRET": "s" * 40},
        ):
            issued_at = datetime.now(timezone.utc)
            token, expires_in = tokens.create_access_token(
                str(uuid.uuid4()),
                now=issued_at,
            )
            claims = tokens.decode_access_token(token, now=issued_at)

        self.assertEqual(expires_in, 900)
        self.assertEqual(claims["iss"], "bfams")
        self.assertEqual(claims["typ"], "access")
        self.assertIn("jti", claims)

    def test_access_tokens_reject_expired_tampered_none_and_wrong_issuer(self):
        secret = "s" * 40
        now = datetime.now(timezone.utc)
        base_claims = {
            "sub": str(uuid.uuid4()),
            "iss": "bfams",
            "iat": now - timedelta(minutes=20),
            "exp": now - timedelta(minutes=1),
            "typ": "access",
        }
        with patch.dict("os.environ", {"AUTH_JWT_SECRET": secret}):
            expired = jwt.encode(base_claims, secret, algorithm="HS256")
            with self.assertRaises(AccessTokenError):
                tokens.decode_access_token(expired, now=now)

            valid, _ = tokens.create_access_token(str(uuid.uuid4()), now=now)
            token_parts = valid.split(".")
            token_parts[2] = ("a" if token_parts[2][0] != "a" else "b") + token_parts[2][1:]
            tampered = ".".join(token_parts)
            with self.assertRaises(AccessTokenError):
                tokens.decode_access_token(tampered, now=now)

            none_token = jwt.encode(
                {
                    "sub": str(uuid.uuid4()),
                    "iss": "bfams",
                    "iat": now,
                    "exp": now + timedelta(minutes=5),
                    "typ": "access",
                },
                key="",
                algorithm="none",
            )
            with self.assertRaises(AccessTokenError):
                tokens.decode_access_token(none_token, now=now)

            wrong_issuer = jwt.encode(
                {
                    "sub": str(uuid.uuid4()),
                    "iss": "other",
                    "iat": now,
                    "exp": now + timedelta(minutes=5),
                    "typ": "access",
                },
                secret,
                algorithm="HS256",
            )
            with self.assertRaises(AccessTokenError):
                tokens.decode_access_token(wrong_issuer, now=now)

            wrong_type = jwt.encode(
                {
                    "sub": str(uuid.uuid4()),
                    "iss": "bfams",
                    "iat": now,
                    "exp": now + timedelta(minutes=5),
                    "typ": "refresh",
                },
                secret,
                algorithm="HS256",
            )
            with self.assertRaises(AccessTokenError):
                tokens.decode_access_token(wrong_type, now=now)

    def test_access_token_rejected_after_password_change(self):
        secret = "s" * 40
        issued_at = datetime.now(timezone.utc)
        changed_at = issued_at + timedelta(seconds=1)
        with patch.dict("os.environ", {"AUTH_JWT_SECRET": secret}):
            token, _ = tokens.create_access_token(
                str(uuid.uuid4()),
                now=issued_at,
            )
            with self.assertRaises(AccessTokenError):
                tokens.decode_access_token(
                    token,
                    password_changed_at=changed_at,
                    now=changed_at,
                )

    def test_access_token_same_second_as_password_change_is_rejected(self):
        secret = "s" * 40
        password_changed_at = datetime.now(timezone.utc).replace(microsecond=500000)
        issued_at = password_changed_at.replace(microsecond=0)
        with patch.dict("os.environ", {"AUTH_JWT_SECRET": secret}):
            token, _ = tokens.create_access_token(
                str(uuid.uuid4()),
                now=issued_at,
            )
            with self.assertRaises(AccessTokenError):
                tokens.decode_access_token(
                    token,
                    password_changed_at=password_changed_at,
                    now=password_changed_at,
                )

    def test_refresh_token_is_random_and_only_hash_is_persistable(self):
        first = tokens.create_refresh_token()
        second = tokens.create_refresh_token()

        self.assertNotEqual(first, second)
        self.assertNotEqual(first, tokens.hash_refresh_token(first))
        self.assertEqual(len(tokens.hash_refresh_token(first)), 64)


if __name__ == "__main__":
    unittest.main()
