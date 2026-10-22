from pydantic_settings import BaseSettings, SettingsConfigDict


class S(BaseSettings):
    auth_jwt_secret: str = ""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


secret = S().auth_jwt_secret
print("present:", bool(secret))
print("length:", len(secret))
print("is the old leaked value:", secret.endswith("jGrAN"))