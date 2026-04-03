import os, time, requests, threading, json, re
from datetime import datetime, timezone
from dotenv import load_dotenv

# SDK OFICIAL DE POLYMARKET
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
    from py_clob_client.models import OrderArgs
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False

load_dotenv()

# ─── CONFIGURACIÓN DE USUARIO ─────────────────────────────────────────────────
MODO_SIMULACION  = os.getenv("SIMULATION_MODE", "true").lower() == "true"
BET_AMOUNT       = float(os.getenv("BET_AMOUNT", "2.0"))
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "20.0"))
THRESHOLD        = 0.85
INTERVAL_SECS    = 300
BET_WINDOW_START = 60   # Inicio ventana
BET_WINDOW_END   = 15   # Fin ventana

# API KEYS
PK              = os.getenv("PK")
CLOB_API_KEY    = os.getenv("CLOB_API_KEY")
CLOB_SECRET     = os.getenv("CLOB_SECRET")
CLOB_PASSPHRASE = os.getenv("CLOB_PASSPHRASE")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json"
}

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.columns import Columns
    from rich import box
    from rich.text import Text
    RICH = True
    console = Console()
except ImportError:
    RICH = False

# ─── MOTOR DE DATOS ───────────────────────────────────────────────────────────

class DataEngine:
    def __init__(self):
        self.btc_price = 0.0
        self.target_price = 0.0
        self.prices = {"UP": 0.0, "DOWN": 0.0}
        self.done = False

    def sync_btc(self):
        while True:
            try:
                r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=3)
                self.btc_price = float(r.json().get("price", 0))
            except: pass
            time.sleep(1)

    def get_target_historical(self, start_iso):
        try:
            ts = int(datetime.fromisoformat(start_iso.replace("Z", "+00:00")).timestamp()) * 1000
            r = requests.get("https://api.binance.com/api/v3/klines", 
                             params={"symbol": "BTCUSDT", "interval": "1m", "startTime": ts, "limit": 1})
            return float(r.json()[0][1])
        except: return 0.0

# ─── BOT ELITE ────────────────────────────────────────────────────────────────

class BtcBotElite:
    def __init__(self):
        self.engine = DataEngine()
        self.trades = []
        self.cash = STARTING_BALANCE
        self.wins = 0
        self.losses = 0
        self.client = None
        self.real_ready = False
        
        if not MODO_SIMULACION:
            self.setup_real_trading()

    def setup_real_trading(self):
        if not SDK_AVAILABLE: return
        if PK and CLOB_API_KEY:
            try:
                self.client = ClobClient(CLOB_API, key=PK, chain_id=POLYGON, 
                                         api_key=CLOB_API_KEY, api_secret=CLOB_SECRET, api_passphrase=CLOB_PASSPHRASE)
                if self.client.get_ok() == "OK": self.real_ready = True
            except: pass

    def place_real_bet(self, token_id, side, price):
        if not self.real_ready or not self.client: return "ERROR AUTH"
        try:
            limit_price = round(price + 0.01, 3)
            size = round(BET_AMOUNT / price, 2)
            order_args = OrderArgs(price=limit_price, size=size, side="BUY", token_id=token_id)
            resp = self.client.create_order(order_args)
            if resp and resp.get("success"): return f"REAL ✅"
            else: return "FALLO ❌"
        except: return "ERROR ❌"

    def render_ui(self, market, seconds_left, bet_placed):
        if not RICH: return
        console.clear()
        
        btc, target = self.engine.btc_price, self.engine.target_price
        diff = btc - target if target > 0 else 0
        status = "[bold green]▲ ARRIBA[/]" if diff >= 0 else "[bold red]▼ ABAJO[/]"
        
        mode_txt = "[bold green]DINERO REAL[/]" if self.real_ready else "[bold yellow]MODO SIMULACIÓN[/]"
        
        console.print(Panel(
            f"MODO: {mode_txt}\n"
            f"SLUG: [cyan]{market.get('slug')}[/]\n"
            f"TARGET: [yellow]${target:,.2f}[/] | BTC: [white]${btc:,.2f}[/]\n"
            f"ESTADO: {status} ([dim]{diff:+.2f} USD[/])", 
            title="🎯 MONITOR DE TRADING", border_style="blue"
        ))

        table = Table(box=box.SIMPLE, expand=True)
        table.add_column("LADO", width=10); table.add_column("CONFIANZA", width=35); table.add_column("PRECIO")
        for s in ["UP", "DOWN"]:
            p = self.engine.prices[s]
            color = "green" if p >= THRESHOLD else "cyan" if p >= 0.5 else "white"
            bar = f"[{color}]{'█'*int(p*30)}{'░'*(30-int(p*30))}[/]"
            table.add_row(f"BTC {s}", bar, f"{p*100:.1f}c")
        console.print(table)

        rem_min, rem_sec = max(0, seconds_left//60), max(0, seconds_left%60)
        
        status_msg = "[bold green]APOSTADO[/]" if bet_placed else \
                     f"[dim]Ventana Activa ({BET_WINDOW_END}s - {BET_WINDOW_START}s)[/]" if BET_WINDOW_END <= seconds_left <= BET_WINDOW_START else \
                     "[dim]Esperando ventana...[/]"

        console.print(f"  [bold red]CIERRE: {rem_min:02d}:{rem_sec:02d}[/] | {status_msg}\n")

        pnl = self.cash - STARTING_BALANCE
        stats = Text(f"Balance Virtual: ${self.cash:.2f}\nP&L Total: {'+' if pnl>=0 else ''}${pnl:.2f}\nWins: {self.wins} | Losses: {self.losses}", style="bold yellow")
        
        hist = Table(title="📝 Historial", box=box.MINIMAL, expand=True)
        hist.add_column("Hora"); hist.add_column("Lado"); hist.add_column("Res")
        for t in self.trades[-3:]:
            hist.add_row(t["time"], t["side"], t["resultado"])
        console.print(Columns([Panel(stats, title="📊 Rendimiento", width=40), Panel(hist, width=50)]))

    def resolve_trades(self, close_price):
        for t in self.trades:
            if "Pendiente" not in t["resultado"] and "VIRTUAL" not in t["resultado"] and "REAL" not in t["resultado"]:
                continue
            if t.get("processed"): continue

            won = (close_price >= t["target"]) if t["side"] == "UP" else (close_price < t["target"])
            
            if won:
                self.wins += 1
                profit = (BET_AMOUNT / t["price"]) - BET_AMOUNT
                self.cash += (BET_AMOUNT + profit)
                t["resultado"] = "WIN 🏆"
            else:
                self.losses += 1
                t["resultado"] = "LOSS 💀"
            
            t["processed"] = True

    def run(self):
        threading.Thread(target=self.engine.sync_btc, daemon=True).start()
        
        while True:
            now_ts = (int(time.time()) // 300) * 300
            slug = f"btc-updown-5m-{now_ts}"
            
            market = None
            for _ in range(12): 
                try:
                    r = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, headers=HEADERS, timeout=5)
                    if r.json(): market = r.json()[0]; break
                except: pass
                time.sleep(3)

            if not market: continue

            c_ids = market.get("clobTokenIds")
            if isinstance(c_ids, str): c_ids = json.loads(c_ids)
            
            start_iso = market.get("eventStartTime") or market.get("startDate")
            self.engine.target_price = self.engine.get_target_historical(start_iso)
            
            end_dt = datetime.fromisoformat(market.get("endDate").replace("Z", "+00:00"))
            bet_placed = False

            while True:
                diff = (end_dt - datetime.now(timezone.utc)).total_seconds()
                seconds_left = int(diff)
                if seconds_left <= 0:
                    btc_at_close = self.engine.btc_price; break

                for i, s in enumerate(["UP", "DOWN"]):
                    try:
                        r_p = requests.get(f"{CLOB_API}/price", params={"token_id": c_ids[i], "side": "BUY"}, timeout=1)
                        self.engine.prices[s] = float(r_p.json().get("price", 0))
                    except: pass

                self.render_ui(market, seconds_left, bet_placed)

                # ─── LÓGICA DE VENTANA CONTINUA ───
                in_window = BET_WINDOW_END <= seconds_left <= BET_WINDOW_START
                if in_window and not bet_placed:
                    up_p, down_p = self.engine.prices["UP"], self.engine.prices["DOWN"]
                    # Evaluar disparo inmediato
                    side = "UP" if up_p >= THRESHOLD else "DOWN" if down_p >= THRESHOLD else None
                    
                    if side:
                        price = self.engine.prices[side]
                        token_id = c_ids[0] if side == "UP" else c_ids[1]
                        
                        res = "VIRTUAL"
                        if not MODO_SIMULACION and self.real_ready:
                            res = self.place_real_bet(token_id, side, price)
                        else:
                            self.cash -= BET_AMOUNT
                        
                        self.trades.append({
                            "time": datetime.now().strftime("%H:%M"), "side": side, 
                            "target": self.engine.target_price, "price": price, 
                            "resultado": f"{res} (Pend)", "cost": BET_AMOUNT
                        })
                        bet_placed = True

                time.sleep(0.5)
            
            self.resolve_trades(btc_at_close)
            
            for _ in range(5):
                self.render_ui(market, 0, bet_placed)
                time.sleep(1)

if __name__ == "__main__":
    bot = BtcBotElite()
    bot.run()
