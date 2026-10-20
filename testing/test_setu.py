from app.integrations.setu.client import SetuClient
from app.integrations.setu.config import SetuSettings

settings = SetuSettings()

print("Client ID:", settings.client_id)
print("Product Instance ID:", settings.product_instance_id)
print("Has Credentials:", settings.has_credentials)

client = SetuClient(settings)

token = client._get_access_token()

print("TOKEN:")
print(token)