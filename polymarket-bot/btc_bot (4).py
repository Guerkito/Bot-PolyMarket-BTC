import os
import time
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

TRADE_SIZE_USDC  = float(os.getenv("TRADE_SIZE_USDC", "10"))
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "100"))
POLL_INTERVAL    = 300   # 5 minutos
CONFIRM_WAIT     = 40    # segundos antes de apostar
THRESHOLD        = 0.85  # 85%

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"

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

# ─── API ──────────────────────────────────────────────────────────────────────

def get_btc_markets():
    """Obtiene mercados activos de Bitcoin up/down."""
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={
                "active": "true",
                "closed": "false",
                "tag_slug": "bitcoin",
                "limit": 50,
            },
            timeout=10,
        )
        data = resp.json()
        markets = data if isinstance(data, list) else data.get("markets", [])

        btc_markets = []
        keywords = ["bitcoin up", "bitcoin down", "btc up", "btc down",
                    "bitcoin above", "bitcoin below", "will bitcoin"]
        for m in markets:
            title = m.get("question", m.get("title", "")).lower()
            if any(k in title for k in keywords):
                btc_markets.append(m)
        return btc_markets
    except Exception as e:
        print(f"Error obteniendo mercados BTC: {e}")
        return []


def get_market_prices(market):
    """Obtiene precios actuales de los tokens de un mercado."""
    tokens = market.get("tokens", [])
    prices = {}
    for token in tokens:
        token_id = token.get("token_id", "")
        outcome  = token.get("outcome", "")
        if not token_id:
            continue
        try:
            resp = requests.get(
                f"{CLOB_API}/price",
                params={"token_id": token_id, "side": "BUY"},
                timeout=8,
            )
            price = float(resp.json().get("price", 0))
            prices[outcome] = {"price": price, "token_id": token_id}
        except:
            prices[outcome] = {"price": 0, "token_id": token_id}
    return prices


def get_orderbook_midpoint(token_id):
    """Obtiene el midpoint del orderbook para más precisión."""
    try:
        resp = requests.get(
            f"{CLOB_API}/midpoint",
            params={"token_id": token_id},
            timeout=8,
        )
        return float(resp.json().get("mid", 0))
    except:
        return 0.0

# ─── Portfolio ────────────────────────────────────────────────────────────────

class Portfolio:
    def __init__(self, balance):
        self.cash     = balance
        self.starting = balance
        self.positions = []
        self.history   = []
        self.wins      = 0
        self.losses    = 0
        self.trades    = 0

    def bet(self, market_title, outcome, token_id, price, size):
        if self.cash < 1:
            return None
        cost   = min(size, self.cash)
        tokens = cost / price
        self.cash  -= cost
        self.trades += 1
        pos = {
            "id":            self.trades,
            "title":         market_title[:50],
            "outcome":       outcome,
            "entry_price":   price,
            "tokens":        tokens,
            "cost":          cost,
            "token_id":      token_id,
            "opened_at":     datetime.now().strftime("%H:%M:%S"),
            "current_price": price,
            "pnl":           0.0,
            "status":        "OPEN",
        }
        self.positions.append(pos)
        return pos

    def update_prices(self):
        for pos in self.positions:
            if pos["status"] != "OPEN" or not pos.get("token_id"):
                continue
            try:
                resp = requests.get(
                    f"{CLOB_API}/price",
                    params={"token_id": pos["token_id"], "side": "BUY"},
                    timeout=8,
                )
                price = float(resp.json().get("price", 0))
                if price > 0:
                    pos["current_price"] = price
                    pos["pnl"] = (price - pos["entry_price"]) * pos["tokens"]
            except:
                pass

    def close_resolved(self):
        still_open = []
        for pos in self.positions:
            if pos["status"] != "OPEN":
                continue
            p = pos["current_price"]
            if p >= 0.97 or p <= 0.03:
                payout = pos["tokens"] * p
                self.cash += payout
                pos["status"] = "CLOSED"
                pos["pnl"]    = payout - pos["cost"]
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
        return sum(p["tokens"] * p["current_price"]
                   for p in self.positions if p["status"] == "OPEN")

    @property
    def total_value(self):
        return self.cash + self.open_value

    @property
    def total_pnl(self):
        return self.total_value - self.starting

    @property
    def total_pnl_pct(self):
        return (self.total_pnl / self.starting) * 100

# ─── Display ─────────────────────────────────────────────────────────────────

def mostrar_estado(portfolio, markets_data, cycle, next_check):
    pnl  = portfolio.total_pnl
    sign = "+" if pnl >= 0 else ""

    if RICH:
        console.clear()
        color = "green" if pnl >= 0 else "red"

        # Header
        console.print(Panel(
            f"[bold yellow]₿ BITCOIN BOT[/bold yellow]  |  "
            f"ciclo #{cycle}  |  {datetime.now().strftime('%H:%M:%S')}  |  "
            f"[dim]próximo scan en {next_check}s[/dim]",
            style="yellow"
        ))

        # Portfolio
        info = Text()
        info.append(f"  Balance total : ", style="white")
        info.append(f"${portfolio.total_value:.2f} USDC\n", style="bold yellow")
        info.append(f"  Cash libre    : ", style="white")
        info.append(f"${portfolio.cash:.2f} USDC\n", style="white")
        info.append(f"  En posiciones : ", style="white")
        info.append(f"${portfolio.open_value:.2f} USDC\n", style="cyan")
        info.append(f"  P&L total     : ", style="white")
        info.append(
            f"{sign}${abs(pnl):.2f} ({sign}{portfolio.total_pnl_pct:.1f}%)",
            style=f"bold {color}"
        )
        info.append(f"\n  Trades        : ", style="white")
        info.append(f"{portfolio.trades}  |  ", style="white")
        info.append(f"✅ {portfolio.wins} wins  ", style="green")
        info.append(f"❌ {portfolio.losses} losses", style="red")
        console.print(Panel(info, title="💰 Portfolio Virtual", style="yellow"))

        # Mercados BTC escaneados
        if markets_data:
            table = Table(
                title="₿ Mercados Bitcoin Activos",
                box=box.ROUNDED, style="yellow", header_style="bold yellow"
            )
            table.add_column("Mercado",   width=45)
            table.add_column("Up %",      width=8)
            table.add_column("Down %",    width=8)
            table.add_column("Señal",     width=15)

            for m in markets_data:
                title  = m["title"][:45]
                up_p   = m.get("up_price", 0)
                down_p = m.get("down_price", 0)

                if up_p >= THRESHOLD:
                    signal = Text("🔺 BET UP", style="bold green")
                elif down_p >= THRESHOLD:
                    signal = Text("🔻 BET DOWN", style="bold red")
                elif up_p > 0.5:
                    signal = Text(f"↑ UP lidera", style="green")
                elif down_p > 0.5:
                    signal = Text(f"↓ DOWN lidera", style="red")
                else:
                    signal = Text("⚖️  50/50", style="dim")

                up_str   = Text(f"{up_p*100:.1f}%",   style="green" if up_p > 0.5 else "dim")
                down_str = Text(f"{down_p*100:.1f}%", style="red" if down_p > 0.5 else "dim")

                table.add_row(title, up_str, down_str, signal)
            console.print(table)
        else:
            console.print("[dim]  Sin mercados BTC activos encontrados[/dim]")

        # Posiciones abiertas
        if portfolio.positions:
            table2 = Table(
                title="📊 Mis Posiciones",
                box=box.ROUNDED, style="cyan", header_style="bold cyan"
            )
            table2.add_column("#",       width=4)
            table2.add_column("Mercado", width=40)
            table2.add_column("Pos",     width=8)
            table2.add_column("Entrada", width=9)
            table2.add_column("Actual",  width=9)
            table2.add_column("P&L",     width=10)
            for pos in portfolio.positions:
                p  = pos["pnl"]
                ps = f"+${p:.2f}" if p >= 0 else f"-${abs(p):.2f}"
                table2.add_row(
                    str(pos["id"]), pos["title"], pos["outcome"],
                    f"${pos['entry_price']:.3f}",
                    f"${pos['current_price']:.3f}",
                    Text(ps, style="green" if p >= 0 else "red"),
                )
            console.print(table2)

        # Historial
        if portfolio.history:
            table3 = Table(title="📜 Cerrados", box=box.SIMPLE,
                           style="dim", header_style="bold white")
            table3.add_column("Mercado", width=40)
            table3.add_column("P&L",     width=12)
            table3.add_column("Res.",    width=8)
            for pos in portfolio.history[-5:]:
                p  = pos["pnl"]
                ps = f"+${p:.2f}" if p >= 0 else f"-${abs(p):.2f}"
                table3.add_row(
                    pos["title"],
                    Text(ps, style="green" if p >= 0 else "red"),
                    "✅ WIN" if p > 0 else "❌ LOSS"
                )
            console.print(table3)

    else:
        print(f"\n{'='*60}")
        print(f"  ₿ BTC BOT | Ciclo #{cycle} | {datetime.now().strftime('%H:%M:%S')}")
        print(f"  Balance: ${portfolio.total_value:.2f} | P&L: {sign}${abs(pnl):.2f}")
        for m in markets_data:
            print(f"  {m['title'][:40]} | UP:{m.get('up_price',0)*100:.1f}% DOWN:{m.get('down_price',0)*100:.1f}%")
        print(f"{'='*60}")

# ─── Bot ─────────────────────────────────────────────────────────────────────

class BitcoinBot:
    def __init__(self):
        self.portfolio     = Portfolio(STARTING_BALANCE)
        self.cycle         = 0
        self.bet_cooldown  = {}   # market_id -> timestamp ultima apuesta
        self.COOLDOWN_SECS = 600  # no apostar en el mismo mercado por 10 min

    def scan_and_bet(self):
        """Escanea mercados BTC y apuesta si hay señal fuerte."""
        markets = get_btc_markets()
        markets_data = []

        for market in markets:
            market_id = market.get("id", "")
            title     = market.get("question", market.get("title", ""))
            prices    = get_market_prices(market)

            up_price   = 0.0
            down_price = 0.0
            up_token   = ""
            down_token = ""

            for outcome, data in prices.items():
                outcome_lower = outcome.lower()
                if any(k in outcome_lower for k in ["up", "yes", "above", "higher"]):
                    up_price  = data["price"]
                    up_token  = data["token_id"]
                elif any(k in outcome_lower for k in ["down", "no", "below", "lower"]):
                    down_price = data["price"]
                    down_token = data["token_id"]

            # Si solo hay 2 tokens y no detectamos up/down, asignar por precio
            if not up_token and not down_token and len(prices) == 2:
                items = list(prices.items())
                up_price,   up_token   = items[0][1]["price"], items[0][1]["token_id"]
                down_price, down_token = items[1][1]["price"], items[1][1]["token_id"]

            markets_data.append({
                "title":      title,
                "id":         market_id,
                "up_price":   up_price,
                "down_price": down_price,
                "up_token":   up_token,
                "down_token": down_token,
            })

            # Verificar señal
            leading_price  = 0
            leading_outcome = ""
            leading_token  = ""

            if up_price >= THRESHOLD:
                leading_price   = up_price
                leading_outcome = "UP"
                leading_token   = up_token
            elif down_price >= THRESHOLD:
                leading_price   = down_price
                leading_outcome = "DOWN"
                leading_token   = down_token

            # Si hay señal fuerte
            if leading_price >= THRESHOLD and leading_token:
                # Verificar cooldown
                last_bet = self.bet_cooldown.get(market_id, 0)
                if time.time() - last_bet < self.COOLDOWN_SECS:
                    continue

                if RICH:
                    console.print(f"\n[bold yellow]⚡ SEÑAL DETECTADA[/bold yellow]")
                    console.print(f"   Mercado : [cyan]{title[:50]}[/cyan]")
                    console.print(f"   {leading_outcome} lidera con [bold green]{leading_price*100:.1f}%[/bold green]")
                    console.print(f"   [dim]Esperando {CONFIRM_WAIT}s para confirmar...[/dim]")
                else:
                    print(f"\n⚡ SEÑAL: {leading_outcome} @ {leading_price*100:.1f}%")
                    print(f"   Esperando {CONFIRM_WAIT}s para confirmar...")

                # Esperar y confirmar
                time.sleep(CONFIRM_WAIT)

                # Volver a verificar precios
                prices2 = get_market_prices(market)
                confirmed_price = 0
                for outcome, data in prices2.items():
                    outcome_lower = outcome.lower()
                    if leading_outcome == "UP" and any(k in outcome_lower for k in ["up", "yes", "above"]):
                        confirmed_price = data["price"]
                    elif leading_outcome == "DOWN" and any(k in outcome_lower for k in ["down", "no", "below"]):
                        confirmed_price = data["price"]

                if confirmed_price >= THRESHOLD:
                    pos = self.portfolio.bet(
                        title, leading_outcome, leading_token,
                        confirmed_price, TRADE_SIZE_USDC
                    )
                    if pos:
                        self.bet_cooldown[market_id] = time.time()
                        if RICH:
                            console.print(f"   [bold green]✅ APUESTA COLOCADA[/bold green]")
                            console.print(f"   ${pos['cost']:.2f} USDC en [bold]{leading_outcome}[/bold] @ ${confirmed_price:.4f}")
                        else:
                            print(f"✅ APUESTA: ${pos['cost']:.2f} en {leading_outcome} @ ${confirmed_price:.4f}")
                else:
                    if RICH:
                        console.print(f"   [yellow]⚠️  Señal no confirmada ({confirmed_price*100:.1f}%), cancelando[/yellow]")
                    else:
                        print(f"⚠️  Señal no confirmada, cancelando")

        return markets_data

    def run(self):
        if RICH:
            console.print(Panel(
                "[bold yellow]₿ BITCOIN BOT[/bold yellow]\n\n"
                f"  Umbral para apostar : [green]{THRESHOLD*100:.0f}%[/green]\n"
                f"  Espera confirmación : [white]{CONFIRM_WAIT}s[/white]\n"
                f"  Intervalo de scan   : [white]{POLL_INTERVAL//60} minutos[/white]\n"
                f"  Tamaño de apuesta   : [yellow]${TRADE_SIZE_USDC} USDC[/yellow]\n"
                f"  Balance inicial     : [yellow]${STARTING_BALANCE} USDC[/yellow]",
                title="⚙️  Configuración",
                style="yellow"
            ))
        else:
            print(f"\n₿ BITCOIN BOT | Umbral: {THRESHOLD*100:.0f}% | Confirmación: {CONFIRM_WAIT}s")

        time.sleep(2)

        try:
            while True:
                self.cycle += 1

                if RICH:
                    console.print(f"\n[dim]🔍 Escaneando mercados BTC...[/dim]")
                else:
                    print(f"\n🔍 Escaneando... (ciclo #{self.cycle})")

                markets_data = self.scan_and_bet()
                self.portfolio.update_prices()
                self.portfolio.close_resolved()

                # Countdown visible
                for remaining in range(POLL_INTERVAL, 0, -10):
                    mostrar_estado(self.portfolio, markets_data, self.cycle, remaining)
                    time.sleep(10)

        except KeyboardInterrupt:
            p = self.portfolio
            sign = "+" if p.total_pnl >= 0 else ""
            print(f"\n{'='*50}")
            print(f"  ₿ BITCOIN BOT — RESUMEN FINAL")
            print(f"  Inicio  : ${p.starting:.2f}")
            print(f"  Final   : ${p.total_value:.2f}")
            print(f"  P&L     : {sign}${abs(p.total_pnl):.2f} ({sign}{p.total_pnl_pct:.1f}%)")
            print(f"  Trades  : {p.trades}")
            print(f"  Wins    : {p.wins} | Losses: {p.losses}")
            if p.trades > 0:
                print(f"  Win rate: {p.wins/p.trades*100:.1f}%")
            print(f"{'='*50}")


if __name__ == "__main__":
    bot = BitcoinBot()
    bot.run()