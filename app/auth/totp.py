"""RFC 6238 TOTP helpers."""

import binascii
import re

import pyotp


_CODE_PATTERN = re.compile(r"^[0-9]{6}$")
_TOTP_INTERVAL_SECONDS = 30
_TOTP_VALID_WINDOW = 1


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def create_totp_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret, interval=_TOTP_INTERVAL_SECONDS, digits=6).provisioning_uri(
        name=email,
        issuer_name="BFAMS-DeFi",
    )


def verify_totp_code(secret: str, code: str | None) -> bool:
    if code is None or _CODE_PATTERN.fullmatch(code) is None:
        return False
    totp = pyotp.TOTP(secret, interval=_TOTP_INTERVAL_SECONDS, digits=6)
    try:
        return totp.verify(code, valid_window=_TOTP_VALID_WINDOW)
    except (binascii.Error, ValueError):
        return False
