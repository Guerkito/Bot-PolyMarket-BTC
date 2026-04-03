import os
import json
import traceback
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs
from py_clob_client.constants import POLYGON
from eth_account import Account
from dotenv import load_dotenv

# Cargar configuración exacta del bot
load_dotenv("polymarket-bot/.env")

PK = os.getenv("PK")
FUNDER = os.getenv("TRADER_ADDRESS")
CLOB_API_KEY = os.getenv("CLOB_API_KEY")
CLOB_SECRET = os.getenv("CLOB_SECRET")
CLOB_PASSPHRASE = os.getenv("CLOB_PASSPHRASE")
CLOB_API = "https://clob.polymarket.com"

# Token USDC.e en Polygon (el que usa Polymarket)
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

def run_diagnostics():
    print("===== DIAGNÓSTICO POLYMARKET =====")
    
    # 1. Signer derivado
    signer = Account.from_key(PK).address
    print(f"SIGNER DERIVADO (EOA): {signer}")
    print(f"FUNDER CONFIGURADO: {FUNDER}")
    
    creds = ApiCreds(api_key=CLOB_API_KEY, api_secret=CLOB_SECRET, api_passphrase=CLOB_PASSPHRASE)
    client = ClobClient(CLOB_API, key=PK, chain_id=POLYGON, creds=creds, funder=FUNDER, signature_type=2)

    # 1. Address exacta consultada para balance
    # El SDK usa el FUNDER si está definido, sino el Signer.
    balance_addr = FUNDER if FUNDER else signer
    print(f"\n1. ADDRESS CONSULTADA PARA BALANCE: {balance_addr}")

    # 2. Contrato exacto del token
    print(f"2. TOKEN USDC.e CONSULTADO: {USDC_E}")

    # 3. Consultar balance y allowance reales via SDK
    try:
        # Reemplazo por get_balance_allowance (método unificado)
        balance_info = client.get_balance_allowance(USDC_E)
        balance = balance_info['balance']
        allowance = balance_info['allowance']
        
        human_balance = float(balance) / 1_000_000
        
        print(f"BALANCE RETORNADO POR SDK: {balance} (Raw)")
        print(f"BALANCE HUMAN READABLE: {human_balance:.2f} USDC")
        print(f"3. ALLOWANCE HACIA EL EXCHANGE: {allowance}")
    except Exception as e:
        print(f"ERROR AL CONSULTAR BALANCE/ALLOWANCE: {repr(e)}")

    # 4. Intento de orden para ver respuesta cruda de post_order
    print("\n4. INTENTO DE post_order (Orden de prueba inválida/barata):")
    try:
        # Intentamos crear una orden "basura" o mínima para forzar una respuesta del servidor
        # Usamos un token_id cualquiera (BTC UP por ejemplo)
        # Nota: post_order requiere una orden firmada. Usamos create_order para generar una.
        # side="BUY" (0), price=0.01, size=1.0
        order_args = OrderArgs(
            price=0.01,
            size=1.0,
            side="BUY",
            token_id="2319954832014101053153578051515132336338001004126137946927974395898822066861" # Ejemplo
        )
        signed_order = client.create_order(order_args)
        
        print("Enviando post_order...")
        resp = client.post_order(signed_order)
        print(f"RESPUESTA CRUDA post_order: {json.dumps(resp, indent=2)}")
    except Exception as e:
        print(f"ERROR CRUDO post_order: {repr(e)}")
        # traceback.print_exc()

if __name__ == "__main__":
    run_diagnostics()
