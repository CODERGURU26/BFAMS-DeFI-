# test_setu.py

from app.integrations.setu.client import SetuClient
from app.integrations.setu.config import settings

client = SetuClient(settings)

print(client._get_access_token())