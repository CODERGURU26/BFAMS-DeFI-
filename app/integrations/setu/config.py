"""Optional configuration for the Setu Account Aggregator integration."""

from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SetuSettings(BaseSettings):
    client_id: SecretStr | None = None
    client_secret: SecretStr | None = None
    product_instance_id: SecretStr | None = None
    vua: SecretStr | None = None
    environment: str = "sandbox"
    session_poll_interval_seconds: float = Field(default=1.0, gt=0)
    session_poll_max_attempts: int = Field(default=30, gt=0)
    session_poll_timeout_seconds: float = Field(default=30.0, gt=0)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SETU_",
        extra="ignore",
    )

    @field_validator(
        "client_id",
        "client_secret",
        "product_instance_id",
        "vua",
        mode="before",
    )
    @classmethod
    def strip_secret_configuration(cls, value: Any) -> Any:
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @property
    def has_credentials(self) -> bool:
        return all(
            (
                self.client_id is not None
                and bool(self.client_id.get_secret_value()),
                self.client_secret is not None
                and bool(self.client_secret.get_secret_value()),
                self.product_instance_id is not None
                and bool(self.product_instance_id.get_secret_value()),
            )
        )
