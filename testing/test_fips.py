# testing/test_fips.py

from app.integrations.setu.client import SetuClient
from app.integrations.setu.config import SetuSettings

settings = SetuSettings()

with SetuClient(settings) as client:
    response = client.request(
        "GET",
        "/v2/fips"
    )

    print(response)