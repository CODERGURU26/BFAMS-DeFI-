"""Optional configuration for the Setu Account Aggregator integration."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class SetuSettings(BaseSettings):
    client_id: SecretStr | None = None
    client_secret: SecretStr | None = None
    product_instance_id: SecretStr | None = None
    environment: str = "sandbox"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SETU_",
        extra="ignore",
    )

    @property
    def has_credentials(self) -> bool:
        return all(
            (
                self.client_id is not None,
                self.client_secret is not None,
                self.product_instance_id is not None,
            )
        )
