import os
import unittest
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pyotp
from cryptography.fernet import Fernet
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth import crypto, routes, security, tokens
from app.auth.deps import get_auth_db
from app.models import (
    AuthEvent,
    RefreshToken,
    User,
    UserCredential,
    UserTotpSecret,
)
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
                "user_totp_secrets",
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
        with routes._LOGIN_FAILURES_LOCK:
            routes._LOGIN_FAILURES_BY_IP.clear()
        with routes._MFA_LOGIN_CHALLENGES_LOCK:
            routes._MFA_LOGIN_CHALLENGES.clear()
        self.secret = "route-test-secret-that-is-long-enough"
        self.totp_encryption_key = Fernet.generate_key().decode("ascii")
        self.environment = patch.dict(
            os.environ,
            {
                "AUTH_JWT_SECRET": self.secret,
                "AUTH_REGISTRATION_ENABLED": "false",
                "AUTH_COOKIE_SECURE": "true",
                "AUTH_TOTP_ENCRYPTION_KEY": self.totp_encryption_key,
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
        with routes._LOGIN_FAILURES_LOCK:
            routes._LOGIN_FAILURES_BY_IP.clear()
        with routes._MFA_LOGIN_CHALLENGES_LOCK:
            routes._MFA_LOGIN_CHALLENGES.clear()

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
                "totp_code": "ignored-when-mfa-is-disabled",
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

    def test_login_rate_limit_and_successful_attempts_are_not_counted(self):
        self._create_user()
        success = self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "long-enough-test-password",
            },
        )
        self.assertEqual(success.status_code, 200)

        for _ in range(5):
            failure = self.client.post(
                "/auth/login",
                json={
                    "email": self.email,
                    "password": "incorrect-test-password",
                },
            )
            self.assertEqual(failure.status_code, 401)
        limited = self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "incorrect-test-password",
            },
        )
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(
            limited.json(),
            {"detail": "Too many login attempts. Please try again later."},
        )
        self.assertNotIn(self.email, limited.text)

    def test_hourly_login_limit(self):
        self._create_user()
        with patch.object(
            routes,
            "_LOGIN_RATE_LIMITS",
            ((20, timedelta(hours=1)),),
        ):
            for _ in range(20):
                response = self.client.post(
                    "/auth/login",
                    json={
                        "email": self.email,
                        "password": "incorrect-test-password",
                    },
                )
                self.assertEqual(response.status_code, 401)
            limited = self.client.post(
                "/auth/login",
                json={
                    "email": self.email,
                    "password": "incorrect-test-password",
                },
            )
        self.assertEqual(limited.status_code, 429)

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

    def test_session_listing_returns_metadata_only_and_marks_current(self):
        self._create_user()
        first = self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "long-enough-test-password",
            },
        )
        first_raw = self.client.cookies.get("bfams_refresh")
        second = self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "long-enough-test-password",
            },
        )
        second_raw = self.client.cookies.get("bfams_refresh")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        response = self.client.get(
            "/auth/sessions",
            headers={"Authorization": f"Bearer {second.json()['access_token']}"},
        )

        self.assertEqual(response.status_code, 200)
        sessions = response.json()
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sum(item["current"] for item in sessions), 1)
        for item in sessions:
            self.assertEqual(
                set(item),
                {"session_id", "issued_at", "expires_at", "current"},
            )
        for secret in (
            first_raw,
            second_raw,
            tokens.hash_refresh_token(first_raw),
            tokens.hash_refresh_token(second_raw),
        ):
            self.assertNotIn(secret, response.text)

    def test_new_session_endpoints_require_authentication(self):
        session_id = uuid.uuid4()
        requests = (
            ("GET", "/auth/sessions"),
            ("POST", f"/auth/sessions/{session_id}/revoke"),
            ("POST", "/auth/logout-all"),
            ("POST", "/auth/mfa/setup"),
            ("POST", "/auth/mfa/verify", {"code": "123456"}),
            ("GET", "/auth/mfa/status"),
            (
                "POST",
                "/auth/mfa/disable",
                {"password": "long-enough-test-password", "code": "123456"},
            ),
            (
                "POST",
                "/auth/change-password",
                {
                    "current_password": "long-enough-test-password",
                    "new_password": "brand-new-long-password",
                },
            ),
        )
        for item in requests:
            method, path, *body = item
            with self.subTest(method=method, path=path):
                response = self.client.request(
                    method,
                    path,
                    json=body[0] if body else None,
                    headers={"X-BFAMS-Auth": "1"},
                )
                self.assertEqual(response.status_code, 401)

    def test_revoke_own_session_blocks_refresh_and_foreign_session_is_hidden(self):
        credential_a = self._create_user()
        login_a = self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "long-enough-test-password",
            },
        )
        raw_a = self.client.cookies.get("bfams_refresh")
        row_a = self.db.scalar(
            select(RefreshToken).where(
                RefreshToken.token_hash == tokens.hash_refresh_token(raw_a)
            )
        )
        response = self.client.post(
            f"/auth/sessions/{row_a.id}/revoke",
            headers={"Authorization": f"Bearer {login_a.json()['access_token']}"},
        )
        self.assertEqual(response.status_code, 204)
        self.client.cookies.clear()
        self.client.cookies.set(
            "bfams_refresh",
            raw_a,
            domain="testserver",
            path="/auth",
        )
        refresh = self.client.post(
            "/auth/refresh",
            headers={"X-BFAMS-Auth": "1"},
        )
        self.assertEqual(refresh.status_code, 401)

        foreign_email = f"{uuid.uuid4()}@example.test"
        credential_b = self._create_user(email=foreign_email)
        self.client.cookies.clear()
        login_b = self.client.post(
            "/auth/login",
            json={
                "email": foreign_email,
                "password": "long-enough-test-password",
            },
        )
        raw_b = self.client.cookies.get("bfams_refresh")
        row_b = self.db.scalar(
            select(RefreshToken).where(
                RefreshToken.token_hash == tokens.hash_refresh_token(raw_b)
            )
        )
        foreign = self.client.post(
            f"/auth/sessions/{row_b.id}/revoke",
            headers={"Authorization": f"Bearer {login_a.json()['access_token']}"},
        )
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.json(), {"detail": "Resource not found."})
        self.assertNotIn(str(row_b.id), foreign.text)
        self.assertIsNotNone(credential_a)
        self.assertIsNotNone(credential_b)
        self.assertEqual(login_b.status_code, 200)

    def test_logout_all_revokes_every_refresh_family(self):
        self._create_user()
        first_login = self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "long-enough-test-password",
            },
        )
        first_raw = self.client.cookies.get("bfams_refresh")
        second_login = self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "long-enough-test-password",
            },
        )
        second_raw = self.client.cookies.get("bfams_refresh")

        response = self.client.post(
            "/auth/logout-all",
            headers={
                "Authorization": f"Bearer {first_login.json()['access_token']}",
                "X-BFAMS-Auth": "1",
            },
        )

        self.assertEqual(response.status_code, 204)
        for raw_token in (first_raw, second_raw):
            self.client.cookies.clear()
            self.client.cookies.set(
                "bfams_refresh",
                raw_token,
                domain="testserver",
                path="/auth",
            )
            refresh = self.client.post(
                "/auth/refresh",
                headers={"X-BFAMS-Auth": "1"},
            )
            self.assertEqual(refresh.status_code, 401)
        self.assertEqual(second_login.status_code, 200)

    def test_password_change_revokes_sessions_and_invalidates_old_access(self):
        credential = self._create_user()
        login = self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "long-enough-test-password",
            },
        )
        raw_refresh = self.client.cookies.get("bfams_refresh")
        old_access = login.json()["access_token"]
        changed_at_before = credential.password_changed_at

        wrong_current = self.client.post(
            "/auth/change-password",
            headers={"Authorization": f"Bearer {old_access}"},
            json={
                "current_password": "incorrect-test-password",
                "new_password": "brand-new-long-password",
            },
        )
        self.assertEqual(wrong_current.status_code, 400)

        changed = self.client.post(
            "/auth/change-password",
            headers={"Authorization": f"Bearer {old_access}"},
            json={
                "current_password": "long-enough-test-password",
                "new_password": "brand-new-long-password",
            },
        )
        self.assertEqual(changed.status_code, 204)
        self.assertIn("max-age=0", changed.headers["set-cookie"].lower())
        self.db.refresh(credential)
        self.assertGreater(credential.password_changed_at, changed_at_before)
        old_access_response = self.client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {old_access}"},
        )
        self.assertEqual(old_access_response.status_code, 401)
        old_refresh_response = self.client.post(
            "/auth/refresh",
            headers={"X-BFAMS-Auth": "1"},
            cookies={"bfams_refresh": raw_refresh},
        )
        self.assertEqual(old_refresh_response.status_code, 401)

        new_login = self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "brand-new-long-password",
            },
        )
        self.assertEqual(new_login.status_code, 200)

    def test_auth_events_are_limited_to_allowed_types_without_sensitive_fields(self):
        credential = self._create_user()
        failed = self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "incorrect-test-password",
            },
        )
        self.assertEqual(failed.status_code, 401)
        login = self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "long-enough-test-password",
            },
        )
        self.assertEqual(login.status_code, 200)
        raw = self.client.cookies.get("bfams_refresh")
        refreshed = self.client.post(
            "/auth/refresh",
            headers={"X-BFAMS-Auth": "1"},
        )
        self.assertEqual(refreshed.status_code, 200)
        reused = self.client.post(
            "/auth/refresh",
            headers={"X-BFAMS-Auth": "1"},
            cookies={"bfams_refresh": raw},
        )
        self.assertEqual(reused.status_code, 401)
        with patch.object(routes, "_LOGIN_FAILURES_BY_IP", defaultdict(deque)):
            fresh_login = self.client.post(
                "/auth/login",
                json={
                    "email": self.email,
                    "password": "long-enough-test-password",
                },
            )
        self.assertEqual(fresh_login.status_code, 200)
        logout = self.client.post(
            "/auth/logout",
            headers={"X-BFAMS-Auth": "1"},
        )
        self.assertEqual(logout.status_code, 204)
        changed = self.client.post(
            "/auth/change-password",
            headers={
                "Authorization": f"Bearer {fresh_login.json()['access_token']}"
            },
            json={
                "current_password": "long-enough-test-password",
                "new_password": "brand-new-long-password",
            },
        )
        self.assertEqual(changed.status_code, 204)
        event_types = set(
            self.db.scalars(
                select(AuthEvent.event_type).where(
                    AuthEvent.user_id == credential.user_id
                )
            ).all()
        )
        self.assertTrue(
            {
                "login_success",
                "login_failed",
                "refresh",
                "refresh_reuse_detected",
                "logout",
                "password_changed",
            }.issubset(event_types),
            str(event_types),
        )
        self.assertTrue(
            event_types.issubset(routes._ALLOWED_AUTH_EVENTS)
        )
        event_columns = set(AuthEvent.__table__.columns.keys())
        self.assertFalse(
            event_columns.intersection(
                {
                    "password",
                    "password_hash",
                    "email",
                    "access_token",
                    "refresh_token",
                    "token_hash",
                    "jwt_secret",
                }
            )
        )

    def _login_and_get_headers(self, password="long-enough-test-password"):
        login = self.client.post(
            "/auth/login",
            json={"email": self.email, "password": password},
        )
        self.assertEqual(login.status_code, 200, login.text)
        return {
            "Authorization": f"Bearer {login.json()['access_token']}"
        }

    def _enable_mfa(self):
        headers = self._login_and_get_headers()
        setup = self.client.post("/auth/mfa/setup", headers=headers)
        self.assertEqual(setup.status_code, 200, setup.text)
        secret = setup.json()["secret"]
        code = pyotp.TOTP(secret).now()
        enabled = self.client.post(
            "/auth/mfa/verify",
            headers=headers,
            json={"code": code},
        )
        self.assertEqual(enabled.status_code, 204, enabled.text)
        return headers, secret

    def test_mfa_setup_verify_status_and_audit_events(self):
        credential = self._create_user()
        headers = self._login_and_get_headers()

        status_before = self.client.get("/auth/mfa/status", headers=headers)
        self.assertEqual(status_before.status_code, 200)
        self.assertEqual(status_before.json(), {"enabled": False})

        setup = self.client.post("/auth/mfa/setup", headers=headers)
        self.assertEqual(setup.status_code, 200, setup.text)
        secret = setup.json()["secret"]
        self.assertEqual(setup.json()["otpauth_uri"], pyotp.TOTP(secret).provisioning_uri(
            name=self.email,
            issuer_name="BFAMS-DeFi",
        ))
        self.assertEqual(pyotp.parse_uri(setup.json()["otpauth_uri"]).secret, secret)
        self.assertFalse(
            self.db.get(UserTotpSecret, credential.user_id).enabled
        )
        stored_secret = self.db.get(UserTotpSecret, credential.user_id).secret
        self.assertNotEqual(stored_secret, secret)
        self.assertEqual(crypto.decrypt_totp_secret(stored_secret), secret)
        self.assertNotIn(secret, stored_secret)
        self.assertFalse(
            routes.totp_security.verify_totp_code(
                stored_secret,
                pyotp.TOTP(secret).now(),
            )
        )

        invalid = self.client.post(
            "/auth/mfa/verify",
            headers=headers,
            json={"code": "not-a-code"},
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertNotIn(secret, invalid.text)

        valid = self.client.post(
            "/auth/mfa/verify",
            headers=headers,
            json={"code": pyotp.TOTP(secret).now()},
        )
        self.assertEqual(valid.status_code, 204)
        status_after = self.client.get("/auth/mfa/status", headers=headers)
        self.assertEqual(status_after.json(), {"enabled": True})
        self.assertNotIn(secret, status_after.text)
        sessions = self.client.get("/auth/sessions", headers=headers)
        self.assertEqual(sessions.status_code, 200)
        self.assertNotIn(secret, sessions.text)

        event_types = set(
            self.db.scalars(
                select(AuthEvent.event_type).where(
                    AuthEvent.user_id == credential.user_id
                )
            ).all()
        )
        self.assertIn("mfa_enabled", event_types)
        self.assertNotIn(secret, str(event_types))

    def test_totp_encryption_roundtrip_and_key_validation(self):
        plaintext = pyotp.random_base32()
        ciphertext = crypto.encrypt_totp_secret(plaintext)
        self.assertNotEqual(ciphertext, plaintext)
        self.assertEqual(crypto.decrypt_totp_secret(ciphertext), plaintext)
        self.assertTrue(routes.config.mfa_encryption_available())

        for key in ("", "invalid-key", Fernet.generate_key().decode("ascii")):
            with patch.dict(os.environ, {"AUTH_TOTP_ENCRYPTION_KEY": key}):
                self.assertEqual(
                    routes.config.mfa_encryption_available(),
                    bool(key) and key != "invalid-key",
                )
                if key == "invalid-key" or key == "":
                    with self.assertRaises(routes.AuthConfigurationError):
                        routes.config.get_totp_encryption_key()
        wrong_key = Fernet.generate_key().decode("ascii")
        with patch.dict(
            os.environ,
            {"AUTH_TOTP_ENCRYPTION_KEY": wrong_key},
        ):
            with self.assertRaises(routes.AuthConfigurationError):
                crypto.decrypt_totp_secret(ciphertext)
        with self.assertRaises(routes.AuthConfigurationError):
            crypto.decrypt_totp_secret(ciphertext[:-2] + "xx")

    def test_missing_or_invalid_encryption_key_fails_closed(self):
        self._create_user()
        headers = self._login_and_get_headers()

        for key in ("", "invalid-key"):
            with patch.dict(os.environ, {"AUTH_TOTP_ENCRYPTION_KEY": key}):
                setup = self.client.post("/auth/mfa/setup", headers=headers)
                verify = self.client.post(
                    "/auth/mfa/verify",
                    headers=headers,
                    json={"code": "123456"},
                )
                disable = self.client.post(
                    "/auth/mfa/disable",
                    headers=headers,
                    json={"password": "long-enough-test-password", "code": "123456"},
                )
            self.assertEqual(setup.status_code, 503)
            self.assertEqual(verify.status_code, 503)
            self.assertEqual(disable.status_code, 503)

    def test_plaintext_secret_upgrades_after_successful_login_verification(self):
        credential = self._create_user()
        _, secret = self._enable_mfa()
        configuration = self.db.get(UserTotpSecret, credential.user_id)
        configuration.secret = secret
        self.db.flush()

        response = self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "long-enough-test-password",
                "totp_code": pyotp.TOTP(secret).now(),
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.db.refresh(configuration)
        self.assertNotEqual(configuration.secret, secret)
        self.assertEqual(crypto.decrypt_totp_secret(configuration.secret), secret)

    def test_plaintext_secret_upgrades_after_successful_setup_verification(self):
        credential = self._create_user()
        headers = self._login_and_get_headers()
        setup = self.client.post("/auth/mfa/setup", headers=headers)
        secret = setup.json()["secret"]
        configuration = self.db.get(UserTotpSecret, credential.user_id)
        configuration.secret = secret
        self.db.flush()

        response = self.client.post(
            "/auth/mfa/verify",
            headers=headers,
            json={"code": pyotp.TOTP(secret).now()},
        )

        self.assertEqual(response.status_code, 204)
        self.db.refresh(configuration)
        self.assertNotEqual(configuration.secret, secret)
        self.assertEqual(crypto.decrypt_totp_secret(configuration.secret), secret)

    def test_corrupted_ciphertext_fails_mfa_login_safely(self):
        credential = self._create_user()
        _, secret = self._enable_mfa()
        configuration = self.db.get(UserTotpSecret, credential.user_id)
        configuration.secret = "gAAAAA-corrupted-ciphertext"
        self.db.flush()

        response = self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "long-enough-test-password",
                "totp_code": pyotp.TOTP(secret).now(),
            },
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "invalid credentials"})
        self.assertNotIn(configuration.secret, response.text)

    def test_missing_key_does_not_disable_mfa_login(self):
        self._create_user()
        _, secret = self._enable_mfa()
        with patch.dict(os.environ, {"AUTH_TOTP_ENCRYPTION_KEY": ""}):
            response = self.client.post(
                "/auth/login",
                json={
                    "email": self.email,
                    "password": "long-enough-test-password",
                    "totp_code": pyotp.TOTP(secret).now(),
                },
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "invalid credentials"})
        self.assertNotIn(secret, response.text)

    def test_mfa_disable_requires_password_and_valid_totp(self):
        credential = self._create_user()
        headers, secret = self._enable_mfa()
        code = pyotp.TOTP(secret).now()

        wrong_password = self.client.post(
            "/auth/mfa/disable",
            headers=headers,
            json={"password": "wrong-password", "code": code},
        )
        self.assertEqual(wrong_password.status_code, 400)
        wrong_code = self.client.post(
            "/auth/mfa/disable",
            headers=headers,
            json={
                "password": "long-enough-test-password",
                "code": "000000" if code != "000000" else "999999",
            },
        )
        self.assertEqual(wrong_code.status_code, 400)
        self.assertTrue(self.db.get(UserTotpSecret, credential.user_id).enabled)

        disabled = self.client.post(
            "/auth/mfa/disable",
            headers=headers,
            json={
                "password": "long-enough-test-password",
                "code": pyotp.TOTP(secret).now(),
            },
        )
        self.assertEqual(disabled.status_code, 204, disabled.text)
        self.assertEqual(
            self.client.get("/auth/mfa/status", headers=headers).json(),
            {"enabled": False},
        )
        event_types = set(
            self.db.scalars(
                select(AuthEvent.event_type).where(
                    AuthEvent.user_id == credential.user_id
                )
            ).all()
        )
        self.assertIn("mfa_disabled", event_types)

    def test_mfa_login_uses_two_step_challenge_and_rejects_invalid_code(self):
        self._create_user()
        _, secret = self._enable_mfa()

        attempts = [
            {"email": f"{uuid.uuid4()}@example.test", "password": "wrong-password"},
            {"email": self.email, "password": "wrong-password"},
        ]
        failures = [
            self.client.post("/auth/login", json=attempt)
            for attempt in attempts
        ]
        self.assertTrue(
            all(
                response.status_code == 401
                and response.json() == {"detail": "invalid credentials"}
                for response in failures
            )
        )
        self.assertTrue(all(secret not in response.text for response in failures))

        challenge = self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "long-enough-test-password",
            },
        )
        self.assertEqual(challenge.status_code, 202)
        self.assertEqual(challenge.json(), {"mfa_required": True})
        self.assertTrue(
            self.client.cookies.get(routes._MFA_CHALLENGE_COOKIE)
        )

        invalid = self.client.post(
            "/auth/login",
            json={"totp_code": "000000" if pyotp.TOTP(secret).now() != "000000" else "999999"},
        )
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(invalid.json(), {"detail": "invalid credentials"})

        successful = self.client.post(
            "/auth/login",
            json={"totp_code": pyotp.TOTP(secret).now()},
        )
        self.assertEqual(successful.status_code, 200, successful.text)
        self.assertNotIn(secret, successful.text)
        self.assertIsNone(
            self.client.cookies.get(routes._MFA_CHALLENGE_COOKIE)
        )

    def test_password_change_keeps_mfa_enabled(self):
        self._create_user()
        headers, secret = self._enable_mfa()
        changed = self.client.post(
            "/auth/change-password",
            headers=headers,
            json={
                "current_password": "long-enough-test-password",
                "new_password": "brand-new-long-password",
            },
        )
        self.assertEqual(changed.status_code, 204)
        self.assertEqual(
            self.client.get("/auth/mfa/status", headers=headers).status_code,
            401,
        )
        row = self.db.scalar(
            select(UserTotpSecret).where(
                UserTotpSecret.user_id == self.db.scalar(
                    select(UserCredential.user_id).where(
                        UserCredential.email == self.email
                    )
                )
            )
        )
        self.assertIsNotNone(row)
        self.assertTrue(row.enabled)
        login_without_code = self.client.post(
            "/auth/login",
            json={
                "email": self.email,
                "password": "brand-new-long-password",
            },
        )
        self.assertEqual(login_without_code.status_code, 202)
        self.assertEqual(login_without_code.json(), {"mfa_required": True})
        login_with_code = self.client.post(
            "/auth/login",
            json={"totp_code": pyotp.TOTP(secret).now()},
        )
        self.assertEqual(login_with_code.status_code, 200)

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
        user = self._create_user()
        access, _ = tokens.create_access_token(str(user.user_id))
        logout_all_missing_header = self.client.post(
            "/auth/logout-all",
            headers={"Authorization": f"Bearer {access}"},
        )
        self.assertEqual(logout_all_missing_header.status_code, 400)

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
