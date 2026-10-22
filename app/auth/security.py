"""Password hashing and validation helpers."""

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError


PASSWORD_HASHER = PasswordHasher(type=Type.ID)
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128


class PasswordPolicyError(ValueError):
    """Raised when a password does not satisfy the auth policy."""


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_password(password: str, email: str) -> None:
    normalized_email = normalize_email(email)
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError("Password does not meet policy.")
    if password.casefold() == normalized_email.casefold():
        raise PasswordPolicyError("Password does not meet policy.")


def hash_password(password: str, email: str) -> str:
    validate_password(password, email)
    return PASSWORD_HASHER.hash(password)


def verify_password(password: str, encoded_hash: str) -> tuple[bool, str | None]:
    try:
        verified = PASSWORD_HASHER.verify(encoded_hash, password)
    except (InvalidHashError, VerificationError):
        return False, None
    if not verified:
        return False, None
    updated_hash = (
        PASSWORD_HASHER.hash(password)
        if PASSWORD_HASHER.check_needs_rehash(encoded_hash)
        else None
    )
    return True, updated_hash
