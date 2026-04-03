import requests
import json
import re
import time
from datetime import datetime, timezone

class PolyScraper:
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json"
        }
        self.base_url = "https://gamma-api.polymarket.com"

    def get_live_btc(self):
        """Intenta obtener BTC de 3 fuentes distintas para evitar el 0.00."""
        # 1. Binance
        try:
            r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=3)
            if r.status_code == 200: return float(r.json()['price']), "Binance"
        except: pass
        
        # 2. Coinbase
        try:
            r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=3)
            if r.status_code == 200: return float(r.json()['data']['amount']), "Coinbase"
        except: pass
        
        # 3. CoinGecko
        try:
            r = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", timeout=3)
            if r.status_code == 200: return float(r.json()['bitcoin']['usd']), "CoinGecko"
        except: pass
        
        return 0.0, "Fallo Red"

    def get_active_market(self):
        """Busca el mercado de 5m por slug directo y luego por búsqueda general."""
        now_ts = int(time.time())
        current_window = (now_ts // 300) * 300
        slug = f"btc-updown-5m-{current_window}"
        
        # Intento A: Slug Directo
        try:
            r = requests.get(f"{self.base_url}/markets?slug={slug}", headers=self.headers, timeout=3)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data: return data[0]
        except: pass

        # Intento B: Búsqueda General de Bitcoin
        try:
            r = requests.get(f"{self.base_url}/markets", params={"q": "Bitcoin", "active": "true", "closed": "false"}, headers=self.headers, timeout=3)
            if r.status_code == 200:
                markets = r.json()
                now = datetime.now(timezone.utc)
                for m in markets:
                    title = m.get("question", "").lower()
                    if ("up or down" in title or "sube o baja" in title) and "5m" in m.get("slug", ""):
                        end_dt = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
                        if end_dt > now: return m
        except: pass
        return None

    def extract_target(self, market):
        """Escanea todos los campos posibles en busca del Target."""
        if not market: return 0.0
        
        # 1. Metadatos oficiales
        meta = market.get("eventMetadata")
        if isinstance(meta, str):
            try: meta = json.loads(meta)
            except: meta = {}
        if meta and meta.get("priceToBeat"): return float(meta['priceToBeat'])
        
        # 2. Regex en Descripción o Pregunta
        full_text = f"{market.get('question','')} {market.get('description','')}"
        # Buscamos números como 67,123.50 o 67123.50
        matches = re.findall(r'\$?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)', full_text)
        for val_str in matches:
            val = float(val_str.replace(',', ''))
            if val > 20000: return val # Solo si parece precio de BTC
            
        return 0.0
