import os
from py_clob_client.client import ClobClient
from dotenv import load_dotenv

load_dotenv()

PK = os.getenv("PK")

if not PK:
    print("❌ ERROR: No se encontró la variable de entorno PK en el archivo .env")
    exit(1)

client = ClobClient(
    "https://clob.polymarket.com",
    key=PK,
    chain_id=137,
)

creds = client.create_or_derive_api_creds()
print(f"\n✅ Credenciales generadas:")
print(f"CLOB_API_KEY={creds.api_key}")
print(f"CLOB_SECRET={creds.api_secret}")
print(f"CLOB_PASSPHRASE={creds.api_passphrase}")
