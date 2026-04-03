from dotenv import load_dotenv
import os
load_dotenv()

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from py_clob_client.constants import POLYGON

creds = ApiCreds(
    api_key=os.getenv("CLOB_API_KEY"),
    api_secret=os.getenv("CLOB_SECRET"),
    api_passphrase=os.getenv("CLOB_PASSPHRASE")
)

for sig_type in [0, 1, 2]:
    try:
        client = ClobClient(
            "https://clob.polymarket.com",
            key=os.getenv("PK"),
            chain_id=POLYGON,
            creds=creds,
            funder=os.getenv("TRADER_ADDRESS"),
            signature_type=sig_type
        )
        print(f"signature_type={sig_type} → Ping:", client.get_ok())
    except Exception as e:
        print(f"signature_type={sig_type} → ERROR: {e}")
