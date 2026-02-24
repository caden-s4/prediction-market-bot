import os, base64
from dotenv import load_dotenv
load_dotenv()

secret = os.getenv('KALSHI_API_SECRET', '')
key = os.getenv('KALSHI_API_KEY', '')

print("API key length:", len(key))
print("Secret length: ", len(secret))
print("Secret starts with:", repr(secret[:40]))

pem = secret.strip().startswith("-----BEGIN")
print("Is PEM key:", pem)

try:
    base64.b64decode(secret, validate=True)
    print("Is valid base64: YES")
except Exception:
    print("Is valid base64: NO")
