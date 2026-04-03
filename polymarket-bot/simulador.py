import os
import time
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TRADER_USERNAME  = os.getenv("TRADER_USERNAME", "kch123")
TRADER_ADDRESS   = os.getenv("TRADER_ADDRESS", "")
TRADE_SIZE_USDC  = float(os.getenv("TRADE_SIZE_USDC", "10"))
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "100"))

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

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


def test_connection(addr):
    """Verifica que la API responde correctamente."""
    try:
        resp = requests.get(
            f"{DATA_API}/activity",
            params={"user": addr, "type": "TRADE", "limit": 1,
                    "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return isinstance(data, list), len(data) > 0
    except Exception as e:
        return False, str(e)


def get_recent_trades(addr, limit=20):
    try:
        resp = requests.get(
            f"{DATA_API}/activity",
            params={"user": addr, "type": "TRADE", "limit": limit,
                    "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Error obteniendo trades: {e}")
        return []


def get_open_positions(addr):
    """Obtiene las posiciones abiertas actuales del trader."""
    try:
        resp = requests.get(
            f"{DATA_API}/positions",
            params={"user": addr, "sizeThreshold": "0.1"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Error obteniendo posiciones: {e}")
        return []


def get_current_price(token_id, side="BUY"):
    try:
        resp = requests.get(
            f"{CLOB_API}/price",
            params={"token_id": token_id, "side": side},
            timeout=8,
        )
        return float(resp.json().get("price", 0))
    except:
        return 0.0


class Portfolio:
    def __init__(self, starting_balance):
        self.cash = starting_balance
        self.starting = starting_balance
        self.positions = []
        self.history = []
        self.trades_count = 0
        self.wins = 0
        self.losses = 0

    def add_position(self, trade):
        side      = trade.get("side", "BUY").upper()
        outcome   = trade.get("outcomeIndex", 0)
        outcome_l = "Yes" if outcome == 0 else "No"
        price     = float(trade.get("price", 0))
        title     = trade.get("title", "Mercado desconocido")
        asset_id  = trade.get("asset", "")

        if price <= 0:
            return None

        cost = min(TRADE_SIZE_USDC, self.cash)
        if cost < 1:
            return None

        tokens_bought = cost / price
        self.cash -= cost
        self.trades_count += 1

        position = {
            "id":            self.trades_count,
            "title":         title[:55],
            "outcome":       outcome_l,
            "side":          side,
            "entry_price":   price,
            "tokens":        tokens_bought,
            "cost":          cost,
            "token_id":      asset_id,
            "opened_at":     datetime.now().strftime("%H:%M:%S"),
            "current_price": price,
            "pnl":           0.0,
            "status":        "OPEN",
        }
        self.positions.append(position)
        return position

    def update_prices(self):
        for pos in self.positions:
            if pos["status"] != "OPEN" or not pos.get("token_id"):
                continue
            price = get_current_price(pos["token_id"], pos["side"])
            if price > 0:
                pos["current_price"] = price
                pos["pnl"] = (price - pos["entry_price"]) * pos["tokens"]

    def close_resolved(self):
        still_open = []
        for pos in self.positions:
            if pos["status"] != "OPEN":
                continue
            p = pos["current_price"]
            if p >= 0.97 or p <= 0.03:
                payout = pos["tokens"] * pos["current_price"]
                self.cash += payout
                pos["status"] = "CLOSED"
                pos["pnl"] = payout - pos["cost"]
                if pos["pnl"] > 0:
                    self.wins += 1
                else:
                    self.losses += 1
                self.history.append(pos)
            else:
                still_open.append(pos)
        self.positions = still_open

    @property
    def open_value(self):
        return sum(p["tokens"] * p["current_price"] for p in self.positions if p["status"] == "OPEN")

    @property
    def total_value(self):
        return self.cash + self.open_value

    @property
    def total_pnl(self):
        return self.total_value - self.starting

    @property
    def total_pnl_pct(self):
        return (self.total_pnl / self.starting) * 100


def mostrar_estado(portfolio, trader, cycle, connected, trader_positions):
    pnl = portfolio.total_pnl
    sign = "+" if pnl >= 0 else ""

    if RICH:
        console.clear()
        color = "green" if pnl >= 0 else "red"
        conn_str = "[bold green]🟢 CONECTADO[/bold green]" if connected else "[bold red]🔴 SIN CONEXIÓN[/bold red]"

        # Header
        console.print(Panel(
            f"[bold cyan]@{trader}[/bold cyan]  |  ciclo #{cycle}  |  {datetime.now().strftime('%H:%M:%S')}  |  {conn_str}",
            style="blue"
        ))

        # Portfolio virtual
        info = Text()
        info.append(f"  Balance total : ", style="white")
        info.append(f"${portfolio.total_value:.2f} USDC\n", style="bold yellow")
        info.append(f"  Cash libre    : ", style="white")
        info.append(f"${portfolio.cash:.2f} USDC\n", style="white")
        info.append(f"  En posiciones : ", style="white")
        info.append(f"${portfolio.open_value:.2f} USDC\n", style="cyan")
        info.append(f"  P&L simulado  : ", style="white")
        info.append(f"{sign}${abs(pnl):.2f} ({sign}{portfolio.total_pnl_pct:.1f}%)", style=f"bold {color}")
        console.print(Panel(info, title="💰 Mi Portfolio Virtual", style="blue"))

        # Posiciones REALES del trader
        if trader_positions:
            table_real = Table(
                title=f"👁  Posiciones REALES de @{trader} (top 10)",
                box=box.ROUNDED, style="yellow", header_style="bold yellow"
            )
            table_real.add_column("Mercado",  width=45)
            table_real.add_column("Outcome",  width=8)
            table_real.add_column("Precio",   width=9)
            table_real.add_column("Valor $",  width=10)
            table_real.add_column("P&L %",    width=10)

            for pos in trader_positions[:10]:
                title    = pos.get("title", "?")[:45]
                outcome  = pos.get("outcome", "?")
                price    = float(pos.get("currentPrice", 0))
                value    = float(pos.get("value", 0))
                avg      = float(pos.get("avgPrice", price))
                pnl_pct  = ((price - avg) / avg * 100) if avg > 0 else 0
                pnl_sign = "+" if pnl_pct >= 0 else ""
                table_real.add_row(
                    title, outcome,
                    f"${price:.3f}",
                    f"${value:.2f}",
                    Text(f"{pnl_sign}{pnl_pct:.1f}%", style="green" if pnl_pct >= 0 else "red"),
                )
            console.print(table_real)
        else:
            console.print("[yellow]  Sin posiciones abiertas del trader disponibles[/yellow]")

        # Mis posiciones simuladas
        if portfolio.positions:
            table = Table(title="📊 Mis Posiciones Simuladas", box=box.ROUNDED,
                          style="cyan", header_style="bold cyan")
            table.add_column("#",       width=4)
            table.add_column("Mercado", width=40)
            table.add_column("Pos",     width=6)
            table.add_column("Entrada", width=9)
            table.add_column("Actual",  width=9)
            table.add_column("Costo",   width=9)
            table.add_column("P&L",     width=10)
            for pos in portfolio.positions:
                p = pos["pnl"]
                ps = f"+${p:.2f}" if p >= 0 else f"-${abs(p):.2f}"
                table.add_row(
                    str(pos["id"]), pos["title"], pos["outcome"],
                    f"${pos['entry_price']:.3f}", f"${pos['current_price']:.3f}",
                    f"${pos['cost']:.2f}", Text(ps, style="green" if p >= 0 else "red"),
                )
            console.print(table)
        else:
            console.print("[dim]  Sin posiciones simuladas aún — esperando nuevos trades del trader[/dim]")

        if portfolio.history:
            table2 = Table(title="📜 Cerrados (últimos 5)", box=box.SIMPLE,
                           style="dim", header_style="bold white")
            table2.add_column("Mercado", width=40)
            table2.add_column("P&L",     width=12)
            table2.add_column("Res.",    width=8)
            for pos in portfolio.history[-5:]:
                p = pos["pnl"]
                ps = f"+${p:.2f}" if p >= 0 else f"-${abs(p):.2f}"
                table2.add_row(pos["title"], Text(ps, style="green" if p >= 0 else "red"),
                               "✅ WIN" if p > 0 else "❌ LOSS")
            console.print(table2)

        console.print(f"\n  [dim]Próxima actualización en {POLL_INTERVAL}s... (Ctrl+C para resumen)[/dim]")

    else:
        print(f"\n{'='*60}")
        conn_str = "🟢 CONECTADO" if connected else "🔴 SIN CONEXIÓN"
        print(f"  @{trader} | Ciclo #{cycle} | {conn_str}")
        print(f"  Balance: ${portfolio.total_value:.2f} | Cash: ${portfolio.cash:.2f}")
        print(f"  P&L: {sign}${abs(pnl):.2f} ({sign}{portfolio.total_pnl_pct:.1f}%)")
        print(f"  Posiciones reales del trader: {len(trader_positions)}")
        print(f"  Mis posiciones simuladas: {len(portfolio.positions)}")
        print(f"{'='*60}")


class Simulator:
    def __init__(self):
        self.trader_username   = TRADER_USERNAME
        self.trader_address    = ""
        self.portfolio         = Portfolio(STARTING_BALANCE)
        self.seen_ids          = set()
        self.cycle             = 0
        self.connected         = False
        self.trader_positions  = []

    def setup(self):
        print(f"\n🚀 SIMULADOR POLYMARKET | @{self.trader_username} | ${STARTING_BALANCE} USDC virtuales\n")

        if not TRADER_ADDRESS:
            print("❌ TRADER_ADDRESS no configurada en .env")
            return False

        self.trader_address = TRADER_ADDRESS
        print(f"🔍 Wallet: {self.trader_address}")

        # Test de conexión
        print("🔌 Probando conexión con la API...")
        ok, has_data = test_connection(self.trader_address)
        if ok:
            self.connected = True
            print(f"✅ API conectada correctamente")
            if has_data:
                print(f"✅ El trader tiene actividad reciente")
            else:
                print(f"⚠️  No se encontró actividad reciente del trader")
        else:
            print(f"❌ Error de conexión: {has_data}")
            return False

        # Cargar posiciones actuales del trader
        print("📂 Cargando posiciones actuales del trader...")
        self.trader_positions = get_open_positions(self.trader_address)
        print(f"✅ {len(self.trader_positions)} posiciones abiertas encontradas")

        # Cargar historial para no repetir trades
        existing = get_recent_trades(self.trader_address, limit=50)
        for t in existing:
            tid = t.get("id") or t.get("transactionHash") or str(t)
            self.seen_ids.add(tid)
        print(f"📋 {len(self.seen_ids)} trades previos registrados\n")

        return True

    def poll(self):
        # Actualizar posiciones reales del trader
        self.trader_positions = get_open_positions(self.trader_address)

        # Revisar nuevos trades
        trades = get_recent_trades(self.trader_address, limit=10)
        self.connected = True

        new_trades = []
        for t in trades:
            tid = t.get("id") or t.get("transactionHash") or str(t)
            if tid not in self.seen_ids:
                new_trades.append((tid, t))

        for tid, trade in reversed(new_trades):
            self.seen_ids.add(tid)
            if trade.get("side", "").upper() not in ("BUY", "SELL"):
                continue
            pos = self.portfolio.add_position(trade)
            if pos:
                if RICH:
                    console.print(f"\n[bold yellow]🔔 NUEVO TRADE DETECTADO[/bold yellow]")
                    console.print(f"   Mercado : [cyan]{pos['title']}[/cyan]")
                    console.print(f"   Acción  : [white]{pos['side']} {pos['outcome']} @ ${pos['entry_price']:.4f}[/white]")
                    console.print(f"   Costo   : [yellow]${pos['cost']:.2f} USDC virtuales[/yellow]")
                else:
                    print(f"\n🔔 NUEVO TRADE: {pos['title']}")
                    print(f"   {pos['side']} {pos['outcome']} @ ${pos['entry_price']:.4f} = ${pos['cost']:.2f} USDC")
                time.sleep(1)

        self.portfolio.update_prices()
        self.portfolio.close_resolved()

    def run(self):
        if not self.setup():
            return

        print("👀 Monitoreando... (Ctrl+C para detener)\n")
        try:
            while True:
                self.cycle += 1
                try:
                    self.poll()
                    self.connected = True
                except Exception as e:
                    self.connected = False
                    print(f"⚠️  Error en ciclo: {e}")

                mostrar_estado(
                    self.portfolio,
                    self.trader_username,
                    self.cycle,
                    self.connected,
                    self.trader_positions
                )
                time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            p = self.portfolio
            sign = "+" if p.total_pnl >= 0 else ""
            print(f"\n{'='*50}")
            print(f"  RESUMEN FINAL")
            print(f"  Inicio : ${p.starting:.2f}")
            print(f"  Final  : ${p.total_value:.2f}")
            print(f"  P&L    : {sign}${abs(p.total_pnl):.2f} ({sign}{p.total_pnl_pct:.1f}%)")
            print(f"  Wins   : {p.wins} | Losses: {p.losses}")
            print(f"{'='*50}")


if __name__ == "__main__":
    sim = Simulator()
    sim.run()