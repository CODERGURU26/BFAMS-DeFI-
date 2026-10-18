import sys
from datetime import datetime, timezone

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict


class S(BaseSettings):
    setu_client_id: str = ""
    setu_client_secret: str = ""
    setu_product_instance_id: str = ""
    setu_test_vua: str = ""
    setu_token_url: str = "https://uat.setu.co/api/v2/auth/token"
    setu_fiu_base_url: str = "https://fiu-sandbox.setu.co"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


s = S()


def auth_headers():
    needed = ("setu_client_id", "setu_client_secret",
              "setu_product_instance_id", "setu_test_vua")
    missing = [n.upper() for n in needed if not getattr(s, n)]
    if missing:
        raise SystemExit(f"missing in .env: {missing}")
    tok = httpx.post(
        s.setu_token_url,
        json={"clientID": s.setu_client_id, "secret": s.setu_client_secret},
        timeout=30,
    )
    if tok.status_code != 200:
        raise SystemExit(f"token request failed: {tok.status_code}")
    return {
        "Authorization": f"Bearer {tok.json()['data']['token']}",
        "x-product-instance-id": s.setu_product_instance_id,
        "Content-Type": "application/json",
    }


def call(method, paths, **kwargs):
    headers = auth_headers()
    for path in paths:
        r = httpx.request(method, s.setu_fiu_base_url + path,
                          headers=headers, timeout=30, **kwargs)
        print(method, path, "->", r.status_code)
        if r.status_code != 404:
            break
    try:
        return r, r.json()
    except ValueError:
        return r, {}


if len(sys.argv) >= 3 and sys.argv[1] == "status":
    cid = sys.argv[2]
    r, out = call("GET", [f"/v2/consents/{cid}", f"/consents/{cid}"])
    if r.status_code == 200:
        linked = out.get("accountsLinked")
        print("status:", out.get("status"))
        print("accounts linked:", len(linked) if isinstance(linked, list) else linked)
    else:
        print("error:", out.get("errorCode"), out.get("errorMsg"))
else:
        body = {
        "consentDuration": {"unit": "MONTH", "value": "1"},
        "consentMode": "STORE",
        "fetchType": "ONETIME",
        "consentTypes": ["TRANSACTIONS", "SUMMARY"],
        "fiTypes": ["DEPOSIT"],
        "vua": s.setu_test_vua,
        "dataRange": {
            "from": "2020-04-01T00:00:00Z",
            "to": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "dataLife": {"unit": "MONTH", "value": 1},
        "frequency": {"unit": "DAILY", "value": 1},
        "redirectUrl": "https://setu.co/",
        "context": [{"key": "purposeCode", "value": "105"}],
    }
    r, out = call("POST", ["/v2/consents", "/consents"], json=body)
    if r.status_code in (200, 201):
        print("consent id:", out.get("id"))
        print("status:", out.get("status"))
        print("open this URL in your browser:", out.get("url"))
    else:
        print("error:", out.get("errorCode"), out.get("errorMsg"),
              "| traceId:", out.get("traceId"))