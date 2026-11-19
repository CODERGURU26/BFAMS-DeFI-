from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str
    db_user: str
    db_password: str
    test_db_name: str | None = None
    app_database_target: Literal["test", "main"] = "test"

    @property
    def app_database_name(self) -> str:
        if self.app_database_target == "test":
            if not self.test_db_name:
                raise RuntimeError("TEST_DB_NAME is not set")
            return self.test_db_name
        if self.db_name != "BFAMS-DeFI":
            raise RuntimeError(
                "APP_DATABASE_TARGET=main is restricted to BFAMS-DeFI staging."
            )
        return self.db_name

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()

database_url = URL.create(
    drivername="postgresql+psycopg",
    username=settings.db_user,
    password=settings.db_password,
    host=settings.db_host,
    port=settings.db_port,
    database=settings.db_name,
)

engine = create_engine(database_url, pool_pre_ping=True)


def test_database_connection():
    with engine.connect() as connection:
        result = connection.execute(
            text("SELECT current_database(), current_user")
        )
        return result.fetchone()


def get_test_engine():
    if not settings.test_db_name:
        raise RuntimeError("TEST_DB_NAME is not set")
    if settings.test_db_name == settings.db_name:
        raise RuntimeError("Test database must differ from the main database")
    if not settings.test_db_name.endswith("-test"):
        raise RuntimeError("Test database name must end with '-test'")

    test_url = URL.create(
        drivername="postgresql+psycopg",
        username=settings.db_user,
        password=settings.db_password,
        host=settings.db_host,
        port=settings.db_port,
        database=settings.test_db_name,
    )
    return create_engine(test_url, pool_pre_ping=True)


def get_app_engine():
    """Return the explicitly selected application database engine."""
    if settings.app_database_target == "main":
        target_name = settings.app_database_name
        if target_name != "BFAMS-DeFI":
            raise RuntimeError("Application main target is not the staging database.")
        return engine
    return get_test_engine()