import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict


class S(BaseSettings):
    setu_client_id: str = ""
    setu_client_secret: str = ""
    setu_product_instance_id: str = ""
    setu_token_url: str = "https://uat.setu.co/api/v2/auth/token"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


s = S()
print("variables present:", {
    "SETU_CLIENT_ID": bool(s.setu_client_id),
    "SETU_CLIENT_SECRET": bool(s.setu_client_secret),
    "SETU_PRODUCT_INSTANCE_ID": bool(s.setu_product_instance_id),
})
if not (s.setu_client_id and s.setu_client_secret):
    raise SystemExit("client id or secret missing in .env")

r = httpx.post(
    s.setu_token_url,
    json={"clientID": s.setu_client_id, "secret": s.setu_client_secret},
    timeout=30,
)
print("status:", r.status_code)
try:
    body = r.json()
except ValueError:
    body = None
if isinstance(body, dict):
    print("top-level keys:", sorted(body))
    data = body.get("data")
    if isinstance(data, dict):
        print("data keys:", sorted(data))