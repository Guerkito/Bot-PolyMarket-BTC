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

def check_balance():
    creds = ApiCreds(api_key=CLOB_API_KEY, api_secret=CLOB_SECRET, api_passphrase=CLOB_PASSPHRASE)
    # Forzamos todos los argumentos como "keyword arguments" para máxima claridad y evitar errores de posición.
    client = ClobClient(
        host=CLOB_API,
        key=PK,
        chain_id=POLYGON,
        creds=creds,
        funder=FUNDER,
        signature_type=2
    )
    
    print(f"Checking balance for FUNDER: {FUNDER}")
    # USDC token on Polygon for Polymarket
    USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    try:
        # La versión del SDK instalada usa un método unificado.
        balance_info = client.get_balance_allowance(USDC)
        balance = balance_info['balance']
        allowance = balance_info['allowance']
        
        # El balance viene en formato de 6 decimales (1 USDC = 1,000,000)
        human_readable_balance = float(balance) / 1_000_000
        
        print(f"USDC Balance (raw): {balance}")
        print(f"USDC Balance (human): {human_readable_balance:.2f} USDC")
        print(f"USDC Allowance: {allowance}")
        
    except Exception as e:
        print(f"Error checking balance: {e}")

if __name__ == "__main__":
    check_balance()
