import os
import time
import requests
import threading
from datetime import datetime
from dotenv import load_dotenv
from web3 import Web3

# SDK de Polymarket
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON
    from py_clob_client.models import OrderArgs
    SDK_INSTALLED = True
except ImportError:
    SDK_INSTALLED = False

load_dotenv()

# ─── CONFIGURACIÓN ────────────────────────────────────────────────────────────
TRADER_USERNAME  = os.getenv("TRADER_USERNAME", "Target")
TRADER_ADDRESS   = os.getenv("TRADER_ADDRESS", "")
TRADE_SIZE_USDC  = float(os.getenv("TRADE_SIZE_USDC", "10"))
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "100"))
POLL_INTERVAL    = 30  # Intervalo normal (lento)
SIMULATION_MODE  = os.getenv("SIMULATION_MODE", "true").lower() == "true"

# Blockchain Config
POLYGON_RPC = "https://polygon-rpc.com"
POLY_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E" # Contrato Polymarket

# Credenciales
PK = os.getenv("PK")
CLOB_API_KEY = os.getenv("CLOB_API_KEY")
CLOB_SECRET = os.getenv("CLOB_SECRET")
CLOB_PASSPHRASE = os.getenv("CLOB_PASSPHRASE")

DATA_API = "https://data-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
    from rich.text import Text
    RICH = True
    console = Console()
except ImportError:
    RICH = False

# ─── CLASES DE APOYO ──────────────────────────────────────────────────────────

class PortfolioVirtual:
    def __init__(self, balance):
        self.cash = balance
        self.starting = balance
        self.positions = []
        self.history = []
        self.wins = 0
        self.losses = 0

    def add_position(self, trade):
        side      = trade.get("side", "BUY").upper()
        outcome   = trade.get("outcome", "?")
        price     = float(trade.get("price", 0))
        title     = trade.get("title", "Mercado Desconocido")
        token_id  = trade.get("asset", "")
        
        if price <= 0 or self.cash < 1: return None

        cost = min(TRADE_SIZE_USDC, self.cash)
        tokens = cost / price
        self.cash -= cost

        pos = {
            "title": title[:30] + "..." if len(title) > 30 else title,
            "side": side,
            "outcome": outcome,
            "entry_price": price,
            "tokens": tokens,
            "cost": cost,
            "token_id": token_id,
            "current_price": price,
            "pnl": 0.0,
            "status": "OPEN",
            "time": datetime.now().strftime("%H:%M")
        }
        self.positions.append(pos)
        return pos

    def update_prices(self):
        for pos in self.positions:
            if pos["status"] != "OPEN": continue
            try:
                resp = requests.get(f"{CLOB_API_URL}/price", params={"token_id": pos["token_id"], "side": "BUY"}, timeout=3)
                if resp.status_code == 200:
                    price = float(resp.json().get("price", 0))
                    if price > 0:
                        pos["current_price"] = price
                        pos["pnl"] = (price - pos["entry_price"]) * pos["tokens"]
            except: pass

    def close_resolved(self):
        still_open = []
        for pos in self.positions:
            p = pos["current_price"]
            if p >= 0.98 or p <= 0.02:
                payout = pos["tokens"] if p >= 0.98 else 0
                self.cash += payout
                pos["status"] = "CLOSED"
                pos["pnl"] = payout - pos["cost"]
                if pos["pnl"] > 0: self.wins += 1
                else: self.losses += 1
                self.history.append(pos)
            else:
                still_open.append(pos)
        self.positions = still_open

# ─── TRADER PRINCIPAL ─────────────────────────────────────────────────────────

class TurboCopyTrader:
    def __init__(self):
        self.clob_client = None
        self.portfolio = PortfolioVirtual(STARTING_BALANCE)
        self.seen_ids = set()
        self.real_ready = False
        self.logs = []
        self.w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
        self.target_address = Web3.to_checksum_address(TRADER_ADDRESS) if TRADER_ADDRESS else None
        
        self.add_log(f"Iniciando Modo Turbo sobre {TRADER_ADDRESS[:6]}...")
        
    def add_log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[dim]{ts}[/] {msg}")
        if len(self.logs) > 6: self.logs.pop(0)

    def setup_real_client(self):
        if SIMULATION_MODE: return False
        if not SDK_INSTALLED: return False
        if not all([PK, CLOB_API_KEY, CLOB_SECRET, CLOB_PASSPHRASE]): return False
        try:
            self.clob_client = ClobClient(CLOB_API_URL, key=PK, chain_id=POLYGON, 
                                          api_key=CLOB_API_KEY, api_secret=CLOB_SECRET, api_passphrase=CLOB_PASSPHRASE)
            if self.clob_client.get_ok() == "OK":
                self.real_ready = True
                self.add_log("Conexión Real EXITOSA")
                return True
        except: return False
        return False

    def check_new_trades(self, source="API"):
        """Consulta la API de actividad para ver qué compró."""
        try:
            r = requests.get(f"{DATA_API}/activity", 
                             params={"user": TRADER_ADDRESS, "type": "TRADE", "limit": 5, "sortBy": "TIMESTAMP", "sortDirection": "DESC"}, 
                             timeout=5)
            if r.status_code == 200:
                trades = r.json()
                found = False
                for t in reversed(trades):
                    tid = t.get("id") or t.get("transactionHash")
                    if tid not in self.seen_ids:
                        self.seen_ids.add(tid)
                        # Solo copiar COMPRAS (entradas)
                        if t.get("side") == "BUY":
                            self.execute_trade(t, source)
                            found = True
                return found
        except: pass
        return False

    def execute_trade(self, trade_data, source):
        side = trade_data.get("side", "").upper()
        token_id = trade_data.get("asset")
        price = float(trade_data.get("price", 0))
        title = trade_data.get("title", "?")
        
        self.add_log(f"[{source}] COPIANDO: {title[:15]}... ({side})")
        
        # 1. Registro
        self.portfolio.add_position(trade_data)
        
        # 2. Ejecución Real
        if self.real_ready and not SIMULATION_MODE:
            try:
                limit_price = round(price + 0.02, 3) 
                order_args = OrderArgs(price=limit_price, size=TRADE_SIZE_USDC/price, side=side, token_id=token_id)
                self.clob_client.create_order(order_args)
                return "REAL ✅"
            except Exception as e:
                self.add_log(f"Error Orden Real: {str(e)[:10]}")
                return "FAIL ❌"
        
        return "SIMULADO"

    def blockchain_listener(self):
        """Escucha la blockchain en tiempo real (Velocidad Luz)."""
        if not self.target_address: return
        self.add_log("Sniffer de Blockchain ACTIVO 🟢")
        
        last_block = self.w3.eth.block_number
        while True:
            try:
                current_block = self.w3.eth.block_number
                if current_block > last_block:
                    block = self.w3.eth.get_block(current_block, full_transactions=True)
                    for tx in block.transactions:
                        if tx['from'] == self.target_address:
                            # ¡Movimiento detectado! Consultar API inmediatamente
                            self.add_log("⚡ ¡MOVIMIENTO DETECTADO EN BLOCKCHAIN!")
                            # Hacemos 3 intentos rápidos de consulta a la API
                            for _ in range(3):
                                if self.check_new_trades(source="FLASH"): break
                                time.sleep(1.5)
                    last_block = current_block
                time.sleep(2)
            except: time.sleep(5)

    def render_ui(self):
        if not RICH: return
        console.clear()
        
        p = self.portfolio
        floating_pnl = sum((pos["current_price"] - pos["entry_price"]) * pos["tokens"] for pos in p.positions)
        total_pnl = (p.cash - p.starting) + floating_pnl
        mode_str = "[bold yellow]SIMULACIÓN[/bold yellow]" if SIMULATION_MODE else "[bold green]DINERO REAL[/bold green]"
        
        console.print(Panel(f"{mode_str} | Objetivo: [cyan]@{TRADER_USERNAME}[/cyan] | Balance: [bold yellow]${p.cash:.2f}[/] | P&L: {'+' if total_pnl>=0 else ''}${total_pnl:.2f}", title="🚀 TURBO COPY TRADER", border_style="red"))

        if p.positions:
            table = Table(title="📊 Posiciones Activas", box=box.SIMPLE, expand=True)
            table.add_column("Hora", width=6); table.add_column("Mercado"); table.add_column("Lado", width=4); 
            table.add_column("Monto", width=8); table.add_column("Progreso", width=30); table.add_column("P&L", width=10)
            
            for pos in p.positions:
                curr = pos["current_price"]
                entry = pos["entry_price"]
                bar_color = "green" if curr >= entry else "red"
                bar = f"[{bar_color}]{'█' * int(curr*20)}{'░' * (20-int(curr*20))}[/] {curr*100:.0f}c"
                cur_pnl = (curr - entry) * pos["tokens"]
                pnl_txt = f"[green]+${cur_pnl:.2f}[/]" if cur_pnl >= 0 else f"[red]${cur_pnl:.2f}[/]"
                table.add_row(pos["time"], pos["title"], pos["side"], f"${pos['cost']:.0f}", bar, pnl_txt)
            console.print(table)
        else:
            console.print("[dim]Esperando al tiburón...[/dim]")

        if p.history:
            hist = Table(title="📜 Historial Cerrado", box=box.MINIMAL, expand=True)
            hist.add_column("Hora"); hist.add_column("Mercado"); hist.add_column("Res."); hist.add_column("P&L")
            for h in reversed(p.history[-5:]):
                res_col = "[green]WIN[/]" if h["pnl"] > 0 else "[red]LOSS[/]"
                pnl_txt = f"[green]+${h['pnl']:.2f}[/]" if h["pnl"] > 0 else f"[red]${h['pnl']:.2f}[/]"
                hist.add_row(h["time"], h["title"], res_col, pnl_txt)
            console.print(hist)

        console.print(Panel("\n".join(self.logs), title="📟 Radar de Actividad", style="dim", height=7))

    def run(self):
        self.setup_real_client()
        
        # Iniciar Sniffer en hilo aparte
        threading.Thread(target=self.blockchain_listener, daemon=True).start()
        
        # Cargar historial inicial
        try:
            r = requests.get(f"{DATA_API}/activity", params={"user": TRADER_ADDRESS, "limit": 20}, timeout=10)
            for t in r.json(): self.seen_ids.add(t.get("id") or t.get("transactionHash"))
        except: pass

        while True:
            # Chequeo regular por si el sniffer falla
            self.check_new_trades(source="POLL")
            
            self.portfolio.update_prices()
            self.portfolio.close_resolved()
            self.render_ui()
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    bot = TurboCopyTrader()
    bot.run()
