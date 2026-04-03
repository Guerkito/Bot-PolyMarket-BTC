import os, requests, json
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs
from py_clob_client.constants import POLYGON
from dotenv import load_dotenv

load_dotenv("polymarket-bot/.env")

PK = os.getenv("PK")
CLOB_API_KEY = os.getenv("CLOB_API_KEY")
CLOB_SECRET = os.getenv("CLOB_SECRET")
CLOB_PASSPHRASE = os.getenv("CLOB_PASSPHRASE")
CLOB_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

def verify_full_access():
    print("--- VERIFICACIÓN DE ACCESO TOTAL ---")
    creds = ApiCreds(api_key=CLOB_API_KEY, api_secret=CLOB_SECRET, api_passphrase=CLOB_PASSPHRASE)
    client = ClobClient(CLOB_API, key=PK, chain_id=POLYGON, creds=creds, signature_type=0)

    # 1. Probar lectura privada
    print("\n1. Probando lectura de órdenes privadas...")
    try:
        orders = client.get_orders()
        print(f"✅ CONECTADO: Se recuperaron {len(orders)} órdenes. La API Key es válida.")
    except Exception as e:
        print(f"❌ ERROR DE LECTURA: {e}")
        return

    # 2. Obtener un mercado de BTC 5m (GARANTIZADO en el CLOB)
    print("\n2. Buscando mercado de BTC 5m activo...")
    try:
        # Los mercados de 5m siempre están en el CLOB
        ts = (int(time.time()) // 300) * 300
        slug = f"btc-updown-5m-{ts}"
        r = requests.get(f"{GAMMA_API}/markets", params={"slug": slug})
        market = r.json()[0]
        c_ids = json.loads(market.get("clobTokenIds"))
        token_id = c_ids[0]
        print(f"✅ MERCADO REAL: {slug} (ID: {token_id[:15]}...)")
    except:
        print("❌ No se encontró mercado de 5m activo en este segundo.")
        return

    # 3. Intentar colocar una orden real
    print("\n3. Intentando FIRMAR y colocar una orden de $5...")
    order_args = OrderArgs(
        price=0.90,
        size=5.5,
        side="BUY",
        token_id=token_id
    )
    
    try:
        resp = client.create_and_post_order(order_args)
        print(f"✅ RESPUESTA DEL SERVIDOR: {resp}")
    except Exception as e:
        error_msg = str(e)
        if "invalid signature" in error_msg.lower():
            print("❌ ERROR DE FIRMA: Polymarket rechazó la firma. La relación Magic.link/Sub-wallet no es válida.")
        elif "not enough balance" in error_msg.lower() or "lower than the minimum" in error_msg.lower():
            print("💎 ¡ESTAMOS DENTRO! La firma fue ACEPTADA. Solo falta balance en la wallet 0x2D9e...")
        else:
            print(f"❓ RESULTADO INESPERADO: {e}")

if __name__ == "__main__":
    verify_full_access()
