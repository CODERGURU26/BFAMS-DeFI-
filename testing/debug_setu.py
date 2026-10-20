from app.integrations.setu.client import SetuClient
from app.integrations.setu.config import SetuSettings

settings = SetuSettings()

print("=" * 60)
print("SETU CONFIG")
print("=" * 60)

print("CLIENT_ID:", settings.client_id.get_secret_value())
print("PRODUCT_INSTANCE_ID:", settings.product_instance_id.get_secret_value())
print("VUA:", settings.vua.get_secret_value())
print("HAS_CREDENTIALS:", settings.has_credentials)

print("\n")

with SetuClient(settings) as client:

    print("=" * 60)
    print("TOKEN TEST")
    print("=" * 60)

    token = client._get_access_token()

    print("TOKEN GENERATED:", bool(token))

    print("\n")

    print("=" * 60)
    print("FIPS TEST")
    print("=" * 60)

    fips = client.request(
        "GET",
        "/v2/fips"
    )

    print("FIPS COUNT:", len(fips.get("data", [])))

    print("\n")

    print("=" * 60)
    print("CONSENT TEST")
    print("=" * 60)

    payload = {
        "vua": settings.vua.get_secret_value(),
        "consentMode": "STORE",
        "consentDurationUnit": "MONTH",
        "consentDurationValue": 12,
        "dataRangeUnit": "MONTH",
        "dataRangeValue": 6,
        "fiTypes": ["DEPOSIT"],
        "purpose": {
            "code": "101",
            "refUri": "https://api.rebit.org.in/aa/purpose/101.xml",
            "text": "Financial assessment"
        }
    }

    try:
        consent = client.request(
            "POST",
            "/consents",
            json=payload,
        )

        print("CONSENT SUCCESS")
        print(consent)

    except Exception as e:
        print("CONSENT FAILED")
        print(type(e).__name__)
        print(str(e))