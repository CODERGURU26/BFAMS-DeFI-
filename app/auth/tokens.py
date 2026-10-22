"""JWT access-token and opaque refresh-token primitives."""

import hashlib
import math
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from jwt import InvalidTokenError

from app.auth.config import (
    ACCESS_TOKEN_MINUTES,
    ISSUER,
    AuthConfigurationError,
    get_jwt_secret,
)


class AccessTokenError(ValueError):
    """Raised for invalid access tokens."""


def _utc_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def create_access_token(
    subject: str,
    *,
    now: datetime | None = None,
) -> tuple[str, int]:
    if not subject:
        raise ValueError("Token subject is required.")
    issued_at = _utc_now(now)
    expires_at = issued_at + timedelta(minutes=ACCESS_TOKEN_MINUTES)
    claims = {
        "sub": subject,
        "iss": ISSUER,
        "iat": issued_at,
        "exp": expires_at,
        "jti": str(uuid.uuid4()),
        "typ": "access",
    }
    return jwt.encode(claims, get_jwt_secret(), algorithm="HS256"), (
        ACCESS_TOKEN_MINUTES * 60
    )


def decode_access_token(
    token: str,
    *,
    password_changed_at: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        claims = jwt.decode(
            token,
            get_jwt_secret(),
            algorithms=["HS256"],
            issuer=ISSUER,
            options={
                "require": ["sub", "iss", "iat", "exp"],
                "verify_exp": False,
            },
            leeway=0,
        )
    except AuthConfigurationError:
        raise
    except InvalidTokenError as exc:
        raise AccessTokenError("Invalid access token.") from exc

    if claims.get("typ") != "access":
        raise AccessTokenError("Invalid access token.")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise AccessTokenError("Invalid access token.")
    expiration = claims.get("exp")
    if (
        isinstance(expiration, bool)
        or not isinstance(expiration, (int, float))
        or not math.isfinite(expiration)
        or expiration <= _utc_now(now).timestamp()
    ):
        raise AccessTokenError("Invalid access token.")
    if password_changed_at is not None:
        changed_at = _utc_now(password_changed_at)
        issued_at = claims.get("iat")
        if (
            isinstance(issued_at, bool)
            or not isinstance(issued_at, (int, float))
            or not math.isfinite(issued_at)
            or math.floor(issued_at) < math.floor(changed_at.timestamp())
        ):
            raise AccessTokenError("Invalid access token.")
    return claims


def create_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
