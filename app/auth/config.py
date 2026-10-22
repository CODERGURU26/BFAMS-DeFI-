"""Authentication configuration kept separate from application/database settings."""

import os


ACCESS_TOKEN_MINUTES = 15
REFRESH_TOKEN_DAYS = 7
ISSUER = "bfams"

LOCKOUT_FAILURES = 5
LOCKOUT_MINUTES = 15

MIN_JWT_SECRET_LENGTH = 32


class AuthConfigurationError(RuntimeError):
    """Raised when authentication cannot be safely configured."""


def get_jwt_secret() -> str:
    secret = os.environ.get("AUTH_JWT_SECRET", "")
    if len(secret) < MIN_JWT_SECRET_LENGTH:
        raise AuthConfigurationError("Authentication is not configured.")
    return secret


def registration_enabled() -> bool:
    return os.environ.get("AUTH_REGISTRATION_ENABLED", "false").strip().lower() == "true"


def cookie_secure() -> bool:
    return os.environ.get("AUTH_COOKIE_SECURE", "true").strip().lower() == "true"
