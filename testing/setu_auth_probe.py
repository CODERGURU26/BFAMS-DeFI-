import re
import uuid
from urllib.parse import urlparse

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class S(BaseSettings):
    setu_client_id: str = ""
    setu_client_secret: str = ""
    setu_product_instance_id: str = ""
    setu_token_url: str = "https://uat.setu.co/api/v2/auth/token"
    setu_fiu_base_url: str = "https://fiu-sandbox.setu.co"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


s = S()

for url in (s.setu_token_url, s.setu_fiu_base_url):
    host = urlparse(url).hostname or ""
    if not host.endswith(".setu.co"):
        raise SystemExit("refusing to send credentials to a non-Setu host")

print("token host:", urlparse(s.setu_token_url).hostname)
print("fiu host:  ", urlparse(s.setu_fiu_base_url).hostname)
for name, value in (
    ("SETU_CLIENT_ID", s.setu_client_id),
    ("SETU_CLIENT_SECRET", s.setu_client_secret),
    ("SETU_PRODUCT_INSTANCE_ID", s.setu_product_instance_id),
):
    print(name, {
        "present": bool(value),
        "length": len(value),
        "uuid_format": bool(UUID_RE.match(value)),
        "space_or_quote": any(c in value for c in " \"'"),
    })
if s.setu_client_id and s.setu_client_id == s.setu_product_instance_id:
    print("WARNING: client id and product instance id are identical")


def show(label, r):
    try:
        body = r.json()
    except ValueError:
        body = {}
    msg = ""
    if isinstance(body, dict):
        msg = f"{body.get('errorCode') or body.get('code') or ''} " \
              f"{body.get('errorMsg') or body.get('message') or ''}"
    print(f"{label} -> {r.status_code} {msg.strip()[:200]}")


tok = httpx.post(
    s.setu_token_url,
    json={"clientID": s.setu_client_id, "secret": s.setu_client_secret},
    timeout=30,
)
show("token request", tok)
if tok.status_code != 200:
    raise SystemExit("stopping: no token")
data = tok.json().get("data") or {}
token = data.get("token", "")
print("expiresIn:", data.get("expiresIn"))

dummy = str(uuid.uuid4())
base = s.setu_fiu_base_url
bearer = {
    "Authorization": f"Bearer {token}",
    "x-product-instance-id": s.setu_product_instance_id,
}
legacy = {
    "x-client-id": s.setu_client_id,
    "x-client-secret": s.setu_client_secret,
    "x-product-instance-id": s.setu_product_instance_id,
}
for label, headers in (("bearer", bearer), ("legacy headers", legacy)):
    show(f"[{label}] GET /v2/consents/<random>",
         httpx.get(f"{base}/v2/consents/{dummy}", headers=headers, timeout=30))
    show(f"[{label}] GET /v2/consents/<random>/fetch/status",
         httpx.get(f"{base}/v2/consents/{dummy}/fetch/status",
                   headers=headers, timeout=30))