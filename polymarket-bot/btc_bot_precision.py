import os, time, requests, threading, json
from datetime import datetime, timezone, timedelta
from collections import deque
from dotenv import load_dotenv

try:
    import websocket
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, OrderArgs, BalanceAllowanceParams, AssetType, OrderType, MarketOrderArgs
    from py_clob_client.constants import POLYGON
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

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

# ══════════════════════════════════════════════════════════
#  CONFIG — solo cambia estos valores
# ══════════════════════════════════════════════════════════
MODO_SIMULACION  = os.getenv("SIMULATION_MODE", "true").lower() == "true"
BET_AMOUNT       = float(os.getenv("BET_AMOUNT", "2.0"))
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "20.0"))
MAX_LOSS_STREAK  = int(os.getenv("MAX_LOSS_STREAK", "4"))
PAUSE_CYCLES     = int(os.getenv("PAUSE_CYCLES", "2"))

PRICE_MIN        = 0.84   # precio mínimo para entrar
PRICE_MAX        = 0.93   # techo duro — no entrar si precio >= este valor
WINDOW_START     = 45     # segundos antes del cierre donde empieza la ventana
WINDOW_END       = 15     # segundos antes del cierre donde termina la ventana
MOMENTUM_TICKS   = 4      # lecturas necesarias para confirmar señal

HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_history_btc_precision.json")

# Credenciales (mismas del .env)
PK              = os.getenv("PK")
FUNDER          = os.getenv("FUNDER", os.getenv("TRADER_ADDRESS"))
CLOB_API_KEY    = os.getenv("CLOB_API_KEY")
CLOB_SECRET     = os.getenv("CLOB_SECRET")
CLOB_PASSPHRASE = os.getenv("CLOB_PASSPHRASE")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"
DATA_API    = "https://data-api.polymarket.com"
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json"
}


# ══════════════════════════════════════════════════════════
#  MOTOR DE MERCADO (solo BTC)
# ══════════════════════════════════════════════════════════
class MarketEngine:
    def __init__(self):
        self.live_price    = 0.0
        self.target_price  = 0.0
        self.market_data   = None
        self.prices        = {"UP": 0.0, "DOWN": 0.0}
        self.price_history = {"UP": deque(maxlen=MOMENTUM_TICKS), "DOWN": deque(maxlen=MOMENTUM_TICKS)}
        self.token_ids     = {}
        self.seconds_left  = 0
        self.bet_placed    = False
        self.market_id     = None
        self._ask_books    = {"UP": {}, "DOWN": {}}
        self._ws           = None
        self._ws_subscribed_ids = set()
        self._ws_connected = False
        self._ws_has_data  = False

    def sync_live_price(self):
        while True:
            try:
                r = requests.get("https://api.binance.com/api/v3/ticker/price",
                                 params={"symbol": "BTCUSDT"}, timeout=2)
                self.live_price = float(r.json().get("price", 0))
            except:
                pass
            time.sleep(1)

    def get_target_historical(self, start_iso):
        try:
            if isinstance(start_iso, (int, float)):
                ts_ms = int(start_iso) * 1000
            else:
                ts_ms = int(datetime.fromisoformat(start_iso.replace("Z", "+00:00")).timestamp()) * 1000

            r = requests.get("https://api.binance.com/api/v3/klines",
                             params={"symbol": "BTCUSDT", "interval": "1m",
                                     "startTime": ts_ms, "limit": 1}, timeout=3)
            data = r.json()
            if isinstance(data, list) and data:
                return float(data[0][1])

            r2 = requests.get("https://api.binance.com/api/v3/klines",
                              params={"symbol": "BTCUSDT", "interval": "1m",
                                      "endTime": ts_ms, "limit": 1}, timeout=3)
            data2 = r2.json()
            if isinstance(data2, list) and data2:
                return float(data2[0][4])
        except:
            pass
        return self.live_price if self.live_price else 0.0

    def _best_ask(self, side):
        book   = self._ask_books[side]
        levels = [(float(p), s) for p, s in book.items() if s > 0]
        return min(levels)[0] if levels else 0.0

    def _start_ws(self):
        if not WS_AVAILABLE or not self.token_ids:
            return
        new_ids = set(self.token_ids.values())
        if new_ids == self._ws_subscribed_ids and self._ws_connected:
            return
        if self._ws:
            try: self._ws.close()
            except: pass
        self._ws_subscribed_ids = new_ids

        def on_message(ws, message):
            try:
                events = json.loads(message)
                if not isinstance(events, list): events = [events]
                for event in events:
                    asset_id = event.get("asset_id")
                    etype    = event.get("event_type")
                    side     = next((s for s, tid in self.token_ids.items() if tid == asset_id), None)
                    if not side: continue

                    if etype == "book":
                        self._ask_books[side] = {e["price"]: float(e["size"]) for e in event.get("sells", [])}
                    elif etype == "price_change":
                        for ch in event.get("changes", []):
                            if ch.get("side") == "SELL":
                                self._ask_books[side][ch["price"]] = float(ch["size"])

                    p = self._best_ask(side)
                    if p > 0:
                        self.prices[side] = p
                        self.price_history[side].append(p)
                        self._ws_has_data = True
            except:
                pass

        def on_open(ws):
            self._ws_connected = True
            ws.send(json.dumps({"assets_ids": list(self.token_ids.values()), "type": "market"}))

        def on_close(ws, *args):
            self._ws_connected = False
            self._ws_has_data  = False
            time.sleep(2)
            self._start_ws()

        def on_error(ws, err):
            self._ws_connected = False

        self._ws = websocket.WebSocketApp(
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            on_message=on_message, on_open=on_open,
            on_close=on_close, on_error=on_error
        )
        threading.Thread(target=self._ws.run_forever, daemon=True).start()

    def has_momentum(self, side):
        hist = [p for p in self.price_history[side] if p > 0]
        if len(hist) < 2: return False
        return all(PRICE_MIN <= p < PRICE_MAX for p in hist) and hist[-1] >= hist[0]

    def sync_market(self, now_ts):
        slug = f"btc-updown-5m-{now_ts}"

        if not self.market_data or self.seconds_left <= -10:
            try:
                r = requests.get(f"{GAMMA_API}/markets", params={"slug": slug},
                                 headers=HEADERS, timeout=3)
                if r.json():
                    new_data = r.json()[0]
                    if self.market_id != new_data.get("id"):
                        self.market_id  = new_data.get("id")
                        self.bet_placed = False
                        self.market_data = new_data
                        self.price_history = {"UP": deque(maxlen=MOMENTUM_TICKS), "DOWN": deque(maxlen=MOMENTUM_TICKS)}

                        c_ids = self.market_data.get("clobTokenIds")
                        if isinstance(c_ids, str): c_ids = json.loads(c_ids)
                        self.token_ids  = {"UP": c_ids[0], "DOWN": c_ids[1]}
                        self._ask_books = {"UP": {}, "DOWN": {}}
                        self._ws_has_data = False
                        self._start_ws()

                        start = self.market_data.get("eventStartTime") or self.market_data.get("startDate")
                        self.target_price = self.get_target_historical(start)
            except:
                pass

        if self.market_data and self.target_price == 0.0:
            start = self.market_data.get("eventStartTime") or self.market_data.get("startDate")
            if start:
                self.target_price = self.get_target_historical(start)

        if self.market_data:
            end_dt = datetime.fromisoformat(self.market_data.get("endDate").replace("Z", "+00:00"))
            self.seconds_left = int((end_dt - datetime.now(timezone.utc)).total_seconds())

            if not self._ws_has_data:
                def _fetch(side, tid):
                    try:
                        r = requests.get(f"{CLOB_API}/price",
                                         params={"token_id": tid, "side": "BUY"}, timeout=1)
                        p = float(r.json().get("price", 0))
                        self.prices[side] = p
                        if p > 0:
                            self.price_history[side].append(p)
                    except:
                        pass
                threads = [threading.Thread(target=_fetch, args=(s, tid), daemon=True)
                           for s, tid in self.token_ids.items()]
                for th in threads: th.start()
                for th in threads: th.join()


# ══════════════════════════════════════════════════════════
#  BOT BTC PRECISION
# ══════════════════════════════════════════════════════════
class BtcPrecisionBot:
    def __init__(self):
        self.btc   = MarketEngine()
        self.trades = self._load_history()
        self.cash   = STARTING_BALANCE
        self.initial_balance = STARTING_BALANCE
        self.wins   = 0
        self.losses = 0
        self.client = None
        self.real_ready = False
        self.cycle_count  = 0
        self.bets_made    = 0
        self.total_invested = 0.0
        self.best_pnl  = 0.0
        self.worst_pnl = 0.0
        self.streak    = 0
        self.start_time = datetime.now()
        self.pause_until_cycle = 0
        self.paused_reason = ""
        self.cycles_without_bet  = 0
        self.bet_placed_this_cycle = False
        self._prev_btc_seconds = 9999

        if not MODO_SIMULACION:
            self.setup_real()

    # ── Historial ────────────────────────────────────────
    def _load_history(self):
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE) as f:
                    return json.load(f)
            except:
                pass
        return []

    def save_history(self):
        try:
            with open(HISTORY_FILE, "w") as f:
                json.dump(self.trades, f, indent=2, default=str)
        except:
            pass

    # ── Autenticación real (igual que crypto_bot_elite) ──
    def setup_real(self):
        if not SDK_AVAILABLE or not PK or not CLOB_API_KEY:
            return
        try:
            creds = ApiCreds(
                api_key=CLOB_API_KEY,
                api_secret=CLOB_SECRET,
                api_passphrase=CLOB_PASSPHRASE
            )
            self.client = ClobClient(
                CLOB_API,
                key=PK,
                chain_id=POLYGON,
                creds=creds,
                funder=FUNDER,
                signature_type=1
            )
            if self.client.get_ok() == "OK":
                self.real_ready = True
                saldo = self.sync_real_balance()
                if saldo > 0:
                    self.cash = saldo
                    self.initial_balance = saldo
                print(f"[INFO] Conectado en REAL | Saldo: ${self.cash:.2f} USDC")
        except Exception as e:
            with open("order_error.log", "a") as f:
                f.write(f"\n[Auth Error] {datetime.now()}: {e}\n")
            self.real_ready = False

    # ── Balance real ─────────────────────────────────────
    def sync_real_balance(self):
        try:
            info = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            raw = info.get("balance", 0) if isinstance(info, dict) else 0
            return int(raw) / 1e6
        except Exception as e:
            with open("order_error.log", "a") as f:
                f.write(f"\n[Balance Error] {datetime.now()}: {e}\n")
            return 0.0

    def _refresh_balance(self):
        saldo = self.sync_real_balance()
        if saldo > 0:
            self.cash = saldo

    # ── Auto-redeem ──────────────────────────────────────
    def auto_redeem(self):
        if not PK or MODO_SIMULACION:
            return
        try:
            from eth_account import Account
            from eth_utils import to_checksum_address
            import eth_abi

            r = requests.get(f"{DATA_API}/positions",
                             params={"user": FUNDER, "sizeThreshold": 0.01}, timeout=5)
            if r.status_code != 200:
                return
            redeemables = [p for p in r.json() if p.get("redeemable")]
            if not redeemables:
                return

            account    = Account.from_key(PK)
            SELECTOR   = bytes.fromhex("01b7037c")
            PARENT_COLL = b'\x00' * 32

            nonce_resp = requests.post(POLYGON_RPC, json={
                "jsonrpc": "2.0", "method": "eth_getTransactionCount",
                "params": [account.address, "latest"], "id": 1
            }, timeout=5).json()
            nonce = int(nonce_resp["result"], 16)

            gp_resp = requests.post(POLYGON_RPC, json={
                "jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 2
            }, timeout=5).json()
            gas_price = int(int(gp_resp["result"], 16) * 1.2)

            redeemed = []
            for pos in redeemables:
                try:
                    condition_id = bytes.fromhex(pos["conditionId"].replace("0x", ""))
                    index_set    = 1 << pos.get("outcomeIndex", 0)
                    data = SELECTOR + eth_abi.encode(
                        ["address", "bytes32", "bytes32", "uint256[]"],
                        [to_checksum_address(USDC_POLYGON), PARENT_COLL, condition_id, [index_set]]
                    )
                    tx = {"nonce": nonce, "gasPrice": gas_price, "gas": 200000,
                          "to": to_checksum_address(CTF_CONTRACT), "value": 0,
                          "data": data, "chainId": 137}
                    signed   = account.sign_transaction(tx)
                    raw_hex  = "0x" + signed.raw_transaction.hex()
                    send_resp = requests.post(POLYGON_RPC, json={
                        "jsonrpc": "2.0", "method": "eth_sendRawTransaction",
                        "params": [raw_hex], "id": 3
                    }, timeout=10).json()
                    if "result" in send_resp:
                        redeemed.append(f"{pos['title'][:30]} ({pos['outcome']})")
                        nonce += 1
                    else:
                        with open("order_error.log", "a") as f:
                            f.write(f"\n[Redeem Error] {datetime.now()}: {send_resp}\n")
                except Exception as e:
                    with open("order_error.log", "a") as f:
                        f.write(f"\n[Redeem Tx] {datetime.now()}: {e}\n")

            if redeemed:
                self.telegram(
                    f"💰 <b>AUTO-REDEEM BTC</b>\n" + "\n".join(f"✅ {t}" for t in redeemed)
                )
                threading.Thread(target=self._refresh_balance, daemon=True).start()
        except Exception as e:
            with open("order_error.log", "a") as f:
                f.write(f"\n[AutoRedeem Error] {datetime.now()}: {e}\n")

    # ── Telegram ─────────────────────────────────────────
    def telegram(self, msg):
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            return
        def _send():
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                    timeout=5
                )
            except:
                pass
        threading.Thread(target=_send, daemon=True).start()

    # ── Apuesta ──────────────────────────────────────────
    def place_bet(self, side):
        token_id = self.btc.token_ids[side]
        price    = self.btc.prices[side]
        res_txt  = "VIRTUAL"

        if not MODO_SIMULACION and self.real_ready:
            try:
                order = MarketOrderArgs(
                    token_id=token_id,
                    amount=BET_AMOUNT,
                    side="BUY",
                )
                signed_order = self.client.create_market_order(order)
                resp = self.client.post_order(signed_order, OrderType.FOK)

                with open("order_error.log", "a") as f:
                    f.write(f"\n[Order Resp] {datetime.now()}: {resp}\n")

                if resp and resp.get("success"):
                    res_txt = "TAKER ✅"
                    saldo = self.sync_real_balance()
                    if saldo > 0:
                        self.cash = saldo
                else:
                    res_txt = "FAIL ❌"
                    with open("order_error.log", "a") as f:
                        f.write(f"\n[Order Fail] {datetime.now()}: {resp}\n")
                    return False
            except Exception as e:
                import traceback
                with open("order_error.log", "a") as f:
                    f.write(traceback.format_exc())
                return False
        else:
            self.cash -= BET_AMOUNT

        self.bets_made += 1
        self.total_invested += BET_AMOUNT
        end_iso = self.btc.market_data.get("endDate")

        self.trades.append({
            "cycle":     self.cycle_count,
            "time":      datetime.now().strftime("%H:%M:%S"),
            "asset":     "BTC",
            "side":      side,
            "target":    self.btc.target_price,
            "price":     price,
            "res":       f"{res_txt} (Pend)",
            "pnl":       0.0,
            "processed": False,
            "end_iso":   end_iso,
            "ticker":    "BTCUSDT"
        })
        self.btc.bet_placed = True
        self.save_history()

        modo_txt = "REAL" if self.real_ready and not MODO_SIMULACION else "SIM"
        self.telegram(
            f"🎯 <b>ENTRADA BTC</b>\n"
            f"Lado: <b>{side}</b> @ {price*100:.0f}c\n"
            f"Target: ${self.btc.target_price:,.2f} | Cierre en {self.btc.seconds_left}s\n"
            f"Balance: ${self.cash:.2f} | [{modo_txt}]"
        )
        return True

    # ── Precio de cierre ─────────────────────────────────
    def get_closing_price(self, end_iso):
        try:
            dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            ts_close = int(dt.timestamp() * 1000)
            r = requests.get("https://api.binance.com/api/v3/klines",
                             params={"symbol": "BTCUSDT", "interval": "1m",
                                     "startTime": ts_close, "limit": 1})
            if r.status_code == 200:
                data = r.json()
                if data: return float(data[0][1])
        except:
            pass
        return None

    # ── Resolver pendientes ──────────────────────────────
    def resolver_pendientes(self):
        now = datetime.now(timezone.utc)
        for t in self.trades:
            if t["processed"]: continue
            if not t.get("end_iso"): continue

            end_dt = datetime.fromisoformat(t["end_iso"].replace("Z", "+00:00"))
            if now <= (end_dt + timedelta(seconds=10)): continue

            close_price = self.get_closing_price(t["end_iso"])
            if not close_price: continue

            target = t["target"]
            won    = (close_price >= target) if t["side"] == "UP" else (close_price < target)

            if self.real_ready and not MODO_SIMULACION:
                saldo = self.sync_real_balance()
                if saldo > 0:
                    self.cash = saldo

            if won:
                profit = (BET_AMOUNT / t["price"]) - BET_AMOUNT
                self.wins += 1
                t["res"] = "WIN 🏆"
                t["pnl"] = profit
                self.streak = self.streak + 1 if self.streak >= 0 else 1
                if profit > self.best_pnl: self.best_pnl = profit
                self.telegram(
                    f"✅ <b>WIN — BTC {t['side']}</b>\n"
                    f"Cierre: ${close_price:,.2f} | Target: ${target:,.2f}\n"
                    f"P&L: +${profit:.2f} | Balance: ${self.cash:.2f}\n"
                    f"Racha: +{self.streak}W"
                )
            else:
                self.losses += 1
                t["res"] = "LOSS 💀"
                t["pnl"] = -BET_AMOUNT
                self.streak = self.streak - 1 if self.streak <= 0 else -1
                if -BET_AMOUNT < self.worst_pnl: self.worst_pnl = -BET_AMOUNT

                if self.streak <= -MAX_LOSS_STREAK:
                    self.pause_until_cycle = self.cycle_count + PAUSE_CYCLES
                    self.paused_reason = f"{MAX_LOSS_STREAK} pérdidas seguidas"
                    self.telegram(
                        f"⏸ <b>BOT BTC EN PAUSA</b>\n"
                        f"Racha negativa: {abs(self.streak)} pérdidas\n"
                        f"Reanuda en ciclo {self.pause_until_cycle} (~{PAUSE_CYCLES * 5} min)"
                    )

                self.telegram(
                    f"❌ <b>LOSS — BTC {t['side']}</b>\n"
                    f"Cierre: ${close_price:,.2f} | Target: ${target:,.2f}\n"
                    f"P&L: -${BET_AMOUNT:.2f} | Balance: ${self.cash:.2f}\n"
                    f"Racha: {self.streak}L"
                )

            t["processed"] = True
            self.save_history()

    # ── UI ───────────────────────────────────────────────
    def render(self):
        if not RICH: return
        console.clear()

        pnl       = self.cash - self.initial_balance
        roi       = (pnl / self.initial_balance * 100) if self.initial_balance else 0
        total_res = self.wins + self.losses
        win_rate  = (self.wins / total_res * 100) if total_res else 0
        pending   = sum(1 for t in self.trades if not t["processed"])
        elapsed   = datetime.now() - self.start_time
        elapsed_s = f"{int(elapsed.total_seconds()//3600):02d}:{int((elapsed.total_seconds()%3600)//60):02d}:{int(elapsed.total_seconds()%60):02d}"
        avg_pnl   = sum(t["pnl"] for t in self.trades if t["processed"]) / total_res if total_res else 0
        pnl_c     = "green" if pnl >= 0 else "red"
        is_paused = self.cycle_count < self.pause_until_cycle
        cycles_to_resume = max(0, self.pause_until_cycle - self.cycle_count)

        mode = "[bold green]DINERO REAL[/]" if self.real_ready else "[bold yellow]SIMULACIÓN[/]"

        # Panel de estadísticas
        stats = Table(box=box.SIMPLE_HEAD, show_header=False, expand=True, padding=(0, 1))
        stats.add_column(ratio=1); stats.add_column(ratio=1)
        stats.add_column(ratio=1); stats.add_column(ratio=1)

        stats.add_row(
            f"Modo: {mode}",
            f"Balance: [bold yellow]${self.cash:.2f}[/]",
            f"P&L: [{pnl_c}]{'+' if pnl>=0 else ''}${pnl:.2f}[/]",
            f"ROI: [{pnl_c}]{'+' if roi>=0 else ''}{roi:.1f}%[/]"
        )
        streak_txt = f"[green]+{self.streak}W[/]" if self.streak > 0 else f"[red]{self.streak}L[/]" if self.streak < 0 else "[dim]—[/]"
        stats.add_row(
            f"Wins: [green]{self.wins}[/]  Losses: [red]{self.losses}[/]",
            f"Win rate: [cyan]{win_rate:.1f}%[/]",
            f"Racha: {streak_txt}",
            f"P&L medio: {'[green]+' if avg_pnl>=0 else '[red]'}${avg_pnl:.2f}[/]"
        )
        no_bet_c = "red" if self.cycles_without_bet >= 3 else "yellow" if self.cycles_without_bet >= 1 else "green"
        tg_ok    = "[green]✔ Activo[/]" if (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID) else "[dim]No config.[/]"
        best_txt  = f"[green]+${self.best_pnl:.2f}[/]"  if self.best_pnl  else "[dim]—[/]"
        worst_txt = f"[red]${self.worst_pnl:.2f}[/]" if self.worst_pnl else "[dim]—[/]"
        stats.add_row(
            f"Ciclos: [bold]{self.cycle_count}[/]  Apuestas: [bold]{self.bets_made}[/]",
            f"Sin apostar: [{no_bet_c}]{self.cycles_without_bet}[/]  Pend: [yellow]{pending}[/]",
            f"Mejor: {best_txt}  Peor: {worst_txt}",
            f"Invertido: ${self.total_invested:.2f}  Tiempo: [dim]{elapsed_s}[/]"
        )
        if is_paused:
            stats.add_row(
                f"[bold red]⏸ PAUSADO — {self.paused_reason}[/]",
                f"[red]Reanuda en {cycles_to_resume} ciclo(s)[/]",
                f"Filtro: [cyan]{PRICE_MIN*100:.0f}c–{PRICE_MAX*100:.0f}c[/]  Ventana: [cyan]{WINDOW_START}s–{WINDOW_END}s[/]",
                f"Telegram: {tg_ok}"
            )
        else:
            stats.add_row(
                "[green]✔ ACTIVO[/]",
                f"Pausa tras: [red]{MAX_LOSS_STREAK} pérdidas[/] → {PAUSE_CYCLES} ciclos",
                f"Filtro: [cyan]{PRICE_MIN*100:.0f}c–{PRICE_MAX*100:.0f}c[/]  Ventana: [cyan]{WINDOW_START}s–{WINDOW_END}s[/]",
                f"Telegram: {tg_ok}"
            )

        border = "red" if is_paused else "blue"
        title  = "₿ BTC PRECISION BOT — [bold red]PAUSADO[/]" if is_paused else "₿ BTC PRECISION BOT"
        console.print(Panel(stats, title=title, border_style=border))

        # Panel live BTC
        eng  = self.btc
        diff = eng.live_price - eng.target_price if eng.target_price else 0
        status_txt = Text(f"▲ UP (+{diff:.2f})", style="bold green") if diff >= 0 else Text(f"▼ DOWN ({diff:.2f})", style="bold red")

        t_odds = Table(box=box.SIMPLE, show_header=False, padding=0, expand=True)
        for s in ["UP", "DOWN"]:
            p = eng.prices[s]
            if p >= PRICE_MAX:
                color = "yellow"
            elif PRICE_MIN <= p < PRICE_MAX:
                color = "green"
            elif p >= 0.5:
                color = "cyan"
            else:
                color = "white"
            mom_icon = " ▲" if eng.has_momentum(s) else ""
            bar = f"[{color}]{'█' * int(p*20)}{'░' * (20-int(p*20))}[/]"
            t_odds.add_row(s, bar, f"[{color}]{p*100:.0f}c{mom_icon}[/]")

        rem_m, rem_s = max(0, eng.seconds_left//60), max(0, eng.seconds_left%60)
        in_window    = WINDOW_END <= eng.seconds_left <= WINDOW_START

        best_side = max(["UP", "DOWN"], key=lambda s: eng.prices[s])
        best_prob = eng.prices[best_side]

        if eng.bet_placed:
            bet_style, bet_msg = "bold green", "✔ APOSTADO"
        elif is_paused:
            bet_style, bet_msg = "bold red", f"⏸ PAUSADO ({cycles_to_resume}c)"
        elif not in_window:
            secs_to_win = eng.seconds_left - WINDOW_START
            bet_style, bet_msg = "dim", f"Esperando ventana — faltan {secs_to_win}s"
        elif best_prob >= PRICE_MAX:
            bet_style, bet_msg = "yellow", f"TECHO — {best_side} {best_prob*100:.0f}c ≥ {PRICE_MAX*100:.0f}c"
        elif best_prob < PRICE_MIN:
            bet_style, bet_msg = "dim", f"Muy bajo — max {best_prob*100:.0f}c (mín {PRICE_MIN*100:.0f}c)"
        elif not eng.has_momentum(best_side):
            bet_style, bet_msg = "cyan", f"Esperando momentum — {best_side} {best_prob*100:.0f}c"
        else:
            bet_style, bet_msg = "cyan", "⏳ Evaluando..."

        info = Text()
        info.append(f"BTC", style="bold blue")
        info.append(f"  Live: ${eng.live_price:,.2f}\n", style="white")
        info.append(f"Target: ${eng.target_price:,.2f}\n", style="yellow")
        info.append("Estado: ", style="white"); info.append_text(status_txt)
        info.append(f"\n{bet_msg}\n", style=bet_style)

        timer_c = "red" if eng.seconds_left <= WINDOW_START else "yellow" if eng.seconds_left <= 60 else "white"
        console.print(Panel(
            Columns([info, t_odds]),
            title=f"BTC | [{timer_c}]{rem_m:02d}:{rem_s:02d}[/]",
            border_style="blue"
        ))

        # Historial
        hist = Table(title="📝 Historial BTC Precision", box=box.MINIMAL, expand=True)
        hist.add_column("#",         width=4)
        hist.add_column("Hora",      width=9)
        hist.add_column("Lado",      width=6)
        hist.add_column("Target",    width=12)
        hist.add_column("Entrada",   width=8)
        hist.add_column("Resultado", width=12)
        hist.add_column("P&L",       width=10)
        hist.add_column("Estado",    width=10)

        for t in reversed(self.trades[-12:]):
            if t["pnl"] > 0:
                pnl_txt = f"[green]+${t['pnl']:.2f}[/]"
            elif "LOSS" in str(t.get("res", "")):
                pnl_txt = f"[red]${t['pnl']:.2f}[/]"
            else:
                pnl_txt = "[dim]—[/]"
            estado   = "[dim]pend[/]" if not t["processed"] else "[green]✔[/]"
            target_t = f"${t['target']:,.2f}" if t["target"] else "—"
            s_color  = "green" if t["side"] == "UP" else "red"
            res_str  = str(t.get("res", ""))
            r_color  = "green" if "WIN" in res_str else "red" if "LOSS" in res_str else "yellow"
            hist.add_row(
                f"[dim]{t['cycle']}[/]", t["time"],
                f"[{s_color}]{t['side']}[/]", target_t,
                f"{t['price']*100:.0f}c",
                f"[{r_color}]{res_str}[/]", pnl_txt, estado
            )
        console.print(hist)

    # ── Loop principal ───────────────────────────────────
    def run(self):
        threading.Thread(target=self.btc.sync_live_price, daemon=True).start()

        while True:
            now_ts = (int(time.time()) // 300) * 300
            t = threading.Thread(target=self.btc.sync_market, args=(now_ts,), daemon=True)
            t.start(); t.join()

            is_paused = self.cycle_count < self.pause_until_cycle

            # Lógica de apuesta
            if not is_paused:
                eng = self.btc
                if WINDOW_END <= eng.seconds_left <= WINDOW_START and not eng.bet_placed:
                    up, dn = eng.prices["UP"], eng.prices["DOWN"]
                    side = None
                    if PRICE_MIN <= up < PRICE_MAX and eng.has_momentum("UP"):
                        side = "UP"
                    elif PRICE_MIN <= dn < PRICE_MAX and eng.has_momentum("DOWN"):
                        side = "DOWN"
                    if side:
                        if self.place_bet(side):
                            self.bet_placed_this_cycle = True

            # Detectar transición de ciclo
            if self._prev_btc_seconds > 0 and self.btc.seconds_left <= 0 and self.btc.market_data:
                self.cycle_count += 1
                if not self.bet_placed_this_cycle:
                    self.cycles_without_bet += 1
                else:
                    self.cycles_without_bet = 0
                self.bet_placed_this_cycle = False
                if self.real_ready and not MODO_SIMULACION:
                    threading.Thread(target=self._refresh_balance, daemon=True).start()
                    threading.Thread(target=self.auto_redeem, daemon=True).start()
            self._prev_btc_seconds = self.btc.seconds_left

            self.resolver_pendientes()
            self.render()
            time.sleep(0.5)


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    bot = BtcPrecisionBot()
    bot.run()
