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
BET_AT_SECONDS   = 60

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
        self.eth_price = 0.0
        self.target_price = 0.0
        self.prices = {"UP": 0.0, "DOWN": 0.0}
        self.done = False

    def sync_price(self):
        while True:
            try:
                r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT", timeout=3)
                self.eth_price = float(r.json().get("price", 0))
            except: pass
            time.sleep(1)

    def get_target_historical(self, start_iso):
        try:
            ts = int(datetime.fromisoformat(start_iso.replace("Z", "+00:00")).timestamp()) * 1000
            r = requests.get("https://api.binance.com/api/v3/klines", 
                             params={"symbol": "ETHUSDT", "interval": "1m", "startTime": ts, "limit": 1})
            return float(r.json()[0][1])
        except: return 0.0

# ─── BOT ELITE (ETH VERSION) ──────────────────────────────────────────────────

class EthBotElite:
    def __init__(self):
        self.engine = DataEngine()
        self.trades = []
        self.cash = STARTING_BALANCE
        self.wins = 0
        self.losses = 0
        self.total_cycles = 0
        self.active_cycles = 0
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
        
        eth, target = self.engine.eth_price, self.engine.target_price
        diff = eth - target if target > 0 else 0
        status = "[bold green]▲ ARRIBA[/]" if diff >= 0 else "[bold red]▼ ABAJO[/]"
        
        mode_txt = "[bold green]DINERO REAL[/]" if self.real_ready else "[bold yellow]MODO SIMULACIÓN[/]"
        
        console.print(Panel(
            f"MODO: {mode_txt} | CICLO ACTUAL: #{self.total_cycles}\n"
            f"SLUG: [cyan]{market.get('slug')}[/]\n"
            f"TARGET: [yellow]${target:,.2f}[/] | ETH: [white]${eth:,.2f}[/]\n"
            f"ESTADO: {status} ([dim]{diff:+.2f} USD[/])", 
            title="💎 ETHEREUM MONITOR", border_style="cyan"
        ))

        table = Table(box=box.SIMPLE, expand=True)
        table.add_column("LADO", width=10); table.add_column("CONFIANZA", width=35); table.add_column("PRECIO")
        for s in ["UP", "DOWN"]:
            p = self.engine.prices[s]
            color = "green" if p >= THRESHOLD else "cyan" if p >= 0.5 else "white"
            bar = f"[{color}]{'█'*int(p*30)}{'░'*(30-int(p*30))}[/]"
            table.add_row(f"ETH {s}", bar, f"{p*100:.1f}c")
        console.print(table)

        rem_min, rem_sec = max(0, seconds_left//60), max(0, seconds_left%60)
        console.print(f"  [bold red]CIERRE: {rem_min:02d}:{rem_sec:02d}[/] | {'[bold green]ORDEN ENVIADA[/]' if bet_placed else '[dim]Esperando señal...[/]'}\n")

        pnl = self.cash - STARTING_BALANCE
        participation = (self.active_cycles / self.total_cycles * 100) if self.total_cycles > 0 else 0
        
        stats = Text(f"Balance: ${self.cash:.2f}\nP&L Total: {'+' if pnl>=0 else ''}${pnl:.2f}\nWins: {self.wins} | Losses: {self.losses}\nCiclos Totales: {self.total_cycles}\nParticipación: {participation:.1f}%", style="bold yellow")
        
        # Historial
        hist = Table(title="📝 Historial", box=box.MINIMAL, expand=True)
        hist.add_column("Ciclo", width=6); hist.add_column("Hora", width=8); hist.add_column("Lado", width=6); hist.add_column("Res.", width=10); hist.add_column("P&L", width=10)
        
        for t in self.trades[-3:]:
            pnl_val = t.get("pnl_val", 0.0)
            pnl_txt = f"[green]+${pnl_val:.2f}[/]" if pnl_val > 0 else f"[red]-${abs(pnl_val):.2f}[/]" if "LOSS" in t["resultado"] else "—"
            hist.add_row(f"#{t.get('cycle', '?')}", t["time"], t["side"], t["resultado"], pnl_txt)

        console.print(Columns([Panel(stats, title="📊 Rendimiento", width=40), Panel(hist, width=50)]))

    def resolve_trades(self, close_price):
        for t in self.trades:
            if "Pendiente" not in t["resultado"] and "VIRTUAL" not in t["resultado"] and "REAL" not in t["resultado"]: continue
            if t.get("processed"): continue

            won = (close_price >= t["target"]) if t["side"] == "UP" else (close_price < t["target"])
            if won:
                self.wins += 1
                profit = (BET_AMOUNT / t["price"]) - BET_AMOUNT
                self.cash += (BET_AMOUNT + profit)
                t["resultado"] = "WIN 🏆"
                t["pnl_val"] = profit
            else:
                self.losses += 1
                t["resultado"] = "LOSS 💀"
                t["pnl_val"] = -BET_AMOUNT
            t["processed"] = True

    def run(self):
        threading.Thread(target=self.engine.sync_price, daemon=True).start()
        
        while True:
            self.total_cycles += 1
            now_ts = (int(time.time()) // 300) * 300
            slug = f"eth-updown-5m-{now_ts}"
            
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
            bet_placed = False; bet_evaluated = False

            while True:
                diff = (end_dt - datetime.now(timezone.utc)).total_seconds()
                seconds_left = int(diff)
                if seconds_left <= 0:
                    eth_at_close = self.engine.eth_price; break

                for i, s in enumerate(["UP", "DOWN"]):
                    try:
                        r_p = requests.get(f"{CLOB_API}/price", params={"token_id": c_ids[i], "side": "BUY"}, timeout=1)
                        self.engine.prices[s] = float(r_p.json().get("price", 0))
                    except: pass

                self.render_ui(market, seconds_left, bet_placed)

                if (BET_AT_SECONDS - 2) <= seconds_left <= (BET_AT_SECONDS + 2) and not bet_evaluated:
                    bet_evaluated = True
                    up_p, down_p = self.engine.prices["UP"], self.engine.prices["DOWN"]
                    side = "UP" if up_p >= THRESHOLD else ("DOWN" if down_p >= THRESHOLD else None)
                    
                    if side:
                        price = self.engine.prices[side]
                        token_id = c_ids[0] if side == "UP" else c_ids[1]
                        
                        res = "VIRTUAL"
                        if not MODO_SIMULACION and self.real_ready:
                            res = self.place_real_bet(token_id, side, price)
                        else:
                            self.cash -= BET_AMOUNT
                        
                        self.active_cycles += 1
                        self.trades.append({
                            "cycle": self.total_cycles,
                            "time": datetime.now().strftime("%H:%M"), "side": side, 
                            "target": self.engine.target_price, "price": price, 
                            "resultado": f"{res} (Pend)", "cost": BET_AMOUNT, "pnl_val": 0.0
                        })
                        bet_placed = True

                time.sleep(0.5)
            
            self.resolve_trades(eth_at_close)
            for _ in range(5):
                self.render_ui(market, 0, bet_placed)
                time.sleep(1)

if __name__ == "__main__":
    bot = EthBotElite()
    bot.run()
