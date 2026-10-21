"""Environment-only settings for the Dhan integration."""

import os

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class DhanSettings(BaseSettings):
    client_id: SecretStr = Field(default=SecretStr(""), validation_alias="DHAN_CLIENT_ID")
    access_token: SecretStr = Field(
        default=SecretStr(""), validation_alias="DHAN_ACCESS_TOKEN"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


def demo_sync_enabled() -> bool:
    return os.environ.get("DEMO_SYNC_ENABLED", "").strip().lower() == "true"


def dhan_credentials() -> tuple[str, str]:
    settings = DhanSettings()
    return (
        settings.client_id.get_secret_value(),
        settings.access_token.get_secret_value(),
    )
