
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str
    db_user: str
    db_password: str

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

    