# testing/show_headers.py

from app.integrations.setu.config import SetuSettings

s = SetuSettings()

print("CLIENT_ID")
print(s.client_id.get_secret_value())

print()

print("PRODUCT_INSTANCE_ID")
print(s.product_instance_id.get_secret_value())