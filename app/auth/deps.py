"""Authentication dependencies using the isolated test database."""

from datetime import datetime, timezone
from functools import lru_cache
from typing import Generator
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth.config import AuthConfigurationError
from app.auth.schemas import UserResponse
from app.auth.tokens import AccessTokenError, decode_access_token
from app.models import RefreshToken, User, UserCredential
from database import get_test_engine


_bearer = HTTPBearer(auto_error=False)


@lru_cache(maxsize=1)
def _auth_engine():
    return get_test_engine()


def get_auth_db() -> Generator[Session, None, None]:
    session = Session(_auth_engine())
    try:
        yield session
    finally:
        session.close()


def assert_auth_database(db: Session) -> None:
    database_name = db.execute(text("SELECT current_database()")).scalar_one()
    if database_name != "BFAMS-DeFI-test":
        raise RuntimeError("Authentication requires BFAMS-DeFI-test.")


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Invalid authentication credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_auth_db),
) -> UserResponse:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    try:
        claims = decode_access_token(credentials.credentials)
        user_id = UUID(claims["sub"])
        assert_auth_database(db)
        session_id = claims.get("sid")
        if session_id is not None:
            if not isinstance(session_id, str):
                raise _unauthorized()
            session_uuid = UUID(session_id)
            active_session = db.scalar(
                select(RefreshToken.id)
                .where(
                    RefreshToken.user_id == user_id,
                    RefreshToken.family_id == session_uuid,
                    RefreshToken.revoked_at.is_(None),
                    RefreshToken.expires_at > datetime.now(timezone.utc),
                )
                .limit(1)
            )
            if active_session is None:
                raise _unauthorized()
        credential = db.execute(
            select(UserCredential)
            .join(User, User.id == UserCredential.user_id)
            .where(
                User.id == user_id,
                UserCredential.is_active.is_(True),
            )
        ).scalar_one_or_none()
        if credential is None:
            raise _unauthorized()
        decode_access_token(
            credentials.credentials,
            password_changed_at=credential.password_changed_at,
        )
    except (AuthConfigurationError, AccessTokenError, ValueError):
        raise _unauthorized() from None
    return UserResponse(user_id=credential.user_id, email=credential.email)
