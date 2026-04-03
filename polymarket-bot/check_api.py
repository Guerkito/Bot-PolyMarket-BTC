import os
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from py_clob_client.constants import POLYGON
from dotenv import load_dotenv

load_dotenv("polymarket-bot/.env")

PK = os.getenv("PK")
FUNDER = os.getenv("TRADER_ADDRESS")
CLOB_API_KEY = os.getenv("CLOB_API_KEY")
CLOB_SECRET = os.getenv("CLOB_SECRET")
CLOB_PASSPHRASE = os.getenv("CLOB_PASSPHRASE")

CLOB_API = "https://clob.polymarket.com"

def check_api_key():
    creds = ApiCreds(api_key=CLOB_API_KEY, api_secret=CLOB_SECRET, api_passphrase=CLOB_PASSPHRASE)
    client = ClobClient(CLOB_API, key=PK, chain_id=POLYGON, creds=creds, funder=FUNDER, signature_type=0)
    
    try:
        # get_api_key should return info about the key
        # If it returns info belonging to a DIFFERENT wallet, that's the problem
        resp = client.get_api_key()
        print(f"API Key Info: {resp}")
    except Exception as e:
        print(f"Error checking API Key: {e}")

if __name__ == "__main__":
    check_api_key()
