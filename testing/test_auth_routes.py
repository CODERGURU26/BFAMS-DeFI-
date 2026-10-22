import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth import routes, security, tokens
from app.auth.deps import get_auth_db
from app.models import AuthEvent, RefreshToken, User, UserCredential
from database import get_test_engine


class AuthRouteTests(unittest.TestCase):
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
        cls.app = FastAPI()
        cls.app.include_router(routes.router)

        def override_auth_db():
            session = Session(
                bind=cls.connection,
                join_transaction_mode="create_savepoint",
            )
            try:
                yield session
            finally:
                session.close()

        cls.app.dependency_overrides[get_auth_db] = override_auth_db

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
                raise AssertionError("Auth route tests changed database counts.")
        finally:
            cls.app.dependency_overrides.clear()
            cls.outer_transaction.rollback()
            cls.connection.close()
            cls.engine.dispose()

    def setUp(self):
        self.secret = "route-test-secret-that-is-long-enough"
        self.environment = patch.dict(
            os.environ,
            {
                "AUTH_JWT_SECRET": self.secret,
                "AUTH_REGISTRATION_ENABLED": "false",
                "AUTH_COOKIE_SECURE": "true",
            },
        )
        self.environment.start()
        self.client = TestClient(self.app, base_url="https://testserver")
        self.email = f"{uuid.uuid4()}@example.test"
        self.db = Session(
            bind=self.connection,
            join_transaction_mode="create_savepoint",
        )
        self.db.execute(text("SELECT 1"))

    def tearDown(self):
        self.db.close()
        self.client.close()
        self.environment.stop()

    def _create_user(
        self,
        email: str | None = None,
        password: str = "long-enough-test-password",
        *,
        active: bool = True,
        failed_attempts: int = 0,
        locked_until: datetime | None = None,
        password_changed_at: datetime | None = None,
    ) -> UserCredential:
        email = email or self.email
        user = User()
        self.db.add(user)
        self.db.flush()
        credential = UserCredential(
            user_id=user.id,
            email=email,
            password_hash=security.hash_password(password, email),
            failed_attempts=failed_attempts,
            locked_until=locked_until,
            is_active=active,
            password_changed_at=password_changed_at or datetime.now(timezone.utc),
        )
        self.db.add(credential)
        self.db.flush()
        return credential

    def test_login_success_sets_secure_refresh_cookie_and_returns_access(self):
        self._create_user()

        response = self.client.post(
            "/auth/login",
            json={
                "email": f"  {self.email.upper()} ",
                "password": "long-enough-test-password",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["token_type"], "bearer")
        self.assertEqual(response.json()["expires_in"], 900)
        self.assertTrue(response.json()["access_token"])
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertIn("path=/auth", cookie)
        self.assertIn("secure", cookie)
        self.assertNotIn(self.email, response.text.lower())
        self.assertNotIn("long-enough-test-password", response.text)
        self.assertNotIn(self.secret, response.text)

    def test_unknown_and_wrong_password_have_identical_responses(self):
        self._create_user()
        inactive = self._create_user(
            email=f"inactive-{uuid.uuid4()}@example.test",
            active=False,
        )
        locked = self._create_user(
            email=f"locked-{uuid.uuid4()}@example.test",
            locked_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

        wrong_password = self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "incorrect-test-password",
            },
        )
        unknown_email = self.client.post(
            "/auth/login",
            json={
                "email": f"unknown-{uuid.uuid4()}@example.test",
                "password": "incorrect-test-password",
            },
        )
        inactive_email = self.client.post(
            "/auth/login",
            json={
                "email": inactive.email,
                "password": "incorrect-test-password",
            },
        )
        locked_email = self.client.post(
            "/auth/login",
            json={
                "email": locked.email,
                "password": "incorrect-test-password",
            },
        )

        self.assertEqual(wrong_password.status_code, 401)
        self.assertEqual(wrong_password.json(), {"detail": "invalid credentials"})
        self.assertEqual(unknown_email.status_code, 401)
        self.assertEqual(unknown_email.json(), wrong_password.json())
        self.assertEqual(inactive_email.status_code, 401)
        self.assertEqual(inactive_email.json(), wrong_password.json())
        self.assertEqual(locked_email.status_code, 401)
        self.assertEqual(locked_email.json(), wrong_password.json())

    def test_lockout_after_five_failures_and_no_extension_while_locked(self):
        credential = self._create_user()

        for _ in range(5):
            response = self.client.post(
                "/auth/login",
                json={
                    "email": self.email,
                    "password": "incorrect-test-password",
                },
            )
            self.assertEqual(response.status_code, 401)

        self.db.refresh(credential)
        locked_until = credential.locked_until
        self.assertEqual(credential.failed_attempts, 5)
        self.assertGreater(locked_until, datetime.now(timezone.utc))
        self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "long-enough-test-password",
            },
        )
        self.db.refresh(credential)
        self.assertEqual(credential.locked_until, locked_until)
        event_types = self.db.scalars(
            select(AuthEvent.event_type).where(
                AuthEvent.user_id == credential.user_id
            )
        ).all()
        self.assertEqual(event_types.count("locked"), 1)

    def test_successful_login_resets_failed_attempts_and_expired_lock(self):
        credential = self._create_user(
            failed_attempts=4,
            locked_until=datetime.now(timezone.utc) - timedelta(minutes=1),
        )

        response = self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "long-enough-test-password",
            },
        )

        self.db.refresh(credential)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(credential.failed_attempts, 0)
        self.assertIsNone(credential.locked_until)

    def test_login_persists_password_rehash(self):
        credential = self._create_user()
        with patch.object(
            security,
            "verify_password",
            return_value=(True, "replacement-hash"),
        ):
            response = self.client.post(
                "/auth/login",
                json={
                    "email": self.email,
                    "password": "long-enough-test-password",
                },
            )
        self.db.refresh(credential)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(credential.password_hash, "replacement-hash")

    def test_me_rejects_inactive_user_and_token_before_password_change(self):
        inactive = self._create_user(
            email=f"inactive-{uuid.uuid4()}@example.test",
            active=False,
        )
        old_token, _ = tokens.create_access_token(str(inactive.user_id))
        inactive_response = self.client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {old_token}"},
        )
        self.assertEqual(inactive_response.status_code, 401)
        self.assertEqual(
            inactive_response.headers["www-authenticate"],
            "Bearer",
        )

        credential = self._create_user(
            email=f"active-{uuid.uuid4()}@example.test"
        )
        issued_at = credential.password_changed_at - timedelta(seconds=2)
        old_token, _ = tokens.create_access_token(
            str(credential.user_id),
            now=issued_at,
        )
        expired_by_password_change = self.client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {old_token}"},
        )
        self.assertEqual(expired_by_password_change.status_code, 401)

    def test_refresh_rotates_token_and_reuse_revokes_family(self):
        self._create_user()
        login_response = self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "long-enough-test-password",
            },
        )
        self.assertEqual(login_response.status_code, 200)
        old_raw = self.client.cookies.get("bfams_refresh")

        rotated = self.client.post(
            "/auth/refresh",
            headers={
                "X-BFAMS-Auth": "1",
                "Origin": "https://testserver",
            },
        )
        self.assertEqual(rotated.status_code, 200)
        new_raw = self.client.cookies.get("bfams_refresh")
        self.assertNotEqual(old_raw, new_raw)
        self.assertNotIn(old_raw, rotated.text)
        self.assertNotIn(new_raw, rotated.text)

        reused = self.client.post(
            "/auth/refresh",
            headers={"X-BFAMS-Auth": "1"},
            cookies={"bfams_refresh": old_raw},
        )
        self.assertEqual(reused.status_code, 401)
        family_rows = self.db.scalars(
            select(RefreshToken).where(
                RefreshToken.token_hash.in_(
                    [tokens.hash_refresh_token(old_raw), tokens.hash_refresh_token(new_raw)]
                )
            )
        ).all()
        self.assertEqual(len(family_rows), 2)
        self.assertTrue(all(row.revoked_at is not None for row in family_rows))
        self.assertEqual(
            self.db.scalar(
                select(AuthEvent.event_type)
                .where(AuthEvent.event_type == "refresh_reuse_detected")
                .limit(1)
            ),
            "refresh_reuse_detected",
        )

    def test_logout_revokes_refresh_family_and_clears_cookie(self):
        self._create_user()
        self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "long-enough-test-password",
            },
        )
        raw_token = self.client.cookies.get("bfams_refresh")

        response = self.client.post(
            "/auth/logout",
            headers={"X-BFAMS-Auth": "1"},
        )

        self.assertEqual(response.status_code, 204)
        refresh = self.db.scalar(
            select(RefreshToken).where(
                RefreshToken.token_hash == tokens.hash_refresh_token(raw_token)
            )
        )
        self.assertIsNotNone(refresh.revoked_at)
        self.assertIn("max-age=0", response.headers["set-cookie"].lower())

    def test_refresh_and_logout_enforce_request_protection(self):
        missing_header = self.client.post("/auth/refresh")
        self.assertEqual(missing_header.status_code, 400)
        foreign_origin = self.client.post(
            "/auth/logout",
            headers={
                "X-BFAMS-Auth": "1",
                "Origin": "https://foreign.example",
            },
        )
        self.assertEqual(foreign_origin.status_code, 403)

    def test_registration_disabled_and_enabled(self):
        disabled = self.client.post(
            "/auth/register",
            json={
                "email": f"new-{uuid.uuid4()}@example.test",
                "password": "long-enough-test-password",
            },
        )
        self.assertEqual(disabled.status_code, 403)

        with patch.dict(os.environ, {"AUTH_REGISTRATION_ENABLED": "true"}):
            enabled = self.client.post(
                "/auth/register",
                json={
                    "email": f"new-{uuid.uuid4()}@example.test",
                    "password": "another-long-test-password",
                },
            )
        self.assertEqual(enabled.status_code, 201)
        self.assertIn("user_id", enabled.json())
        self.assertNotIn("email", enabled.json())
        self.assertNotIn("another-long-test-password", enabled.text)

    def test_validation_errors_do_not_echo_email_or_password(self):
        private_email = f"invalid-{uuid.uuid4()}-no-at-symbol"
        private_password = "short-secret"
        with patch.dict(os.environ, {"AUTH_REGISTRATION_ENABLED": "true"}):
            response = self.client.post(
                "/auth/register",
                json={"email": private_email, "password": private_password},
            )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(private_email, response.text)
        self.assertNotIn(private_password, response.text)

    def test_missing_secret_fails_closed_for_routes_and_current_user(self):
        for secret in ("", "short-secret"):
            with patch.dict(os.environ, {"AUTH_JWT_SECRET": secret}):
                login = self.client.post(
                    "/auth/login",
                    json={
                        "email": self.email,
                        "password": "long-enough-test-password",
                    },
                )
                refresh = self.client.post(
                    "/auth/refresh",
                    headers={"X-BFAMS-Auth": "1"},
                )
                register = self.client.post(
                    "/auth/register",
                    json={
                        "email": f"new-{uuid.uuid4()}@example.test",
                        "password": "another-long-test-password",
                    },
                )
                current_user = self.client.get(
                    "/auth/me",
                    headers={"Authorization": "Bearer invalid"},
                )
            self.assertEqual(login.status_code, 503)
            self.assertEqual(refresh.status_code, 503)
            self.assertEqual(register.status_code, 503)
            self.assertEqual(current_user.status_code, 401)


if __name__ == "__main__":
    unittest.main()
