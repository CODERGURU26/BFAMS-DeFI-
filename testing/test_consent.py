# testing/test_consent.py

from app.integrations.setu.client import SetuClient
from app.integrations.setu.config import SetuSettings

settings = SetuSettings()

payload = {
    "vua": settings.vua.get_secret_value(),
    "consentDurationUnit": "MONTH",
    "consentDurationValue": 12,
    "dataRangeUnit": "MONTH",
    "dataRangeValue": 6,
    "consentMode": "STORE",
    "purpose": {
        "code": "101",
        "refUri": "https://api.rebit.org.in/aa/purpose/101.xml",
        "text": "Financial assessment"
    },
    "fiTypes": ["DEPOSIT"]
}

with SetuClient(settings) as client:
    response = client.request(
        "POST",
        "/consents",
        json=payload,
    )

    print(response)