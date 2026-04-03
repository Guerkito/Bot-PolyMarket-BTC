import os
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from py_clob_client.constants import POLYGON
from dotenv import load_dotenv

load_dotenv("polymarket-bot/.env")

# Proxy config
HTTP_PROXY_URL = os.getenv("HTTP_PROXY")
if HTTP_PROXY_URL:
    os.environ["HTTP_PROXY"] = HTTP_PROXY_URL
    os.environ["HTTPS_PROXY"] = HTTP_PROXY_URL

PK = os.getenv("PK")
FUNDER = os.getenv("TRADER_ADDRESS")
CLOB_API_KEY = os.getenv("CLOB_API_KEY")
CLOB_SECRET = os.getenv("CLOB_SECRET")
CLOB_PASSPHRASE = os.getenv("CLOB_PASSPHRASE")

CLOB_API = "https://clob.polymarket.com"

import traceback

def test_auth():
    print(f"Testing with FUNDER: {FUNDER}")
    
    creds = ApiCreds(api_key=CLOB_API_KEY, api_secret=CLOB_SECRET, api_passphrase=CLOB_PASSPHRASE)
    
    for sig_type in [0, 1, 2]:
        print(f"\n--- Testing with signature_type={sig_type} ---")
        try:
            client = ClobClient(CLOB_API, key=PK, chain_id=POLYGON, creds=creds, funder=FUNDER, signature_type=sig_type)
            ok = client.get_ok()
            print(f"get_ok() status: {ok}")
            
            try:
                # Intento de obtener órdenes (requiere firma válida)
                orders = client.get_orders()
                print(f"Successfully fetched orders (Auth OK) with sig_type={sig_type}")
                return # Si uno funciona, paramos
            except Exception as e:
                print(f"Failed to fetch orders with sig_type={sig_type}: {e}")
                # traceback.print_exc()
                
        except Exception as e:
            print(f"Error initializing/calling with sig_type={sig_type}: {e}")
            traceback.print_exc()

if __name__ == "__main__":
    test_auth()
