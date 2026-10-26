"""Email/password authentication routes."""

import re
import hashlib
import secrets
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import config, security, tokens
from app.auth import crypto as totp_crypto
from app.auth import totp as totp_security
from app.auth.config import AuthConfigurationError
from app.auth.deps import assert_auth_database, get_auth_db, get_current_user
from app.auth.schemas import (
    CredentialsRequest,
    LoginRequest,
    PasswordChangeRequest,
    RegistrationResponse,
    TotpCodeRequest,
    TotpDisableRequest,
    TotpSetupResponse,
    TotpStatusResponse,
    TokenResponse,
    UserResponse,
    is_valid_email,
)
from app.models import (
    AuthEvent,
    RefreshToken,
    User,
    UserCredential,
    UserTotpSecret,
)


router = APIRouter(prefix="/auth", tags=["Auth"])
_REFRESH_COOKIE = "bfams_refresh"
_MFA_CHALLENGE_COOKIE = "bfams_mfa_challenge"
_MFA_CHALLENGE_TTL = timedelta(minutes=5)
_MFA_CHALLENGE_MAX_ATTEMPTS = 5
_DUMMY_PASSWORD_HASH = security.PASSWORD_HASHER.hash("auth-timing-normalization")
_LOGIN_FAILURES_BY_IP: dict[str, deque[datetime]] = defaultdict(deque)
_LOGIN_FAILURES_LOCK = Lock()
_LOGIN_RATE_LIMITS = ((5, timedelta(minutes=1)), (20, timedelta(hours=1)))
_LEGACY_TOTP_SECRET = re.compile(r"^[A-Z2-7]{16,}$")


@dataclass
class _MfaLoginChallenge:
    user_id: uuid.UUID
    expires_at: datetime
    client_ip: str
    attempts: int = 0


_MFA_LOGIN_CHALLENGES: dict[str, _MfaLoginChallenge] = {}
_MFA_LOGIN_CHALLENGES_LOCK = Lock()
_ALLOWED_AUTH_EVENTS = {
    "register",
    "login_success",
    "login_failed",
    "locked",
    "refresh",
    "refresh_reuse_detected",
    "logout",
    "password_changed",
    "mfa_enabled",
    "mfa_disabled",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_configured() -> None:
    try:
        config.get_jwt_secret()
    except AuthConfigurationError:
        raise HTTPException(
            status_code=503,
            detail="Authentication is not configured.",
        ) from None


def _require_mfa_encryption() -> None:
    try:
        config.get_totp_encryption_key()
    except AuthConfigurationError:
        raise HTTPException(
            status_code=503,
            detail="MFA encryption is not configured.",
        ) from None


def _load_totp_secret(configuration: UserTotpSecret) -> tuple[str, bool]:
    stored_value = configuration.secret
    if _LEGACY_TOTP_SECRET.fullmatch(stored_value):
        return stored_value, True
    return totp_crypto.decrypt_totp_secret(stored_value), False


def _challenge_key(raw_challenge: str) -> str:
    return hashlib.sha256(raw_challenge.encode("utf-8")).hexdigest()


def _issue_mfa_challenge(user_id: uuid.UUID, client_ip: str, now: datetime) -> str:
    raw_challenge = secrets.token_urlsafe(32)
    with _MFA_LOGIN_CHALLENGES_LOCK:
        for key, challenge in list(_MFA_LOGIN_CHALLENGES.items()):
            if challenge.expires_at <= now:
                _MFA_LOGIN_CHALLENGES.pop(key, None)
        _MFA_LOGIN_CHALLENGES[_challenge_key(raw_challenge)] = _MfaLoginChallenge(
            user_id=user_id,
            expires_at=now + _MFA_CHALLENGE_TTL,
            client_ip=client_ip,
        )
    return raw_challenge


def _delete_mfa_challenge(raw_challenge: str | None) -> None:
    if raw_challenge:
        with _MFA_LOGIN_CHALLENGES_LOCK:
            _MFA_LOGIN_CHALLENGES.pop(_challenge_key(raw_challenge), None)


def _set_mfa_challenge_cookie(response: Response, raw_challenge: str) -> None:
    response.set_cookie(
        key=_MFA_CHALLENGE_COOKIE,
        value=raw_challenge,
        max_age=int(_MFA_CHALLENGE_TTL.total_seconds()),
        httponly=True,
        secure=config.cookie_secure(),
        samesite="strict",
        path="/auth",
    )


def _clear_mfa_challenge_cookie(response: Response) -> None:
    response.delete_cookie(
        key=_MFA_CHALLENGE_COOKIE,
        path="/auth",
        secure=config.cookie_secure(),
        httponly=True,
        samesite="strict",
    )


def _record_event(
    db: Session,
    event_type: str,
    user_id: uuid.UUID | None,
) -> None:
    if event_type not in _ALLOWED_AUTH_EVENTS:
        raise ValueError("Invalid authentication event.")
    db.add(AuthEvent(user_id=user_id, event_type=event_type))


def _client_ip(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host


def _check_login_rate_limit(request: Request, now: datetime) -> None:
    client_ip = _client_ip(request)
    with _LOGIN_FAILURES_LOCK:
        expired = now - timedelta(hours=1)
        for ip, failures in list(_LOGIN_FAILURES_BY_IP.items()):
            while failures and failures[0] <= expired:
                failures.popleft()
            if not failures:
                _LOGIN_FAILURES_BY_IP.pop(ip, None)
        failures = _LOGIN_FAILURES_BY_IP[client_ip]
        if any(
            sum(1 for failed_at in failures if failed_at > now - window) >= limit
            for limit, window in _LOGIN_RATE_LIMITS
        ):
            raise HTTPException(
                status_code=429,
                detail="Too many login attempts. Please try again later.",
                headers={"Retry-After": "60"},
            )


def _record_login_failure(request: Request, now: datetime) -> None:
    with _LOGIN_FAILURES_LOCK:
        _LOGIN_FAILURES_BY_IP[_client_ip(request)].append(now)


def _new_refresh_token(
    db: Session,
    *,
    user_id: uuid.UUID,
    family_id: uuid.UUID | None = None,
) -> tuple[str, RefreshToken]:
    now = _now()
    raw_token = tokens.create_refresh_token()
    row = RefreshToken(
        user_id=user_id,
        family_id=family_id or uuid.uuid4(),
        token_hash=tokens.hash_refresh_token(raw_token),
        issued_at=now,
        expires_at=now + timedelta(days=config.REFRESH_TOKEN_DAYS),
    )
    db.add(row)
    db.flush()
    return raw_token, row


def _set_refresh_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=raw_token,
        max_age=config.REFRESH_TOKEN_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=config.cookie_secure(),
        samesite="strict",
        path="/auth",
    )


def _require_request_protection(request: Request) -> None:
    if request.headers.get("X-BFAMS-Auth") != "1":
        raise HTTPException(status_code=400, detail="Invalid auth request.")
    origin = request.headers.get("Origin")
    if origin is not None:
        expected_origin = str(request.base_url).rstrip("/")
        if origin != expected_origin:
            raise HTTPException(status_code=403, detail="Origin not allowed.")


def _issue_access_token(user_id: uuid.UUID) -> TokenResponse:
    raw_token, expires_in = tokens.create_access_token(str(user_id))
    return TokenResponse(access_token=raw_token, expires_in=expires_in)


def _finish_mfa_login(
    request: Request,
    response: Response,
    db: Session,
    code: str | None,
) -> TokenResponse:
    raw_challenge = request.cookies.get(_MFA_CHALLENGE_COOKIE)
    now = _now()
    if raw_challenge is None:
        raise HTTPException(status_code=401, detail="invalid credentials")

    key = _challenge_key(raw_challenge)
    with _MFA_LOGIN_CHALLENGES_LOCK:
        challenge = _MFA_LOGIN_CHALLENGES.get(key)
        if (
            challenge is None
            or challenge.expires_at <= now
            or challenge.client_ip != _client_ip(request)
        ):
            _MFA_LOGIN_CHALLENGES.pop(key, None)
            _clear_mfa_challenge_cookie(response)
            raise HTTPException(status_code=401, detail="invalid credentials")

        credential = db.execute(
            select(UserCredential)
            .where(UserCredential.user_id == challenge.user_id)
            .with_for_update()
        ).scalar_one_or_none()
        configuration = db.execute(
            select(UserTotpSecret)
            .where(
                UserTotpSecret.user_id == challenge.user_id,
                UserTotpSecret.enabled.is_(True),
            )
            .with_for_update()
        ).scalar_one_or_none()
        currently_locked = (
            credential is not None
            and credential.locked_until is not None
            and credential.locked_until > now
        )

        secret = None
        is_legacy = False
        if (
            credential is not None
            and credential.is_active
            and configuration is not None
            and not currently_locked
        ):
            try:
                secret, is_legacy = _load_totp_secret(configuration)
            except AuthConfigurationError:
                secret = None
        code_valid = (
            secret is not None
            and totp_security.verify_totp_code(secret, code)
        )
        if (
            credential is None
            or not credential.is_active
            or currently_locked
            or configuration is None
            or not code_valid
        ):
            _record_login_failure(request, now)
            if credential is not None:
                _record_event(db, "login_failed", credential.user_id)
                if not currently_locked:
                    credential.failed_attempts += 1
                    if credential.failed_attempts >= config.LOCKOUT_FAILURES:
                        credential.locked_until = now + timedelta(
                            minutes=config.LOCKOUT_MINUTES
                        )
                        _record_event(db, "locked", credential.user_id)
            challenge.attempts += 1
            exhausted = (
                challenge.attempts >= _MFA_CHALLENGE_MAX_ATTEMPTS
                or (
                    credential is not None
                    and credential.locked_until is not None
                    and credential.locked_until > now
                )
            )
            if exhausted:
                _MFA_LOGIN_CHALLENGES.pop(key, None)
                _clear_mfa_challenge_cookie(response)
            db.commit()
            raise HTTPException(status_code=401, detail="invalid credentials")

        credential.failed_attempts = 0
        credential.locked_until = None
        if is_legacy:
            configuration.secret = totp_crypto.encrypt_totp_secret(secret)
        _record_event(db, "login_success", credential.user_id)
        raw_refresh, _ = _new_refresh_token(db, user_id=credential.user_id)
        access_response = _issue_access_token(credential.user_id)
        db.commit()
        _MFA_LOGIN_CHALLENGES.pop(key, None)
        _set_refresh_cookie(response, raw_refresh)
        _clear_mfa_challenge_cookie(response)
        return access_response


@router.post(
    "/login",
    response_model=TokenResponse,
    responses={202: {"description": "Additional MFA verification required"}},
)
def login(
    body: LoginRequest,
    response: Response,
    request: Request,
    db: Session = Depends(get_auth_db),
) -> TokenResponse | JSONResponse:
    _ensure_configured()
    now = _now()
    _check_login_rate_limit(request, now)
    assert_auth_database(db)
    if body.email is None and body.password is None and body.totp_code is not None:
        return _finish_mfa_login(request, response, db, body.totp_code)
    if body.email is None or body.password is None:
        _record_login_failure(request, now)
        _record_event(db, "login_failed", None)
        db.commit()
        raise HTTPException(status_code=401, detail="invalid credentials")

    credential = None
    if is_valid_email(body.email):
        credential = db.execute(
            select(UserCredential)
            .where(UserCredential.email == body.email)
            .with_for_update()
        ).scalar_one_or_none()
    if credential is None:
        dummy_password = (
            body.password
            if 12 <= len(body.password) <= 128
            else "auth-dummy-password"
        )
        security.verify_password(dummy_password, _DUMMY_PASSWORD_HASH)
        _record_login_failure(request, now)
        _record_event(db, "login_failed", None)
        db.commit()
        raise HTTPException(status_code=401, detail="invalid credentials")

    password_in_policy = 12 <= len(body.password) <= 128
    if password_in_policy:
        valid_password, replacement_hash = security.verify_password(
            body.password,
            credential.password_hash,
        )
    else:
        security.verify_password(body.password, _DUMMY_PASSWORD_HASH)
        valid_password, replacement_hash = False, None
    currently_locked = (
        credential.locked_until is not None and credential.locked_until > now
    )
    totp_configuration = None
    totp_valid = True
    mfa_challenge_required = False
    if valid_password and credential.is_active and not currently_locked:
        totp_configuration = db.get(UserTotpSecret, credential.user_id)
        if totp_configuration is not None and totp_configuration.enabled:
            if body.totp_code is None:
                mfa_challenge_required = True
            else:
                try:
                    secret, is_legacy = _load_totp_secret(totp_configuration)
                    totp_valid = totp_security.verify_totp_code(
                        secret,
                        body.totp_code,
                    )
                    if totp_valid and is_legacy:
                        totp_configuration.secret = totp_crypto.encrypt_totp_secret(
                            secret
                        )
                except AuthConfigurationError:
                    totp_valid = False
    if (
        not credential.is_active
        or currently_locked
        or not valid_password
        or not totp_valid
    ):
        _record_login_failure(request, now)
        _record_event(db, "login_failed", credential.user_id)
        if not currently_locked:
            credential.failed_attempts += 1
            if credential.failed_attempts >= config.LOCKOUT_FAILURES:
                credential.locked_until = now + timedelta(
                    minutes=config.LOCKOUT_MINUTES
                )
                _record_event(db, "locked", credential.user_id)
        db.commit()
        raise HTTPException(status_code=401, detail="invalid credentials")

    if mfa_challenge_required:
        if replacement_hash is not None:
            credential.password_hash = replacement_hash
        raw_challenge = _issue_mfa_challenge(
            credential.user_id,
            _client_ip(request),
            now,
        )
        db.commit()
        challenge_response = JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"mfa_required": True},
        )
        _set_mfa_challenge_cookie(challenge_response, raw_challenge)
        return challenge_response

    credential.failed_attempts = 0
    credential.locked_until = None
    if replacement_hash is not None:
        credential.password_hash = replacement_hash
    _record_event(db, "login_success", credential.user_id)
    raw_refresh, _ = _new_refresh_token(db, user_id=credential.user_id)
    access_response = _issue_access_token(credential.user_id)
    db.commit()
    _set_refresh_cookie(response, raw_refresh)
    return access_response


@router.post("/login/cancel", status_code=status.HTTP_204_NO_CONTENT)
def cancel_mfa_login(request: Request) -> Response:
    _delete_mfa_challenge(request.cookies.get(_MFA_CHALLENGE_COOKIE))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_mfa_challenge_cookie(response)
    return response


@router.post("/refresh", response_model=TokenResponse)
def refresh(
    request: Request,
    response: Response,
    db: Session = Depends(get_auth_db),
) -> TokenResponse:
    _ensure_configured()
    _require_request_protection(request)
    raw_token = request.cookies.get(_REFRESH_COOKIE)
    if not raw_token:
        raise HTTPException(status_code=401, detail="Invalid refresh token.")

    assert_auth_database(db)
    now = _now()
    current = db.execute(
        select(RefreshToken)
        .where(RefreshToken.token_hash == tokens.hash_refresh_token(raw_token))
        .with_for_update()
    ).scalar_one_or_none()
    if current is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token.")

    if current.revoked_at is not None or current.replaced_by is not None:
        db.execute(
            update(RefreshToken)
            .where(
                RefreshToken.family_id == current.family_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        _record_event(db, "refresh_reuse_detected", current.user_id)
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid refresh token.")
    if current.expires_at <= now:
        raise HTTPException(status_code=401, detail="Invalid refresh token.")

    credential = db.get(UserCredential, current.user_id)
    if credential is None or not credential.is_active:
        db.execute(
            update(RefreshToken)
            .where(
                RefreshToken.family_id == current.family_id,
                RefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid refresh token.")

    next_raw, replacement = _new_refresh_token(
        db,
        user_id=current.user_id,
        family_id=current.family_id,
    )
    current.revoked_at = now
    current.replaced_by = replacement.id
    _record_event(db, "refresh", current.user_id)
    access_response = _issue_access_token(current.user_id)
    db.commit()
    _set_refresh_cookie(response, next_raw)
    return access_response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, db: Session = Depends(get_auth_db)) -> Response:
    _ensure_configured()
    _require_request_protection(request)
    raw_token = request.cookies.get(_REFRESH_COOKIE)
    if raw_token:
        assert_auth_database(db)
        current = db.execute(
            select(RefreshToken)
            .where(
                RefreshToken.token_hash == tokens.hash_refresh_token(raw_token)
            )
            .with_for_update()
        ).scalar_one_or_none()
        if current is not None:
            now = _now()
            db.execute(
                update(RefreshToken)
                .where(
                    RefreshToken.family_id == current.family_id,
                    RefreshToken.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            _record_event(db, "logout", current.user_id)
            db.commit()

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        key=_REFRESH_COOKIE,
        path="/auth",
        secure=config.cookie_secure(),
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("/me", response_model=UserResponse)
def me(user: UserResponse = Depends(get_current_user)) -> UserResponse:
    return user


@router.get("/sessions")
def list_sessions(
    request: Request,
    current_user: UserResponse = Depends(get_current_user),
    db: Session = Depends(get_auth_db),
) -> list[dict[str, str | bool]]:
    assert_auth_database(db)
    now = _now()
    current_refresh = request.cookies.get(_REFRESH_COOKIE)
    current_hash = (
        tokens.hash_refresh_token(current_refresh) if current_refresh else None
    )
    rows = db.scalars(
        select(RefreshToken)
        .where(
            RefreshToken.user_id == current_user.user_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
        )
        .order_by(RefreshToken.issued_at.desc())
    ).all()
    return [
        {
            "session_id": str(row.id),
            "issued_at": row.issued_at.isoformat(),
            "expires_at": row.expires_at.isoformat(),
            "current": row.token_hash == current_hash,
        }
        for row in rows
    ]


@router.post(
    "/sessions/{session_id}/revoke",
    status_code=status.HTTP_204_NO_CONTENT,
)
def revoke_session(
    session_id: uuid.UUID,
    current_user: UserResponse = Depends(get_current_user),
    db: Session = Depends(get_auth_db),
) -> Response:
    assert_auth_database(db)
    session = db.execute(
        select(RefreshToken)
        .where(
            RefreshToken.id == session_id,
            RefreshToken.user_id == current_user.user_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Resource not found.")

    now = _now()
    revoked = db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == current_user.user_id,
            RefreshToken.family_id == session.family_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    if revoked.rowcount:
        _record_event(db, "logout", current_user.user_id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
def logout_all(
    request: Request,
    current_user: UserResponse = Depends(get_current_user),
    db: Session = Depends(get_auth_db),
) -> Response:
    _require_request_protection(request)
    assert_auth_database(db)
    now = _now()
    family_ids = set(
        db.scalars(
            select(RefreshToken.family_id)
            .where(
                RefreshToken.user_id == current_user.user_id,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > now,
            )
            .with_for_update()
        ).all()
    )
    db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == current_user.user_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    for _ in family_ids or {None}:
        _record_event(db, "logout", current_user.user_id)
    db.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        key=_REFRESH_COOKIE,
        path="/auth",
        secure=config.cookie_secure(),
        httponly=True,
        samesite="strict",
    )
    return response


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: PasswordChangeRequest,
    response: Response,
    current_user: UserResponse = Depends(get_current_user),
    db: Session = Depends(get_auth_db),
) -> Response:
    _ensure_configured()
    assert_auth_database(db)
    credential = db.execute(
        select(UserCredential)
        .where(
            UserCredential.user_id == current_user.user_id,
            UserCredential.is_active.is_(True),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if credential is None:
        raise HTTPException(status_code=401, detail="Invalid authentication credentials.")
    valid, _ = security.verify_password(
        body.current_password,
        credential.password_hash,
    )
    if not valid:
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    try:
        security.validate_password(body.new_password, credential.email)
    except security.PasswordPolicyError:
        raise HTTPException(
            status_code=422,
            detail="New password does not meet policy.",
        ) from None

    now = _now()
    credential.password_hash = security.hash_password(
        body.new_password,
        credential.email,
    )
    credential.password_changed_at = now
    db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == current_user.user_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    _record_event(db, "password_changed", current_user.user_id)
    db.commit()
    response.delete_cookie(
        key=_REFRESH_COOKIE,
        path="/auth",
        secure=config.cookie_secure(),
        httponly=True,
        samesite="strict",
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post("/mfa/setup", response_model=TotpSetupResponse)
def setup_mfa(
    current_user: UserResponse = Depends(get_current_user),
    db: Session = Depends(get_auth_db),
) -> TotpSetupResponse:
    assert_auth_database(db)
    _require_mfa_encryption()
    db.execute(
        select(User.id)
        .where(User.id == current_user.user_id)
        .with_for_update()
    ).scalar_one()
    configuration = db.execute(
        select(UserTotpSecret)
        .where(UserTotpSecret.user_id == current_user.user_id)
        .with_for_update()
    ).scalar_one_or_none()
    if configuration is not None and configuration.enabled:
        raise HTTPException(status_code=409, detail="MFA is already enabled.")

    secret = totp_security.generate_totp_secret()
    encrypted_secret = totp_crypto.encrypt_totp_secret(secret)
    if configuration is None:
        configuration = UserTotpSecret(
            user_id=current_user.user_id,
            secret=encrypted_secret,
            enabled=False,
        )
        db.add(configuration)
    else:
        configuration.secret = encrypted_secret
    db.commit()
    return TotpSetupResponse(
        secret=secret,
        otpauth_uri=totp_security.create_totp_uri(secret, current_user.email),
    )


@router.post("/mfa/verify", status_code=status.HTTP_204_NO_CONTENT)
def verify_mfa(
    body: TotpCodeRequest,
    current_user: UserResponse = Depends(get_current_user),
    db: Session = Depends(get_auth_db),
) -> Response:
    assert_auth_database(db)
    _require_mfa_encryption()
    configuration = db.execute(
        select(UserTotpSecret)
        .where(UserTotpSecret.user_id == current_user.user_id)
        .with_for_update()
    ).scalar_one_or_none()
    if configuration is None or configuration.enabled:
        raise HTTPException(status_code=400, detail="Invalid verification code.")
    try:
        secret, is_legacy = _load_totp_secret(configuration)
    except AuthConfigurationError:
        raise HTTPException(
            status_code=400,
            detail="Invalid verification code.",
        ) from None
    if not totp_security.verify_totp_code(secret, body.code):
        raise HTTPException(status_code=400, detail="Invalid verification code.")

    if is_legacy:
        configuration.secret = totp_crypto.encrypt_totp_secret(secret)
    configuration.enabled = True
    _record_event(db, "mfa_enabled", current_user.user_id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/mfa/status", response_model=TotpStatusResponse)
def mfa_status(
    current_user: UserResponse = Depends(get_current_user),
    db: Session = Depends(get_auth_db),
) -> TotpStatusResponse:
    assert_auth_database(db)
    configuration = db.get(UserTotpSecret, current_user.user_id)
    return TotpStatusResponse(
        enabled=configuration is not None and configuration.enabled
    )


@router.post("/mfa/disable", status_code=status.HTTP_204_NO_CONTENT)
def disable_mfa(
    body: TotpDisableRequest,
    current_user: UserResponse = Depends(get_current_user),
    db: Session = Depends(get_auth_db),
) -> Response:
    assert_auth_database(db)
    _require_mfa_encryption()
    credential = db.execute(
        select(UserCredential)
        .where(
            UserCredential.user_id == current_user.user_id,
            UserCredential.is_active.is_(True),
        )
        .with_for_update()
    ).scalar_one_or_none()
    configuration = db.execute(
        select(UserTotpSecret)
        .where(
            UserTotpSecret.user_id == current_user.user_id,
            UserTotpSecret.enabled.is_(True),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if (
        credential is None
        or configuration is None
        or not security.verify_password(
            body.password,
            credential.password_hash,
        )[0]
    ):
        raise HTTPException(status_code=400, detail="MFA verification failed.")

    try:
        secret, is_legacy = _load_totp_secret(configuration)
    except AuthConfigurationError:
        raise HTTPException(
            status_code=400,
            detail="MFA verification failed.",
        ) from None
    if not totp_security.verify_totp_code(secret, body.code):
        raise HTTPException(status_code=400, detail="MFA verification failed.")
    if is_legacy:
        configuration.secret = totp_crypto.encrypt_totp_secret(secret)
    configuration.enabled = False
    _record_event(db, "mfa_disabled", current_user.user_id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/register",
    response_model=RegistrationResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(
    body: CredentialsRequest,
    db: Session = Depends(get_auth_db),
) -> RegistrationResponse:
    _ensure_configured()
    if not config.registration_enabled():
        raise HTTPException(status_code=403, detail="Registration is disabled.")
    if not is_valid_email(body.email):
        raise HTTPException(status_code=422, detail="Invalid registration details.")
    try:
        security.validate_password(body.password, body.email)
    except security.PasswordPolicyError:
        raise HTTPException(
            status_code=422,
            detail="Password does not meet policy.",
        ) from None

    assert_auth_database(db)
    user = User()
    db.add(user)
    db.flush()
    credential = UserCredential(
        user_id=user.id,
        email=body.email,
        password_hash=security.hash_password(body.password, body.email),
        failed_attempts=0,
        locked_until=None,
        is_active=True,
        password_changed_at=_now(),
    )
    db.add(credential)
    _record_event(db, "register", user.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Unable to register with provided details.",
        ) from None
    return RegistrationResponse(user_id=user.id)
