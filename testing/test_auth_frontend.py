import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes.testing_dashboard import router


class AuthFrontendTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()

    def test_dashboard_exposes_phase_one_to_three_auth_controls(self):
        response = self.client.get("/testing/dashboard")

        self.assertEqual(response.status_code, 200)
        for expected in (
            "Authentication workflow",
            'id="auth-login"',
            'id="auth-register"',
            'id="auth-password"',
            'id="auth-refresh"',
            'id="auth-logout"',
            'id="auth-logout-all"',
            'id="auth-mfa-step"',
            'id="mfa-setup-flow"',
            'id="mfa-disable-form"',
            "'/auth/sessions'",
            "'/auth/change-password'",
            "'/auth/logout-all'",
            "'/auth/mfa/setup'",
            "'/auth/mfa/verify'",
            "'/auth/mfa/status'",
            "'/auth/mfa/disable'",
            "Scan this QR code with Microsoft Authenticator or Google Authenticator.",
            "MFA enabled successfully.",
            "MFA disabled successfully.",
            "Signed out",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, response.text)

    def test_dashboard_does_not_persist_or_render_access_tokens(self):
        response = self.client.get("/testing/dashboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn("let accessToken = null", response.text)
        self.assertNotIn('name="totp_code"', response.text)
        self.assertIn("Login → MFA Verification → Dashboard", response.text)
        self.assertNotIn("localStorage", response.text)
        self.assertNotIn("sessionStorage", response.text)
