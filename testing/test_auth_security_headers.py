import unittest

from fastapi.testclient import TestClient

from main import app


class SecurityHeaderTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app, base_url="https://testserver")

    def tearDown(self):
        self.client.close()

    def test_security_headers_are_present_on_application_and_docs(self):
        expected = {
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
            "referrer-policy": "strict-origin-when-cross-origin",
            "permissions-policy": (
                "accelerometer=(), camera=(), geolocation=(), microphone=()"
            ),
        }
        for path in ("/health", "/docs", "/openapi.json", "/not-found"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404 if path == "/not-found" else 200)
                headers = {name.lower(): value for name, value in response.headers.items()}
                for name, value in expected.items():
                    self.assertEqual(headers[name], value)
                self.assertIn("content-security-policy", headers)
                self.assertIn("default-src 'self'", headers["content-security-policy"])

    def test_swagger_documentation_remains_available(self):
        response = self.client.get("/docs")
        self.assertEqual(response.status_code, 200)
        self.assertIn("swagger-ui", response.text.lower())
        self.assertIn("cdn.jsdelivr.net", response.headers["content-security-policy"])


if __name__ == "__main__":
    unittest.main()
