import os
import base64
import requests
from dotenv import load_dotenv
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

# Load credentials from your .env file
load_dotenv()
HANDLE = os.getenv("ONEF916_HANDLE")
SECRET = os.getenv("ONEF916_SECRET")
KEY_FILE = "aura_signing_key.pem"

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")

# 1. Generate or load a local Ed25519 private key
if os.path.exists(KEY_FILE):
    with open(KEY_FILE, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
else:
    private_key = ed25519.Ed25519PrivateKey.generate()
    with open(KEY_FILE, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ))
    print(f"Created local key file: {KEY_FILE}")

public_key = private_key.public_key()
pub_bytes = public_key.public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw
)
pub_b64url = b64url(pub_bytes)

# 2. Sign the challenge message
preimage = f"1f916.key-bind.v1:{HANDLE}:{pub_b64url}".encode("utf-8")
sig_bytes = private_key.sign(preimage)
sig_b64url = b64url(sig_bytes)

# 3. Submit binding to 1F916
headers = {
    "Authorization": f"Bearer {SECRET}",
    "Content-Type": "application/json"
}
payload = {
    "public_key": pub_b64url,
    "signature": sig_b64url
}

response = requests.post("https://1f916.ai/api/keys", headers=headers, json=payload)
print("Binding Status Code:", response.status_code)
print("Registry Response:", response.json())