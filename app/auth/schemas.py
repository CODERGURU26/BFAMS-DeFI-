"""Request and response schemas for password authentication."""

import re
import uuid

from pydantic import BaseModel, field_validator

from app.auth.security import normalize_email


_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class CredentialsRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def normalize(cls, value: str) -> str:
        return normalize_email(value)


def is_valid_email(value: str) -> bool:
    return len(value) <= 320 and _EMAIL_PATTERN.fullmatch(value) is not None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    user_id: uuid.UUID
    email: str


class RegistrationResponse(BaseModel):
    user_id: uuid.UUID
