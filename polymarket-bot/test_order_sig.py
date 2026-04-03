import os
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs
from py_clob_client.constants import POLYGON
from dotenv import load_dotenv

load_dotenv("polymarket-bot/.env")

PK = os.getenv("PK")
FUNDER = os.getenv("TRADER_ADDRESS")
CLOB_API_KEY = os.getenv("CLOB_API_KEY")
CLOB_SECRET = os.getenv("CLOB_SECRET")
CLOB_PASSPHRASE = os.getenv("CLOB_PASSPHRASE")

CLOB_API = "https://clob.polymarket.com"

def test_juanito_config():
    creds = ApiCreds(api_key=CLOB_API_KEY, api_secret=CLOB_SECRET, api_passphrase=CLOB_PASSPHRASE)
    
    # Configuración de Juanito: sig_type=1 y funder del perfil
    funder = os.getenv("FUNDER", FUNDER)
    client = ClobClient(CLOB_API, key=PK, chain_id=POLYGON, creds=creds, funder=funder, signature_type=1)
    
    print(f"\n--- Testing Juanito's Config (sig_type=1, funder={funder}) ---")
    
    # Obtener un mercado de BTC 5m real
    import time, requests
    ts = (int(time.time()) // 300) * 300
    slug = f"btc-updown-5m-{ts}"
    token_id = None
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets", params={"slug": slug})
        token_id = json.loads(r.json()[0]["clobTokenIds"])[0]
    except:
        print("No btc 5m market found, using static ID for testing")
        token_id = "16665941372370715367683401730072617719602324911765796035882662283944648714695"

    order_args = OrderArgs(
        price=0.95,
        size=5.0,
        side="BUY",
        token_id=token_id
    )
    
    try:
        resp = client.create_and_post_order(order_args)
        print(f"Response: {resp}")
    except Exception as e:
        print(f"Result: {e}")

if __name__ == "__main__":
    test_juanito_config()
