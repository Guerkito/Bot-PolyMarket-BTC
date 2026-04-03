import requests
import sys

def find_user(username):
    print(f"🔍 Buscando address para @{username}...")
    
    # Intento 1: API de Perfil (a veces requiere auth, probamos suerte)
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/profiles?username={username}")
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and data:
                print(f"✅ Encontrado: {data[0].get('address')}")
                return data[0].get('address')
            if isinstance(data, dict) and data.get('address'):
                print(f"✅ Encontrado: {data.get('address')}")
                return data.get('address')
    except: pass

    print("❌ No se pudo encontrar automáticamente.")
    print("👉 Por favor, ve al perfil de Polymarket, abre la consola del navegador (F12) o mira la URL de un trade suyo para copiar la '0x... address'.")

if __name__ == "__main__":
    username = sys.argv[1] if len(sys.argv) > 1 else "PBot1"
    find_user(username)
