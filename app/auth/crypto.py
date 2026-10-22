"""Authenticated encryption for TOTP secrets."""

from cryptography.fernet import Fernet, InvalidToken

from app.auth.config import AuthConfigurationError, get_totp_encryption_key


def encrypt_totp_secret(secret: str) -> str:
    key = get_totp_encryption_key()
    try:
        return Fernet(key).encrypt(secret.encode("utf-8")).decode("ascii")
    except (UnicodeEncodeError, ValueError):
        raise AuthConfigurationError("MFA encryption failed.") from None


def decrypt_totp_secret(ciphertext: str) -> str:
    key = get_totp_encryption_key()
    try:
        plaintext = Fernet(key).decrypt(ciphertext.encode("ascii"))
        return plaintext.decode("utf-8")
    except (InvalidToken, UnicodeEncodeError, UnicodeDecodeError, ValueError):
        raise AuthConfigurationError("MFA secret could not be decrypted.") from None
