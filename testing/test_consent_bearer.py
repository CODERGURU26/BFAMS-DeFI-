from app.integrations.setu.client import SetuClient
from app.integrations.setu.config import SetuSettings

settings = SetuSettings()

with SetuClient(settings) as client:

    token = client._get_access_token()

    payload = {
        "vua": settings.vua.get_secret_value(),
        "consentMode": "STORE",
        "consentDurationUnit": "MONTH",
        "consentDurationValue": 12,
        "dataRangeUnit": "MONTH",
        "dataRangeValue": 6,
        "fiTypes": ["DEPOSIT"]
    }

    response = client._send_request(
        "POST",
        "https://fiu-sandbox.setu.co/consents",
        headers={
            "Authorization": f"Bearer {token}",
            "x-product-instance-id": settings.product_instance_id.get_secret_value(),
        },
        json=payload,
    )

    print(response)