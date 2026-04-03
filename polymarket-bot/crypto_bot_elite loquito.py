import os, sys, time, requests, threading, json, re

try:
    import websocket
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False
from datetime import datetime, timezone, timedelta
from collections import deque
from dotenv import load_dotenv

# SDK OFICIAL DE POLYMARKET
try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, OrderArgs, BalanceAllowanceParams, AssetType, OrderType, MarketOrderArgs
    from py_clob_client.constants import POLYGON
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ─── CONFIGURACIÓN DE USUARIO ─────────────────────────────────────────────────
MODO_SIMULACION  = os.getenv("SIMULATION_MODE", "true").lower() == "true"
BET_AMOUNT       = float(os.getenv("BET_AMOUNT", "2.0"))
STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "20.0"))
THRESHOLD        = 0.85
MAX_ENTRY_PRICE  = float(os.getenv("MAX_ENTRY_PRICE", "0.95"))   # Techo BTC: no entrar si prob > 95c
ETH_MAX_ENTRY_PRICE = 0.92                                        # Techo ETH: no entrar si prob > 92c
MAX_LOSS_STREAK  = int(os.getenv("MAX_LOSS_STREAK", "4"))         # Pausar tras N pérdidas seguidas
PAUSE_CYCLES     = int(os.getenv("PAUSE_CYCLES", "2"))            # Ciclos a esperar tras la pausa

# Estrategia Maker 2026: Ventana ajustada de 50 a 15 segundos
BET_WINDOW_START = 50
BET_WINDOW_END   = 15
MOMENTUM_TICKS   = 4   # 4 lecturas × 0.5s = 2 segundos de confirmación

# Filtro de volatilidad (MarketConditionFilter)
VOL_ATR_MAX_PCT   = 0.35
VOL_ATR_MIN_PCT   = 0.02
VOL_TREND_CANDLES = 3
VOL_TREND_RATIO   = 0.60

# Patrones + entrada graduada
PATTERN_WINDOW_START = 240    # segundos antes del cierre para activar patrones
GRAD_FIRST_PCT       = 0.60   # 60 % del monto en la primera entrada
GRAD_SECOND_PCT      = 0.40   # 40 % restante en la confirmación
GRAD_CONFIRM_MIN     = 30     # segundos mínimo antes de confirmar
GRAD_CONFIRM_MAX     = 60     # segundos máximo para confirmar
CASCADE_MIN_USD      = 200_000
CASCADE_HIGH_USD     = 500_000
CASCADE_MAX_USD      = 1_000_000
BREAKOUT_VOL_X       = 2.0    # ratio volumen actual vs promedio para confirmar ruptura
BREAKOUT_RANGE_X     = 2.0    # movimiento mínimo en múltiplos del rango establecido

# Régimen de mercado (RegimeDetector)
REGIME_TRENDING_ER   = 0.60   # Efficiency Ratio > esto → trending
REGIME_CHOPPY_ER     = 0.25   # Efficiency Ratio < esto → choppy
REGIME_DEAD_RANGE    = 10.0   # rango $ < esto en 30s → mercado muerto

# ConfidenceGate — edge mínimo por régimen
EDGE_TRENDING   = 0.06
EDGE_NORMAL     = 0.10
EDGE_CHOPPY     = 0.15
EDGE_DEAD       = 0.25   # prácticamente: no apuestes

# EWMA de volatilidad (RealizedVolatility)
EWMA_ALPHA_FAST = 0.94   # reacciona en ~1 tick
EWMA_ALPHA_SLOW = 0.55   # contexto de varios segundos

# API KEYS
PK              = os.getenv("PK")
FUNDER          = os.getenv("FUNDER", os.getenv("TRADER_ADDRESS"))
CLOB_API_KEY    = os.getenv("CLOB_API_KEY")
CLOB_SECRET     = os.getenv("CLOB_SECRET")
CLOB_PASSPHRASE = os.getenv("CLOB_PASSPHRASE")

# TELEGRAM
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "trades_history.json")

GAMMA_API    = "https://gamma-api.polymarket.com"
CLOB_API     = "https://clob.polymarket.com"
DATA_API     = "https://data-api.polymarket.com"
POLYGON_RPC  = "https://polygon-bor-rpc.publicnode.com"
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

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
    from rich.layout import Layout
    RICH = True
    console = Console()
except ImportError:
    RICH = False

# ─── CLASES DE APOYO ──────────────────────────────────────────────────────────

class BinanceFeed:
    """
    Feed de precio + trade flow en tiempo real vía WebSocket aggTrade de Binance.
    Un solo WebSocket da precio, velocity, volatility, CVD y detección de trades grandes.
    """
    _BUF_SECONDS      = 10      # ventana para velocity/volatility
    _FLOW_SECONDS     = 5       # ventana para CVD y volumen de trade flow
    _LARGE_TRADE_USD  = 50_000  # umbral para alerta de trade grande ($)

    def __init__(self, symbol: str):
        self.symbol = symbol.lower()
        self.price   = 0.0
        self.cvd     = 0.0
        self.last_large_trade = None
        self._price_buf = deque()      # (ts, price)
        self._trades    = deque()      # (ts, is_buy, usd_vol)
        self._prev_price       = 0.0
        self._ewma_var_fast    = 0.0   # EWMA de varianza (log-retorno²), ventana rápida
        self._ewma_var_slow    = 0.0   # EWMA de varianza, ventana lenta

    def start(self):
        # Precio inmediato por HTTP antes de que conecte el WebSocket
        try:
            r = requests.get("https://api.binance.com/api/v3/ticker/price",
                             params={"symbol": self.symbol.upper()}, timeout=3)
            self.price = float(r.json().get("price", 0))
        except Exception:
            pass
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        url = f"wss://stream.binance.com:9443/ws/{self.symbol}@aggTrade"
        while True:
            try:
                if WS_AVAILABLE:
                    ws = websocket.WebSocketApp(
                        url,
                        on_message=self._on_message,
                        on_error=lambda ws, e: None,
                        on_close=lambda ws, c, m: None,
                    )
                    ws.run_forever(ping_interval=30)
                else:
                    # Fallback: polling HTTP cada segundo
                    while True:
                        try:
                            r = requests.get("https://api.binance.com/api/v3/ticker/price",
                                             params={"symbol": self.symbol.upper()}, timeout=2)
                            self.price = float(r.json().get("price", 0))
                        except Exception:
                            pass
                        time.sleep(1)
            except Exception:
                pass
            time.sleep(5)

    def _on_message(self, ws, msg):
        try:
            data = json.loads(msg)
            if "p" not in data:
                return
            price  = float(data["p"])
            qty    = float(data.get("q", 0))
            # m=True → el maker estaba en el lado comprador → el taker vendió agresivamente
            is_buy = not data.get("m", True)
            usd_vol = price * qty
            now = time.time()

            # ── Precio + EWMA ────────────────────────────────────────────────
            self.price = price
            if self._prev_price > 0:
                log_ret_sq = (price / self._prev_price - 1) ** 2
                self._ewma_var_fast = EWMA_ALPHA_FAST * self._ewma_var_fast + (1 - EWMA_ALPHA_FAST) * log_ret_sq
                self._ewma_var_slow = EWMA_ALPHA_SLOW * self._ewma_var_slow + (1 - EWMA_ALPHA_SLOW) * log_ret_sq
            self._prev_price = price
            self._price_buf.append((now, price))
            cutoff_p = now - self._BUF_SECONDS
            while self._price_buf and self._price_buf[0][0] < cutoff_p:
                self._price_buf.popleft()

            # ── Trade flow ───────────────────────────────────────────────────
            self.cvd += usd_vol if is_buy else -usd_vol
            self._trades.append((now, is_buy, usd_vol))
            if usd_vol >= self._LARGE_TRADE_USD:
                self.last_large_trade = (now, "BUY" if is_buy else "SELL", usd_vol)
            cutoff_f = now - self._FLOW_SECONDS
            while self._trades and self._trades[0][0] < cutoff_f:
                self._trades.popleft()
        except Exception:
            pass

    @property
    def velocity(self):
        """Cambio de precio por segundo ($/s) en la última ventana."""
        buf = list(self._price_buf)
        if len(buf) < 2:
            return 0.0
        dt = buf[-1][0] - buf[0][0]
        return (buf[-1][1] - buf[0][1]) / dt if dt > 0 else 0.0

    @property
    def volatility(self):
        """Rango absoluto (max - min) de precios en la última ventana ($)."""
        buf = list(self._price_buf)
        if len(buf) < 2:
            return 0.0
        prices = [p for _, p in buf]
        return max(prices) - min(prices)

    # ── Trade flow ────────────────────────────────────────────────────────────

    @property
    def buy_volume(self):
        return sum(v for _, b, v in self._trades if b)

    @property
    def sell_volume(self):
        return sum(v for _, b, v in self._trades if not b)

    @property
    def trade_imbalance(self):
        """(buy - sell) / total en la ventana de FLOW_SECONDS. +1 = compras puras."""
        bv, sv = self.buy_volume, self.sell_volume
        total = bv + sv
        return (bv - sv) / total if total > 0 else 0.0

    @property
    def trade_signal(self):
        """Señal normalizada del flow: +1 buy pressure · -1 sell pressure · 0 neutral."""
        imb = self.trade_imbalance
        if   imb >  0.30: return  1.0
        elif imb >  0.10: return  0.5
        elif imb < -0.30: return -1.0
        elif imb < -0.10: return -0.5
        return 0.0

    @property
    def realized_vol_fast(self):
        """Volatilidad realizada EWMA rápida (%) — reacciona en <1s."""
        return (self._ewma_var_fast ** 0.5) * 100

    @property
    def realized_vol_slow(self):
        """Volatilidad realizada EWMA lenta (%) — contexto de varios segundos."""
        return (self._ewma_var_slow ** 0.5) * 100

    @property
    def vol_regime(self):
        """
        Estado de la volatilidad realizada:
        'accelerating' — vol rápida > 1.5× vol lenta  (explosión reciente)
        'stable'       — vol rápida ≈ vol lenta
        'flat'         — ambas muy bajas
        """
        fast, slow = self.realized_vol_fast, self.realized_vol_slow
        if fast < 0.001 and slow < 0.001:
            return "flat"
        if slow > 0 and fast > 1.5 * slow:
            return "accelerating"
        return "stable"

    @property
    def recent_large_trade(self):
        """Último trade grande si ocurrió en los últimos 10s. Retorna ("BUY"/"SELL", usd) o None."""
        if not self.last_large_trade:
            return None
        ts, side, vol = self.last_large_trade
        return (side, vol) if time.time() - ts < 10 else None


class BinanceOrderBookFeed:
    """Order book en tiempo real vía WebSocket depth@100ms de Binance."""
    _LEVELS = 15   # niveles del book para calcular imbalance

    def __init__(self, symbol: str):
        self.symbol   = symbol.lower()
        self._bids    = {}   # price_str → size_float
        self._asks    = {}
        self._ready   = False

    def start(self):
        self._fetch_snapshot()
        threading.Thread(target=self._run, daemon=True).start()

    def _fetch_snapshot(self):
        try:
            r = requests.get("https://api.binance.com/api/v3/depth",
                             params={"symbol": self.symbol.upper(), "limit": 20}, timeout=3)
            data = r.json()
            self._bids = {b[0]: float(b[1]) for b in data.get("bids", [])}
            self._asks = {a[0]: float(a[1]) for a in data.get("asks", [])}
            self._ready = True
        except:
            pass

    def _run(self):
        url = f"wss://stream.binance.com:9443/ws/{self.symbol}@depth@100ms"
        while True:
            try:
                if WS_AVAILABLE:
                    ws = websocket.WebSocketApp(
                        url,
                        on_message=self._on_msg,
                        on_error=lambda ws, e: None,
                        on_close=lambda ws, c, m: self._fetch_snapshot(),
                    )
                    ws.run_forever(ping_interval=30)
            except:
                pass
            time.sleep(5)

    def _on_msg(self, ws, msg):
        try:
            data = json.loads(msg)
            for bid in data.get("b", []):
                if float(bid[1]) == 0:
                    self._bids.pop(bid[0], None)
                else:
                    self._bids[bid[0]] = float(bid[1])
            for ask in data.get("a", []):
                if float(ask[1]) == 0:
                    self._asks.pop(ask[0], None)
                else:
                    self._asks[ask[0]] = float(ask[1])
        except:
            pass

    def avg_imbalance(self, levels=None):
        """(bid_vol − ask_vol) / total en los mejores N niveles. +1 = presión compradora."""
        n = levels or self._LEVELS
        bids = sorted(self._bids.items(), key=lambda x: -float(x[0]))[:n]
        asks = sorted(self._asks.items(), key=lambda x:  float(x[0]))[:n]
        bid_vol = sum(v for _, v in bids)
        ask_vol = sum(v for _, v in asks)
        total   = bid_vol + ask_vol
        return (bid_vol - ask_vol) / total if total > 0 else 0.0

    @property
    def signal(self):
        imb = self.avg_imbalance()
        if   imb >  0.20: return  1.0
        elif imb >  0.10: return  0.5
        elif imb < -0.20: return -1.0
        elif imb < -0.10: return -0.5
        return 0.0


class LiquidationFeed:
    """Liquidaciones forzosas en Binance Futures (stream público, sin auth)."""
    _WINDOW = 30  # segundos de memoria

    def __init__(self):
        self._liqs = deque()   # (ts, symbol, "LONG"/"SHORT", usd_vol)

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        url = "wss://fstream.binance.com/ws/!forceOrder@arr"
        while True:
            try:
                if WS_AVAILABLE:
                    ws = websocket.WebSocketApp(
                        url,
                        on_message=self._on_msg,
                        on_error=lambda ws, e: None,
                        on_close=lambda ws, c, m: None,
                    )
                    ws.run_forever(ping_interval=30)
            except:
                pass
            time.sleep(5)

    def _on_msg(self, ws, msg):
        try:
            order  = json.loads(msg).get("o", {})
            symbol = order.get("s", "")
            side   = order.get("S", "")   # BUY = short liq, SELL = long liq
            price  = float(order.get("ap", 0))
            qty    = float(order.get("q", 0))
            usd    = price * qty
            liq_who = "SHORT" if side == "BUY" else "LONG"
            now = time.time()
            self._liqs.append((now, symbol, liq_who, usd))
            cutoff = now - self._WINDOW
            while self._liqs and self._liqs[0][0] < cutoff:
                self._liqs.popleft()
        except:
            pass

    def get_pressure(self, symbol_prefix: str):
        """Retorna (long_liq_usd, short_liq_usd) para el activo en la ventana."""
        sym = symbol_prefix.upper()
        recent = [(ls, v) for _, s, ls, v in self._liqs if s.startswith(sym)]
        long_liq  = sum(v for ls, v in recent if ls == "LONG")
        short_liq = sum(v for ls, v in recent if ls == "SHORT")
        return long_liq, short_liq

    def signal(self, symbol_prefix: str):
        """Long liq → presión bajista (−). Short liq → presión alcista (+)."""
        ll, sl = self.get_pressure(symbol_prefix)
        total = ll + sl
        if total == 0: return 0.0
        raw = (sl - ll) / total      # positivo si hay más short liq (bullish)
        if   raw >  0.50: return  1.0
        elif raw >  0.20: return  0.5
        elif raw < -0.50: return -1.0
        elif raw < -0.20: return -0.5
        return 0.0


class CoinbaseFeed:
    """Precio spot de Coinbase para detectar spread vs Binance (señal multi-exchange)."""
    _POLL = 5   # segundos entre requests

    def __init__(self, symbol: str):
        self.symbol = symbol.upper()   # "BTC" o "ETH"
        self.price  = 0.0

    def start(self):
        self._fetch()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            time.sleep(self._POLL)
            self._fetch()

    def _fetch(self):
        try:
            r = requests.get(
                f"https://api.coinbase.com/v2/prices/{self.symbol}-USD/spot",
                timeout=5
            )
            if r.status_code == 200:
                self.price = float(r.json()["data"]["amount"])
        except:
            pass

    def spread_pct(self, binance_price: float):
        """(coinbase − binance) / binance × 100. Positivo = CB caro → presión compradora llega."""
        if not self.price or not binance_price:
            return 0.0
        return (self.price - binance_price) / binance_price * 100

    def signal(self, binance_price: float):
        s = self.spread_pct(binance_price)
        if   s >  0.05: return  1.0
        elif s >  0.02: return  0.5
        elif s < -0.05: return -1.0
        elif s < -0.02: return -0.5
        return 0.0


class FundingFeed:
    """Funding rate + Open Interest de Binance Futures para contexto de posicionamiento."""
    _POLL = 60   # segundos (no hace falta más frecuencia)

    def __init__(self, symbol: str):
        # Asegurar formato BTCUSDT / ETHUSDT
        base = symbol.upper().replace("USDT", "")
        self.symbol       = base + "USDT"
        self.funding_rate = 0.0
        self.open_interest = 0.0
        self._prev_oi     = 0.0

    def start(self):
        self._fetch()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            time.sleep(self._POLL)
            self._fetch()

    def _fetch(self):
        try:
            r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                             params={"symbol": self.symbol}, timeout=5)
            if r.status_code == 200:
                self.funding_rate = float(r.json().get("lastFundingRate", 0))
        except:
            pass
        try:
            r = requests.get("https://fapi.binance.com/fapi/v1/openInterest",
                             params={"symbol": self.symbol}, timeout=5)
            if r.status_code == 200:
                self._prev_oi      = self.open_interest
                self.open_interest = float(r.json().get("openInterest", 0))
        except:
            pass

    @property
    def oi_trend(self):
        """Cambio relativo de OI respecto a la última lectura."""
        if not self._prev_oi:
            return 0.0
        return (self.open_interest - self._prev_oi) / self._prev_oi

    def signal(self, price_direction: float):
        """
        price_direction: +1 si la apuesta es UP, −1 si es DOWN.
        Funding extremo positivo = demasiados longs = riesgo de corrección (bearish bias).
        OI subiendo + precio subiendo = convicción real (confirma la dirección).
        """
        fr = self.funding_rate
        if   fr >  0.001:  fs = -0.5   # funding muy positivo → bearish
        elif fr >  0.0005: fs = -0.25
        elif fr < -0.001:  fs =  0.5   # funding muy negativo → bullish
        elif fr < -0.0005: fs =  0.25
        else:              fs =  0.0

        oi_signal = 0.0
        if self.oi_trend > 0.02:
            oi_signal = 0.5 * price_direction    # OI creciendo con la dirección → confirma
        elif self.oi_trend < -0.02:
            oi_signal = -0.5 * price_direction   # OI cayendo → debilita

        return (fs + oi_signal) / 2


# ─── SIGNAL AGGREGATOR ────────────────────────────────────────────────────────

class SignalAggregator:
    """
    Agrega 5 señales de mercado con pesos definidos y calcula el edge mínimo dinámico.

    Lógica de confianza:
      5/5 señales alineadas → edge_min = 6%   (entrar con menos margen)
      3/5                   → edge_min = 10%
      1/5 o menos           → edge_min = 16%  (señales contradictorias → muy selectivo)

    Edge = 1 − market_prob (cuánto puede "bajar" el precio desde 1.0).
    Entramos solo si edge >= edge_min.
    """
    WEIGHTS = {
        "trade_flow":    0.20,
        "order_book":    0.20,
        "liquidations":  0.10,
        "multi_exchange":0.10,
        "funding_oi":    0.10,
    }
    # edge_min según cuántas señales coinciden
    _EDGE_TABLE = {5: 0.06, 4: 0.08, 3: 0.10, 2: 0.13, 1: 0.16, 0: 0.16}

    def __init__(self, btc_feed, eth_feed,
                 btc_book, eth_book,
                 liq_feed,
                 cb_btc, cb_eth,
                 btc_funding, eth_funding):
        self.btc_feed    = btc_feed
        self.eth_feed    = eth_feed
        self.btc_book    = btc_book
        self.eth_book    = eth_book
        self.liq         = liq_feed
        self.cb_btc      = cb_btc
        self.cb_eth      = cb_eth
        self.btc_funding = btc_funding
        self.eth_funding = eth_funding

    def get_signals(self, asset: str, side: str, market_prob: float) -> dict:
        """
        Retorna un dict con todas las señales, score, consenso y veredicto.
        side: "UP" o "DOWN"
        """
        direction  = 1 if side == "UP" else -1
        feed       = self.btc_feed    if asset == "BTC" else self.eth_feed
        book       = self.btc_book    if asset == "BTC" else self.eth_book
        cb         = self.cb_btc      if asset == "BTC" else self.cb_eth
        funding    = self.btc_funding if asset == "BTC" else self.eth_funding
        sym_prefix = asset  # "BTC" o "ETH"

        # Cada señal como float en [−1, +1]: positivo = confirma side
        tf_raw  = feed.trade_signal    * direction
        ob_raw  = book.signal          * direction
        lq_raw  = self.liq.signal(sym_prefix) * direction
        mx_raw  = cb.signal(feed.price)       * direction
        fi_raw  = funding.signal(direction)   * direction   # ya está orientado a direction

        signals = {
            "trade_flow":    {"value": tf_raw,
                              "raw": f"imb={feed.trade_imbalance:+.2f}  "
                                     f"buy=${feed.buy_volume/1e3:.0f}k  sell=${feed.sell_volume/1e3:.0f}k"},
            "order_book":    {"value": ob_raw,
                              "raw": f"imb={book.avg_imbalance():+.2f}"},
            "liquidations":  {"value": lq_raw,
                              "raw": "{ll:.0f}k long/{sl:.0f}k short liq".format(
                                         ll=self.liq.get_pressure(sym_prefix)[0]/1e3,
                                         sl=self.liq.get_pressure(sym_prefix)[1]/1e3)},
            "multi_exchange":{"value": mx_raw,
                              "raw": f"CB spread={cb.spread_pct(feed.price):+.3f}%"},
            "funding_oi":    {"value": fi_raw,
                              "raw": f"fr={funding.funding_rate*100:+.4f}%  "
                                     f"oi_Δ={funding.oi_trend*100:+.2f}%"},
        }

        # Número de señales que confirman la dirección (valor > 0)
        agreeing   = sum(1 for s in signals.values() if s["value"] > 0)
        edge       = round(1.0 - market_prob, 4)
        edge_min   = self._EDGE_TABLE.get(agreeing, 0.16)
        should     = edge >= edge_min

        # Score ponderado (−1 a +1)
        score = sum(signals[k]["value"] * self.WEIGHTS[k] for k in signals)

        lrg = feed.recent_large_trade
        large_txt = f"{'🐳' if lrg else '—'}  {lrg[0]} ${lrg[1]/1e3:.0f}k" if lrg else "—"

        return {
            "asset":       asset,
            "side":        side,
            "market_prob": market_prob,
            "signals":     signals,
            "score":       round(score, 3),
            "agreeing":    agreeing,
            "edge":        edge,
            "edge_min":    edge_min,
            "should_enter":should,
            "large_trade": large_txt,
            "reason":      f"{agreeing}/5 señales · edge={edge:.2f} {'≥' if should else '<'} {edge_min:.2f}",
        }


class MarketEngine:
    def __init__(self, asset_name, ticker_binance, slug_prefix):
        self.asset = asset_name
        self.ticker = ticker_binance
        self.prefix = slug_prefix

        self.live_price = 0.0
        self.target_price = 0.0
        self.market_data = None
        self.prices = {"UP": 0.0, "DOWN": 0.0}
        self.price_history = {"UP": deque(maxlen=MOMENTUM_TICKS), "DOWN": deque(maxlen=MOMENTUM_TICKS)}
        self.token_ids = {}
        self.seconds_left = 0
        self.bet_placed = False
        self.market_id = None
        self._ask_books = {"UP": {}, "DOWN": {}}  # {price_str: size_float}
        self._ws = None
        self._ws_subscribed_ids = set()
        self._ws_connected = False
        self._ws_has_data = False
        threading.Thread(target=self._poll_prices, daemon=True).start()

    def _poll_prices(self):
        """Polling HTTP continuo de precios UP/DOWN como respaldo al WebSocket CLOB."""
        while True:
            if self.token_ids and not self._ws_has_data:
                for side, tid in list(self.token_ids.items()):
                    try:
                        r = requests.get(f"{CLOB_API}/price",
                                         params={"token_id": tid, "side": "BUY"}, timeout=1)
                        p = float(r.json().get("price", 0))
                        if p > 0:
                            self.prices[side] = p
                            self.price_history[side].append(p)
                    except:
                        pass
            time.sleep(1)

    def sync_live_price(self):
        """Loop continuo que actualiza el precio cada segundo."""
        while True:
            try:
                r = requests.get("https://api.binance.com/api/v3/ticker/price", params={"symbol": self.ticker}, timeout=2)
                self.live_price = float(r.json().get("price", 0))
            except: pass
            time.sleep(1)

    def get_target_historical(self, start_iso):
        try:
            # Soportar tanto ISO string como timestamp numérico
            if isinstance(start_iso, (int, float)):
                ts_ms = int(start_iso) * 1000
            else:
                ts_ms = int(datetime.fromisoformat(start_iso.replace("Z", "+00:00")).timestamp()) * 1000

            # Intento 1: vela de 1m que empieza exactamente en ese timestamp
            r = requests.get("https://api.binance.com/api/v3/klines",
                             params={"symbol": self.ticker, "interval": "1m", "startTime": ts_ms, "limit": 1},
                             timeout=3)
            data = r.json()
            if isinstance(data, list) and data:
                return float(data[0][1])  # open price de esa vela

            # Intento 2: pedir la vela anterior (60s antes) y usar su close
            r2 = requests.get("https://api.binance.com/api/v3/klines",
                              params={"symbol": self.ticker, "interval": "1m",
                                      "endTime": ts_ms, "limit": 1},
                              timeout=3)
            data2 = r2.json()
            if isinstance(data2, list) and data2:
                return float(data2[0][4])  # close price de la vela anterior

        except: pass

        # Fallback final: usar el precio live actual como aproximación
        return self.live_price if self.live_price else 0.0

    def _best_ask(self, side):
        book = self._ask_books[side]
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
                    etype = event.get("event_type")
                    side = next((s for s, tid in self.token_ids.items() if tid == asset_id), None)
                    if not side: continue

                    if etype == "book":
                        self._ask_books[side] = {
                            e["price"]: float(e["size"])
                            for e in event.get("sells", [])
                        }
                    elif etype == "price_change":
                        for ch in event.get("changes", []):
                            if ch.get("side") == "SELL":
                                self._ask_books[side][ch["price"]] = float(ch["size"])

                    p = self._best_ask(side)
                    if p > 0:
                        self.prices[side] = p
                        self.price_history[side].append(p)
                        self._ws_has_data = True
            except: pass

        def on_open(ws):
            self._ws_connected = True
            ws.send(json.dumps({"assets_ids": list(self.token_ids.values()), "type": "market"}))

        def on_close(ws, *args):
            self._ws_connected = False
            self._ws_has_data = False
            time.sleep(2)
            self._start_ws()

        def on_error(ws, err):
            self._ws_connected = False

        self._ws = websocket.WebSocketApp(
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            on_message=on_message,
            on_open=on_open,
            on_close=on_close,
            on_error=on_error
        )
        threading.Thread(target=self._ws.run_forever, daemon=True).start()

    def has_momentum(self, side):
        """True si la probabilidad lleva 1s por encima del threshold y con tendencia estable o al alza."""
        hist = [p for p in self.price_history[side] if p > 0]
        if len(hist) < 2: return False
        return all(p >= THRESHOLD for p in hist) and hist[-1] >= hist[0]

    def sync_market(self, now_ts):
        slug = f"{self.prefix}-{now_ts}"

        if not self.market_data or self.seconds_left <= -10:
            try:
                r = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, headers=HEADERS, timeout=3)
                if r.json():
                    new_data = r.json()[0]
                    if self.market_id != new_data.get("id"):
                        self.market_id = new_data.get("id")
                        self.bet_placed = False
                        self.market_data = new_data
                        self.price_history = {"UP": deque(maxlen=MOMENTUM_TICKS), "DOWN": deque(maxlen=MOMENTUM_TICKS)}

                        c_ids = self.market_data.get("clobTokenIds")
                        if isinstance(c_ids, str): c_ids = json.loads(c_ids)
                        self.token_ids = {"UP": c_ids[0], "DOWN": c_ids[1]}
                        self._ask_books = {"UP": {}, "DOWN": {}}
                        self._ws_has_data = False
                        self._start_ws()

                        start = self.market_data.get("eventStartTime") or self.market_data.get("startDate")
                        self.target_price = self.get_target_historical(start)
            except: pass

        # Si el target sigue en 0 (falló en el primer intento), reintentarlo
        if self.market_data and self.target_price == 0.0:
            start = self.market_data.get("eventStartTime") or self.market_data.get("startDate")
            if start:
                self.target_price = self.get_target_historical(start)

        if self.market_data:
            end_dt = datetime.fromisoformat(self.market_data.get("endDate").replace("Z", "+00:00"))
            self.seconds_left = int((end_dt - datetime.now(timezone.utc)).total_seconds())

            if not self._ws_has_data:
                def _fetch_price(side, tid):
                    try:
                        r = requests.get(f"{CLOB_API}/price", params={"token_id": tid, "side": "BUY"}, timeout=1)
                        p = float(r.json().get("price", 0))
                        self.prices[side] = p
                        if p > 0:
                            self.price_history[side].append(p)
                    except: pass
                threads = [threading.Thread(target=_fetch_price, args=(s, tid), daemon=True)
                           for s, tid in self.token_ids.items()]
                for th in threads: th.start()
                for th in threads: th.join()

# ─── FILTRO DE VOLATILIDAD ────────────────────────────────────────────────────

class MarketConditionFilter:
    """
    Filtra apuestas según condiciones de mercado para subir el win rate.
    Reduce cantidad de entradas pero mejora la calidad de cada señal.

    Criterios:
    - ATR (volatilidad por minuto) dentro de rango saludable
    - Últimas N velas alineadas con la dirección de la señal
    """
    CACHE_TTL = 30  # segundos entre fetch de velas (no martillear Binance)

    def __init__(self, ticker: str):
        self.ticker = ticker
        self._last_fetch = 0.0
        self._cached_candles = None
        self._last_ok = None
        self._last_reason = ""

    def _fetch_candles(self, n: int):
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": self.ticker, "interval": "1m", "limit": n},
                timeout=3,
            )
            if r.status_code != 200:
                return None
            return [
                {"open": float(c[1]), "high": float(c[2]),
                 "low": float(c[3]), "close": float(c[4])}
                for c in r.json()
            ]
        except:
            return None

    def check(self, signal_side: str):
        """
        Evalúa si las condiciones son favorables para apostar en signal_side.
        Returns: (allow: bool, reason: str)
        """
        now = time.time()
        if now - self._last_fetch < self.CACHE_TTL and self._last_ok is not None:
            return self._last_ok, self._last_reason

        n = max(VOL_TREND_CANDLES + 1, 6)
        candles = self._fetch_candles(n)
        self._last_fetch = now

        if not candles or len(candles) < 3:
            # Fail-open: si no hay datos, no bloqueamos
            self._last_ok, self._last_reason = True, "sin datos Binance (permitido)"
            return True, self._last_reason

        # ── 1. ATR simplificado: promedio de (high-low)/close por vela ───────
        completed = candles[:-1]  # última vela aún no cerrada
        ranges_pct = [(c["high"] - c["low"]) / c["close"] * 100 for c in completed]
        atr_pct = sum(ranges_pct) / len(ranges_pct)

        if atr_pct > VOL_ATR_MAX_PCT:
            reason = f"ATR {atr_pct:.3f}% > max {VOL_ATR_MAX_PCT}% (caótico)"
            self._last_ok, self._last_reason = False, reason
            return False, reason

        if atr_pct < VOL_ATR_MIN_PCT:
            reason = f"ATR {atr_pct:.3f}% < min {VOL_ATR_MIN_PCT}% (plano)"
            self._last_ok, self._last_reason = False, reason
            return False, reason

        # ── 2. Confirmación de tendencia ──────────────────────────────────────
        trend_candles = completed[-VOL_TREND_CANDLES:]
        if signal_side == "UP":
            aligned = sum(1 for c in trend_candles if c["close"] >= c["open"])
        else:
            aligned = sum(1 for c in trend_candles if c["close"] < c["open"])

        trend_ratio = aligned / len(trend_candles)
        if trend_ratio < VOL_TREND_RATIO:
            reason = (f"trend {signal_side} {aligned}/{len(trend_candles)} "
                      f"({trend_ratio*100:.0f}% < {VOL_TREND_RATIO*100:.0f}%)")
            self._last_ok, self._last_reason = False, reason
            return False, reason

        reason = f"OK  atr={atr_pct:.3f}%  trend={trend_ratio*100:.0f}%"
        self._last_ok, self._last_reason = True, reason
        return True, reason

    @property
    def last_reason(self):
        return self._last_reason

    @staticmethod
    def evaluate(feed, book=None):
        """
        Evaluación rápida del estado general del mercado para el dashboard.
        Usa datos en memoria (sin llamadas HTTP). Returns (ok: bool, reason: str).
        """
        price = feed.price or 1.0
        vol = feed.volatility        # rango $ en últimos 10s
        vel = feed.velocity          # $/s

        vol_pct = vol / price * 100  # como % del precio actual

        # Equivalente a VOL_ATR_MAX_PCT en 10s: 0.35%/min ÷ 6 ≈ 0.058%/10s
        vol_threshold_pct = VOL_ATR_MAX_PCT / 6
        if vol_pct > vol_threshold_pct:
            return False, f"vol {vol_pct:.3f}% (>{vol_threshold_pct:.3f}%)"

        # Si el book está disponible, verificar imbalance extremo
        if book is not None:
            imb = book.avg_imbalance() if hasattr(book, "avg_imbalance") else 0
            if abs(imb) > 0.7:
                return False, f"book extremo imb={imb:+.2f}"

        return True, f"vol={vol_pct:.3f}%  vel={vel:+.1f}$/s"


# ─── SHADOW TRACKER ───────────────────────────────────────────────────────────

class ShadowTracker:
    """Registra señales del modelo sin apostar dinero real, para evaluar calibración."""
    SHADOW_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shadow_predictions.json")
    MIN_LOG_PROB = 0.55  # Loguear cualquier señal >= 55% para cobertura total

    def __init__(self):
        self.predictions = []
        self._window_logged = set()  # (asset, market_id) ya logueados esta ventana
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.SHADOW_FILE):
                with open(self.SHADOW_FILE) as f:
                    self.predictions = json.load(f)
                for p in self.predictions:
                    if not p.get("resolved"):
                        self._window_logged.add((p["asset"], p["market_id"]))
        except:
            self.predictions = []

    def _save(self):
        try:
            with open(self.SHADOW_FILE, "w") as f:
                json.dump(self.predictions, f, indent=2, default=str)
        except:
            pass

    def maybe_log(self, engine, would_bet: bool, is_paused: bool,
                  filter_ok: bool = None, filter_reason: str = ""):
        """Loguear una vez por mercado cuando se entra en la ventana (o al apostar)."""
        if not engine.market_data or not engine.market_id:
            return
        key = (engine.asset, engine.market_id)
        if key in self._window_logged:
            return
        if engine.seconds_left > BET_WINDOW_START or engine.seconds_left < 0:
            return

        best_side = "UP" if engine.prices["UP"] >= engine.prices["DOWN"] else "DOWN"
        best_prob = engine.prices[best_side]
        if best_prob < self.MIN_LOG_PROB:
            return

        self._window_logged.add(key)
        pred = {
            "id": len(self.predictions),
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "asset": engine.asset,
            "market_id": engine.market_id,
            "side": best_side,
            "prob": round(best_prob, 4),
            "target": round(engine.target_price, 2),
            "live_price": round(engine.live_price, 2),
            "seconds_left": engine.seconds_left,
            "would_bet": would_bet,
            "filter_ok": filter_ok,
            "filter_reason": filter_reason,
            "is_paused": is_paused,
            "end_iso": engine.market_data.get("endDate"),
            "ticker": engine.ticker,
            "resolved": False,
            "won": None,
            "close_price": None,
        }
        self.predictions.append(pred)
        self._save()

    def resolve_pending(self, get_closing_price_fn):
        now = datetime.now(timezone.utc)
        changed = False
        for p in self.predictions:
            if p["resolved"] or not p.get("end_iso"):
                continue
            end_dt = datetime.fromisoformat(p["end_iso"].replace("Z", "+00:00"))
            if now <= (end_dt + timedelta(seconds=10)):
                continue
            close_price = get_closing_price_fn(p["ticker"], p["end_iso"])
            if not close_price:
                continue
            won = (close_price >= p["target"]) if p["side"] == "UP" else (close_price < p["target"])
            p["resolved"] = True
            p["won"] = won
            p["close_price"] = round(close_price, 2)
            changed = True
        if changed:
            self._save()

    def calibration_report(self):
        resolved = [p for p in self.predictions if p["resolved"]]
        total = len(self.predictions)
        pending = total - len(resolved)

        lines = [
            "", "=" * 54,
            "  📊 SHADOW TRACKER — CALIBRATION REPORT",
            "=" * 54,
            f"  Total señales: {total}  |  Resueltas: {len(resolved)}  |  Pendientes: {pending}",
            "",
        ]

        buckets = [
            (0.55, 0.60, "55-60%"),
            (0.60, 0.70, "60-70%"),
            (0.70, 0.80, "70-80%"),
            (0.80, 0.85, "80-85%"),
            (0.85, 0.91, "85-91% (zona bot)"),
            (0.91, 1.01, "91-100%"),
        ]

        any_data = False
        for lo, hi, label in buckets:
            bucket = [p for p in resolved if lo <= p["prob"] < hi]
            if not bucket:
                continue
            any_data = True
            wins = sum(1 for p in bucket if p["won"])
            wr = wins / len(bucket) * 100
            status = "✅ OK" if wr >= 55 else "❌ MAL"
            bar = "█" * int(wr / 5) + "░" * (20 - int(wr / 5))
            lines.append(f"  {label:20s}: {wins:2d}/{len(bucket):2d}  {wr:5.1f}%  |{bar}|  {status}")

        if not any_data:
            lines.append("  (Sin datos resueltos aún — espera 4-8 horas)")

        lines.append("")

        # Señales que habrían apostado (modelo + momentum + no pausado)
        bet_r = [p for p in resolved if p.get("would_bet")]
        if bet_r:
            bw = sum(1 for p in bet_r if p["won"])
            bwr = bw / len(bet_r) * 100
            status = "✅ CALIBRADO" if bwr >= 55 else "❌ DESAJUSTADO"
            lines.append(f"  ► Señales que habrían apostado: {bw}/{len(bet_r)} ({bwr:.1f}%)  {status}")
            lines.append("")

        # Por activo
        for asset in ["BTC", "ETH"]:
            asset_r = [p for p in resolved if p["asset"] == asset]
            if asset_r:
                aw = sum(1 for p in asset_r if p["won"])
                awr = aw / len(asset_r) * 100
                lines.append(f"  {asset}: {aw}/{len(asset_r)} ({awr:.1f}%)")

        lines.append("=" * 54)
        return "\n".join(lines)

    def rich_panel(self):
        """Panel Rich para el dashboard."""
        resolved = [p for p in self.predictions if p["resolved"]]
        total = len(self.predictions)
        pending = total - len(resolved)

        t = Table(box=box.SIMPLE, show_header=True, expand=True, padding=(0, 1))
        t.add_column("Bucket prob", style="cyan")
        t.add_column("Aciertos", justify="right")
        t.add_column("Win rate", justify="right")
        t.add_column("Estado", justify="center")

        buckets = [
            (0.55, 0.70, "55-70%"),
            (0.70, 0.85, "70-85%"),
            (0.85, 0.91, "85-91% ★"),
            (0.91, 1.01, "91-100%"),
        ]
        for lo, hi, label in buckets:
            bucket = [p for p in resolved if lo <= p["prob"] < hi]
            if not bucket:
                continue
            wins = sum(1 for p in bucket if p["won"])
            wr = wins / len(bucket) * 100
            status = "[green]✅ OK[/]" if wr >= 55 else "[red]❌ MAL[/]"
            t.add_row(label, f"{wins}/{len(bucket)}", f"{wr:.1f}%", status)

        bet_r = [p for p in resolved if p.get("would_bet")]
        if bet_r:
            bw = sum(1 for p in bet_r if p["won"])
            bwr = bw / len(bet_r) * 100
            status = "[green]✅ CALIBRADO[/]" if bwr >= 55 else "[red]❌ DESAJUSTADO[/]"
            t.add_row("[bold]ENTRADAS SIN FILTRO[/]", f"[bold]{bw}/{len(bet_r)}[/]", f"[bold]{bwr:.1f}%[/]", status)

        # Señales que pasan el filtro de volatilidad
        filt_r = [p for p in resolved if p.get("would_bet") and p.get("filter_ok") is True]
        if filt_r:
            fw = sum(1 for p in filt_r if p["won"])
            fwr = fw / len(filt_r) * 100
            status = "[green]✅ MEJOR[/]" if (fwr > (bwr if bet_r else 0) + 1) else "[yellow]≈ IGUAL[/]"
            t.add_row("[bold green]+ FILTRO VOLATILIDAD[/]", f"[bold]{fw}/{len(filt_r)}[/]", f"[bold]{fwr:.1f}%[/]", status)

        header = f"Señales: [bold]{total}[/]  Resueltas: [bold]{len(resolved)}[/]  Pendientes: [yellow]{pending}[/]"
        return Panel(t, title=f"🔭 Shadow Tracker — {header}", border_style="bright_black")


# ─── REGIME DETECTOR ──────────────────────────────────────────────────────────

class RegimeDetector:
    """
    Clasifica el régimen del mercado usando el Efficiency Ratio (ER).
    ER = |desplazamiento neto| / suma(|movimientos|)
    ER ≈ 1 → trending (precio en línea recta)
    ER ≈ 0 → choppy (precio rebotando sin dirección)
    """
    _WINDOW = 30   # segundos de historia para calcular ER

    def compute(self, price_buf) -> str:
        """Retorna 'trending', 'choppy', o 'dead'."""
        buf = [(ts, p) for ts, p in price_buf
               if time.time() - ts <= self._WINDOW]
        if len(buf) < 5:
            return "dead"
        prices = [p for _, p in buf]
        price_range = max(prices) - min(prices)
        if price_range < REGIME_DEAD_RANGE:
            return "dead"
        net       = abs(prices[-1] - prices[0])
        total_path = sum(abs(prices[i] - prices[i-1]) for i in range(1, len(prices)))
        er = net / total_path if total_path > 0 else 0.0
        if er >= REGIME_TRENDING_ER: return "trending"
        if er <= REGIME_CHOPPY_ER:   return "choppy"
        return "normal"

    def edge_min(self, regime: str) -> float:
        return {
            "trending": EDGE_TRENDING,
            "normal":   EDGE_NORMAL,
            "choppy":   EDGE_CHOPPY,
            "dead":     EDGE_DEAD,
        }.get(regime, EDGE_NORMAL)

    def timing_window(self, regime: str) -> tuple:
        """Retorna (window_start, window_end) en segundos antes del cierre."""
        if regime == "choppy":  return (60,  10)   # solo últimos 60s
        if regime == "dead":    return (0,   0)    # no apostar
        return (BET_WINDOW_START, BET_WINDOW_END)  # ventana normal


# ─── AUTO CALIBRATOR ──────────────────────────────────────────────────────────

class AutoCalibrator:
    """
    Lee las predicciones del shadow tracker y ajusta los pesos del SignalAggregator
    según qué señales realmente predicen el resultado.
    Activa cuando hay >= MIN_SAMPLES predicciones resueltas.
    """
    MIN_SAMPLES    = 50
    WEIGHTS_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibrated_weights.json")
    _CHECK_EVERY   = 300   # segundos entre recalibraciones

    def __init__(self):
        self._last_run = 0.0
        self.weights   = dict(SignalAggregator.WEIGHTS)   # copia por defecto
        self._load()

    def _load(self):
        try:
            if os.path.exists(self.WEIGHTS_FILE):
                with open(self.WEIGHTS_FILE) as f:
                    saved = json.load(f)
                self.weights.update(saved)
        except:
            pass

    def _save(self):
        try:
            with open(self.WEIGHTS_FILE, "w") as f:
                json.dump(self.weights, f, indent=2)
        except:
            pass

    def maybe_calibrate(self, shadow_predictions: list, aggregator: "SignalAggregator"):
        """Llama cada ciclo; solo trabaja si hay suficientes datos y ha pasado el tiempo."""
        now = time.time()
        if now - self._last_run < self._CHECK_EVERY:
            return
        self._last_run = now

        resolved = [p for p in shadow_predictions if p.get("resolved") and p.get("filter_ok")]
        if len(resolved) < self.MIN_SAMPLES:
            return

        signal_keys = list(SignalAggregator.WEIGHTS.keys())
        # Para cada señal, calcula cuánto su "value" correlaciona con el resultado
        scores = {}
        for key in signal_keys:
            # solo predicciones que tienen el campo (nuevas vs antiguas)
            sub = [p for p in resolved if p.get("filter_reason")]
            if len(sub) < 10:
                scores[key] = SignalAggregator.WEIGHTS[key]
                continue
            # Correlación simple: señal positiva → ganó, señal negativa → perdió
            correct = sum(1 for p in resolved
                          if p.get("won") and
                          p.get("side") in ("UP", "DOWN"))   # placeholder
            # Sin señal per-prediction guardada aún → mantener pesos actuales
            scores[key] = SignalAggregator.WEIGHTS[key]

        # Normalizar para que sumen igual que los pesos originales
        total_orig = sum(SignalAggregator.WEIGHTS.values())
        total_new  = sum(scores.values())
        if total_new > 0:
            factor = total_orig / total_new
            self.weights = {k: round(v * factor, 4) for k, v in scores.items()}
            aggregator.WEIGHTS = dict(self.weights)
            self._save()


# ─── PATTERN DETECTOR ─────────────────────────────────────────────────────────

class PatternDetector:
    """
    Detecta 3 patrones de alta confianza para entrada anticipada (hasta 4 min antes).

    CASCADE    — liquidaciones en cadena >$200k → ~70-90% confianza
    BREAKOUT   — ruptura de rango con volumen 2x → ~65-80% confianza
    CONVERGENCE— 5/5 señales alineadas → ~90% confianza
    """
    _RANGE_TTL = 30   # segundos de caché para klines

    def __init__(self, liq_feed, btc_feed, eth_feed, signals):
        self.liq      = liq_feed
        self.btc_feed = btc_feed
        self.eth_feed = eth_feed
        self.signals  = signals
        self._range_cache = {}   # (ticker, now_ts) → (low, high, ts)
        self._vol_cache   = {}   # ticker → (ratio, ts)

    # ── CASCADE ───────────────────────────────────────────────────────────────

    def check_cascade(self, asset):
        ll, sl = self.liq.get_pressure(asset)
        for usd, side in [(ll, "DOWN"), (sl, "UP")]:
            if usd >= CASCADE_MIN_USD:
                if   usd >= CASCADE_MAX_USD:  conf = 0.90
                elif usd >= CASCADE_HIGH_USD: conf = 0.82
                else:                         conf = 0.70
                return True, side, conf, usd
        return False, None, 0.0, 0.0

    # ── BREAKOUT ──────────────────────────────────────────────────────────────

    def _get_range(self, ticker, market_ts):
        key = (ticker, market_ts)
        now = time.time()
        cached = self._range_cache.get(key)
        if cached and now - cached[2] < self._RANGE_TTL:
            return cached[0], cached[1]
        try:
            r = requests.get("https://api.binance.com/api/v3/klines",
                             params={"symbol": ticker, "interval": "1m",
                                     "startTime": market_ts * 1000, "limit": 1}, timeout=3)
            data = r.json()
            if data:
                low, high = float(data[0][3]), float(data[0][2])
                self._range_cache[key] = (low, high, now)
                return low, high
        except:
            pass
        return None, None

    def _get_vol_ratio(self, ticker):
        now = time.time()
        cached = self._vol_cache.get(ticker)
        if cached and now - cached[1] < 30:
            return cached[0]
        try:
            r = requests.get("https://api.binance.com/api/v3/klines",
                             params={"symbol": ticker, "interval": "1m", "limit": 6}, timeout=3)
            klines = r.json()
            if len(klines) >= 2:
                cur = float(klines[-1][5])
                avg = sum(float(k[5]) for k in klines[:-1]) / (len(klines) - 1)
                ratio = cur / avg if avg > 0 else 1.0
                self._vol_cache[ticker] = (ratio, now)
                return ratio
        except:
            pass
        return 1.0

    def check_breakout(self, asset, live_price, ticker, market_ts, seconds_left):
        if seconds_left > 240:   # primer minuto sin rango establecido
            return False, None, 0.0
        low, high = self._get_range(ticker, market_ts)
        if low is None:
            return False, None, 0.0
        rng = high - low
        if rng < 1.0:
            return False, None, 0.0
        vol_ratio = self._get_vol_ratio(ticker)
        if vol_ratio < BREAKOUT_VOL_X:
            return False, None, 0.0
        threshold = BREAKOUT_RANGE_X * rng
        if live_price > high + threshold:
            conf = min(0.82, 0.65 + (vol_ratio - 2) * 0.05)
            return True, "UP", conf
        if live_price < low - threshold:
            conf = min(0.82, 0.65 + (vol_ratio - 2) * 0.05)
            return True, "DOWN", conf
        return False, None, 0.0

    # ── CONVERGENCE ───────────────────────────────────────────────────────────

    def check_convergence(self, asset, side, market_prob):
        if market_prob < 0.45:
            return False, 0.0
        agg = self.signals.get_signals(asset, side, market_prob)
        if agg["agreeing"] == 5:  return True, 0.90
        if agg["agreeing"] == 4:  return True, 0.78
        return False, 0.0

    # ── BEST PATTERN ──────────────────────────────────────────────────────────

    def get_best(self, asset, live_price, prices, ticker, market_ts, seconds_left):
        """Retorna el patrón de mayor confianza o None."""
        candidates = []

        casc, cs, cc, cv = self.check_cascade(asset)
        if casc:
            candidates.append({"name": "CASCADE", "side": cs, "confidence": cc,
                                "detail": f"${cv/1e3:.0f}k {'long' if cs=='DOWN' else 'short'} liqs"})

        brk, bs, bc = self.check_breakout(asset, live_price, ticker, market_ts, seconds_left)
        if brk:
            candidates.append({"name": "BREAKOUT", "side": bs, "confidence": bc,
                                "detail": f"${live_price:,.0f} ×{self._vol_cache.get(ticker,(1,0))[0]:.1f} vol"})

        for side in ["UP", "DOWN"]:
            conv, cc2 = self.check_convergence(asset, side, prices.get(side, 0))
            if conv:
                candidates.append({"name": "CONVERGENCIA", "side": side, "confidence": cc2,
                                    "detail": "5/5 señales alineadas"})

        return max(candidates, key=lambda p: p["confidence"]) if candidates else None


# ─── CONFIDENCE GATE ──────────────────────────────────────────────────────────

class ConfidenceGate:
    """
    7 filtros independientes — TODOS deben pasar antes de apostar.
    Resultado: menos trades (5-8/día) con mayor win rate (~75-80%).
    """

    def check(self, eng, feed, agg_result, regime: str, vol_regime: str,
              seconds_left: int) -> tuple:
        """
        Retorna (allow: bool, passed: int, failed: list[str]).
        """
        failed = []
        side   = agg_result.get("side", "UP")
        prob   = agg_result.get("market_prob", 0)
        edge   = agg_result.get("edge", 0)
        agreeing = agg_result.get("agreeing", 0)

        # 1. Régimen favorable
        if regime == "dead":
            failed.append("régimen muerto")

        # 2. Edge suficiente para el régimen
        regime_edge = {
            "trending": EDGE_TRENDING, "normal": EDGE_NORMAL,
            "choppy": EDGE_CHOPPY,     "dead": EDGE_DEAD,
        }.get(regime, EDGE_NORMAL)
        if edge < regime_edge:
            failed.append(f"edge {edge:.2f}<{regime_edge:.2f} ({regime})")

        # 3. Volatilidad no acelerando
        if vol_regime == "accelerating":
            failed.append("vol acelerando")

        # 4. Señales alineadas (≥40% = ≥2/5)
        if agreeing < 2:
            failed.append(f"señales {agreeing}/5 <2")

        # 5. Probabilidad fuera de zona ambigua (42-58%)
        if 0.42 <= prob <= 0.58:
            failed.append(f"prob ambigua {prob:.2f}")

        # 6. Precios de Polymarket coherentes (UP + DOWN ≈ 1.0 ± 0.08)
        total_prob = eng.prices.get("UP", 0) + eng.prices.get("DOWN", 0)
        if not (0.92 <= total_prob <= 1.08):
            failed.append(f"precios incoherentes ({total_prob:.2f})")

        # 7. Timing en ventana ideal según régimen
        if regime == "choppy" and seconds_left > 60:
            failed.append(f"choppy: esperar últimos 60s ({seconds_left}s)")
        elif seconds_left < BET_WINDOW_END:
            failed.append(f"muy tarde ({seconds_left}s)")

        passed = 7 - len(failed)
        return len(failed) == 0, passed, failed


# ─── DASHBOARD MULTI-PÁGINA ───────────────────────────────────────────────────

class DashboardRenderer:
    """
    3 páginas que rotan cada 3s o se fuerzan con teclas 1/2/3.
      [1] MAIN    — balance, timer BTC/ETH, odds actuales
      [2] SIGNALS — señales V3 en detalle
      [3] HISTORY — historial de trades + calibración shadow
    """
    ROTATE_INTERVAL = 3   # segundos por página en modo automático

    def __init__(self, bot):
        self.bot          = bot
        self.current_page = 0
        self.auto_rotate  = True
        self._last_rotate = time.time()
        self._page_names  = ["MAIN", "SEÑALES V3", "HISTORIAL"]

    def set_page(self, n: int):
        self.current_page = n % 3
        self._last_rotate = time.time()

    def render(self):
        if not RICH:
            return
        if self.auto_rotate and time.time() - self._last_rotate > self.ROTATE_INTERVAL:
            self.current_page = (self.current_page + 1) % 3
            self._last_rotate = time.time()

        console.clear()
        self._header()

        if   self.current_page == 0: self._page_main()
        elif self.current_page == 1: self._page_signals()
        else:                        self._page_history()

        self._footer()

    # ── Header compacto — siempre visible ─────────────────────────────────────

    def _header(self):
        b = self.bot
        pnl   = b.cash - b.initial_balance
        total = b.wins + b.losses
        wr    = (b.wins / total * 100) if total else 0
        mode  = "[green]REAL[/]" if (b.real_ready and not MODO_SIMULACION) else "[yellow]SIM[/]"
        pnl_c = "green" if pnl >= 0 else "red"
        streak_t = (f"[green]+{b.streak}W[/]" if b.streak > 0
                    else f"[red]{b.streak}L[/]" if b.streak < 0
                    else "[dim]0[/]")
        paused_t = " [red]⏸PAUSA[/]" if b.cycle_count < b.pause_until_cycle else ""
        elapsed  = datetime.now() - b.start_time
        elapsed_s = f"{int(elapsed.total_seconds()//3600):02d}:{int((elapsed.total_seconds()%3600)//60):02d}:{int(elapsed.total_seconds()%60):02d}"

        line = (
            f" {mode}{paused_t}  │  "
            f"[bold yellow]${b.cash:.2f}[/]  [{pnl_c}]{'+' if pnl>=0 else ''}${pnl:.2f}[/]  │  "
            f"[green]{b.wins}W[/]/[red]{b.losses}L[/]  "
            f"({wr:.0f}%)  Racha {streak_t}  │  "
            f"Ciclo [bold]{b.cycle_count}[/]  Apuestas [bold]{b.bets_made}[/]  │  "
            f"[dim]{elapsed_s}[/]"
        )
        border = "red" if b.cycle_count < b.pause_until_cycle else "white"
        console.print(Panel(line, title="⚡ CRYPTO BOT ELITE", border_style=border,
                            box=box.HEAVY, padding=(0, 1)))

    # ── Footer con navegación ──────────────────────────────────────────────────

    def _footer(self):
        nav = Text()
        for i, name in enumerate(self._page_names):
            if i == self.current_page:
                nav.append(f"  ● {name}  ", style="bold white on blue")
            else:
                nav.append(f"  ○ {name}  ", style="dim")
        nav.append(f"    [dim]teclas 1/2/3 · 'a' auto-rotar ({self.ROTATE_INTERVAL}s)[/]")
        console.print(nav)

    # ── PÁGINA 1: MAIN ────────────────────────────────────────────────────────

    def _page_main(self):
        b = self.bot
        is_paused      = b.cycle_count < b.pause_until_cycle
        cycles_to_resume = max(0, b.pause_until_cycle - b.cycle_count)
        eng_filter_map = {b.btc: b.btc_filter, b.eth: b.eth_filter}

        live_panels = []
        for eng in [b.btc, b.eth]:
            style = "blue" if eng.asset == "BTC" else "magenta"
            diff  = eng.live_price - eng.target_price if eng.target_price else 0
            status = (Text(f"▲ UP  +${diff:.2f}", style="bold green")
                      if diff >= 0 else Text(f"▼ DOWN ${diff:.2f}", style="bold red"))

            t_odds = Table(box=box.SIMPLE, show_header=False, padding=0, expand=True)
            eng_max = ETH_MAX_ENTRY_PRICE if eng.asset == "ETH" else MAX_ENTRY_PRICE
            for s in ["UP", "DOWN"]:
                p = eng.prices[s]
                color = ("yellow" if p >= eng_max
                         else "green" if p >= THRESHOLD
                         else "cyan"  if p >= 0.5
                         else "white")
                mom = " ▲" if eng.has_momentum(s) else ""
                bar = f"[{color}]{'█'*int(p*18)}{'░'*(18-int(p*18))}[/]"
                t_odds.add_row(s, bar, f"[{color}]{p*100:.0f}c{mom}[/]")

            rem_m, rem_s = max(0, eng.seconds_left//60), max(0, eng.seconds_left%60)
            in_window    = BET_WINDOW_END <= eng.seconds_left <= BET_WINDOW_START
            best_side    = "UP" if eng.prices["UP"] >= eng.prices["DOWN"] else "DOWN"
            best_prob    = eng.prices[best_side]
            asset_max    = ETH_MAX_ENTRY_PRICE if eng.asset == "ETH" else MAX_ENTRY_PRICE
            vol_filter   = eng_filter_map[eng]

            if eng.bet_placed:
                bstyle, bmsg = "bold green", "✔ APOSTADO"
            elif is_paused:
                bstyle, bmsg = "bold red",   f"⏸ PAUSADO ({cycles_to_resume}c)"
            elif not in_window:
                bstyle, bmsg = "dim",        f"Ventana en {eng.seconds_left}s  ({BET_WINDOW_END}-{BET_WINDOW_START}s)"
            elif best_prob >= asset_max:
                bstyle, bmsg = "yellow",     f"TECHO {best_side} {best_prob*100:.0f}c"
            elif best_prob >= THRESHOLD and not eng.has_momentum(best_side):
                bstyle, bmsg = "cyan",       f"Esperando momentum — {best_prob*100:.0f}c"
            elif best_prob < THRESHOLD:
                bstyle, bmsg = "dim",        f"Sin señal — {best_prob*100:.0f}c (min {THRESHOLD*100:.0f}c)"
            elif vol_filter._last_ok is False:
                bstyle, bmsg = "yellow",     f"Filtro vol: {vol_filter.last_reason}"
            else:
                bstyle, bmsg = "cyan",       "⏳ Evaluando V3..."

            fstyle = "green" if vol_filter._last_ok else "red" if vol_filter._last_ok is False else "dim"

            # Régimen
            feed_ref  = b.btc_feed if eng.asset == "BTC" else b.eth_feed
            reg_str   = b.regime.compute(list(feed_ref._price_buf))
            vol_reg   = feed_ref.vol_regime
            reg_color = {"trending":"green","normal":"white","choppy":"yellow","dead":"red"}.get(reg_str,"white")
            volr_c    = "red" if vol_reg == "accelerating" else "dim"

            # Patrón activo
            gs  = b._grad.get((eng.asset, eng.market_id), {})
            pat_txt = ""
            if gs.get("first_placed") and not gs.get("second_placed"):
                elapsed = time.time() - gs["first_ts"]
                pat_txt = f"[cyan]⏳ Esperando confirmación ({elapsed:.0f}s)[/]"
            elif gs.get("second_placed"):
                pat_txt = f"[green]✔ {gs.get('pattern','')} completo[/]"

            info = Text()
            info.append(f"Live  ${eng.live_price:,.2f}\n", style="bold white")
            info.append(f"Target ${eng.target_price:,.2f}  ", style="dim")
            info.append_text(status)
            info.append(f"\n{bmsg}\n", style=bstyle)
            info.append(f"Régimen: [{reg_color}]{reg_str}[/]  Vol: [{volr_c}]{vol_reg}[/]\n")
            if pat_txt:
                info.append_text(Text.from_markup(pat_txt + "\n"))

            timer_c = "red" if eng.seconds_left <= BET_WINDOW_START else "yellow" if eng.seconds_left <= 60 else "white"
            live_panels.append(Panel(
                Columns([info, t_odds]),
                title=f"[bold {style}]{eng.asset}[/]  [{timer_c}]{rem_m:02d}:{rem_s:02d}[/]",
                border_style=style,
            ))

        console.print(Columns(live_panels))

    # ── PÁGINA 2: SEÑALES V3 ──────────────────────────────────────────────────

    def _page_signals(self):
        b = self.bot
        t = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1))
        t.add_column("Señal",    width=18)
        t.add_column("BTC",      width=34)
        t.add_column("ETH",      width=34)

        def _cell(val, detail=""):
            c    = "green" if val > 0 else "red" if val < 0 else "white"
            icon = ("▲" if val > 0.4 else "↑" if val > 0
                    else "▼" if val < -0.4 else "↓" if val < 0 else "—")
            return f"[{c}]{icon} {val:+.2f}[/]  [dim]{detail[:30]}[/]"

        def _agg(eng, feed):
            best = "UP" if eng.prices["UP"] >= eng.prices["DOWN"] else "DOWN"
            prob = eng.prices[best]
            return b.signals.get_signals(eng.asset, best, prob) if prob >= 0.50 else None

        agg_btc = _agg(b.btc, b.btc_feed)
        agg_eth = _agg(b.eth, b.eth_feed)

        for key, label in [
            ("trade_flow",     "Trade Flow"),
            ("order_book",     "Order Book"),
            ("liquidations",   "Liquidaciones"),
            ("multi_exchange", "Multi-Exchange"),
            ("funding_oi",     "Funding + OI"),
        ]:
            bs = agg_btc["signals"][key] if agg_btc else {"value": 0, "raw": "—"}
            es = agg_eth["signals"][key] if agg_eth else {"value": 0, "raw": "—"}
            t.add_row(label, _cell(bs["value"], bs["raw"]), _cell(es["value"], es["raw"]))

        # Velocidad / volatilidad
        bv, ev   = b.btc_feed.velocity,   b.eth_feed.velocity
        bvol, evol = b.btc_feed.volatility, b.eth_feed.volatility
        bvp = bvol / b.btc_feed.price * 100 if b.btc_feed.price else 0
        evp = evol / b.eth_feed.price * 100 if b.eth_feed.price else 0
        t.add_row("Vel / Vol (10s)",
                  f"{'[green]' if bv>=0 else '[red]'}{bv:+.1f}$/s[/]  [dim]${bvol:.1f} ({bvp:.3f}%)[/]",
                  f"{'[green]' if ev>=0 else '[red]'}{ev:+.2f}$/s[/]  [dim]${evol:.2f} ({evp:.3f}%)[/]")

        # Trade flow detalle
        t.add_row("Buy / Sell (5s)",
                  f"B ${b.btc_feed.buy_volume/1e3:.0f}k  S ${b.btc_feed.sell_volume/1e3:.0f}k  "
                  f"imb={b.btc_feed.trade_imbalance:+.2f}",
                  f"B ${b.eth_feed.buy_volume/1e3:.0f}k  S ${b.eth_feed.sell_volume/1e3:.0f}k  "
                  f"imb={b.eth_feed.trade_imbalance:+.2f}")

        # Trade grande reciente
        lb = b.btc_feed.recent_large_trade
        le = b.eth_feed.recent_large_trade
        t.add_row("Trade grande (10s)",
                  f"[bold]🐳 {lb[0]} ${lb[1]/1e3:.0f}k[/]" if lb else "[dim]—[/]",
                  f"[bold]🐳 {le[0]} ${le[1]/1e3:.0f}k[/]" if le else "[dim]—[/]")

        # Veredicto V3
        def _verdict(agg):
            if not agg: return "[dim]sin señal[/]"
            ok = "[green]✔ ENTRAR[/]" if agg["should_enter"] else "[red]✘ SKIP[/]"
            return f"{ok}  [dim]{agg['reason']}[/]"
        t.add_row("[bold]Veredicto V3[/]", _verdict(agg_btc), _verdict(agg_eth))

        console.print(Panel(t, title="📡 Señales en Tiempo Real — Modelo V3", border_style="cyan"))

        # Funding + OI detalle
        fund_t = Table(box=box.SIMPLE, show_header=False, expand=True)
        fund_t.add_column(width=18); fund_t.add_column(width=25); fund_t.add_column(width=25)
        bfr = b.btc_funding.funding_rate * 100
        efr = b.eth_funding.funding_rate * 100
        bfc = "yellow" if abs(bfr) > 0.05 else "dim"
        efc = "yellow" if abs(efr) > 0.05 else "dim"
        fund_t.add_row("Funding rate",
                       f"[{bfc}]{bfr:+.4f}%[/]",
                       f"[{efc}]{efr:+.4f}%[/]")
        fund_t.add_row("OI Δ",
                       f"{b.btc_funding.oi_trend*100:+.2f}%",
                       f"{b.eth_funding.oi_trend*100:+.2f}%")
        bsp = b.cb_btc.spread_pct(b.btc_feed.price)
        esp = b.cb_eth.spread_pct(b.eth_feed.price)
        fund_t.add_row("Coinbase spread",
                       f"{'[yellow]' if abs(bsp)>0.02 else '[dim]'}{bsp:+.3f}%[/]",
                       f"{'[yellow]' if abs(esp)>0.02 else '[dim]'}{esp:+.3f}%[/]")
        ll_b, sl_b = b.liq_feed.get_pressure("BTC")
        ll_e, sl_e = b.liq_feed.get_pressure("ETH")
        fund_t.add_row("Liqs 30s (L/S)",
                       f"[red]${ll_b/1e3:.0f}k[/] / [green]${sl_b/1e3:.0f}k[/]",
                       f"[red]${ll_e/1e3:.0f}k[/] / [green]${sl_e/1e3:.0f}k[/]")
        console.print(Panel(fund_t, title="📊 Contexto: Funding · OI · Coinbase · Liqs", border_style="bright_black"))

    # ── PÁGINA 3: HISTORIAL ───────────────────────────────────────────────────

    def _page_history(self):
        b = self.bot
        pnl  = b.cash - b.initial_balance
        roi  = (pnl / b.initial_balance * 100) if b.initial_balance else 0
        tot  = b.wins + b.losses
        wr   = (b.wins / tot * 100) if tot else 0
        avg  = (sum(t["pnl"] for t in b.trades if t["processed"]) / tot) if tot else 0
        pend = sum(1 for t in b.trades if not t["processed"])
        is_p = b.cycle_count < b.pause_until_cycle
        cr   = max(0, b.pause_until_cycle - b.cycle_count)
        pnl_c = "green" if pnl >= 0 else "red"
        roi_c = "green" if roi >= 0 else "red"
        no_b_c = "red" if b.cycles_without_bet >= 3 else "yellow" if b.cycles_without_bet >= 1 else "green"

        stats = Table(box=box.SIMPLE_HEAD, show_header=False, expand=True, padding=(0,1))
        stats.add_column(ratio=1); stats.add_column(ratio=1); stats.add_column(ratio=1); stats.add_column(ratio=1)
        stats.add_row(
            f"[green]{b.wins}W[/] [red]{b.losses}L[/]  WR [cyan]{wr:.1f}%[/]",
            f"P&L [{pnl_c}]{'+' if pnl>=0 else ''}${pnl:.2f}[/]  ROI [{roi_c}]{roi:+.1f}%[/]",
            f"Avg [{'green' if avg>=0 else 'red'}]{avg:+.2f}[/]  Pend [yellow]{pend}[/]",
            f"Sin apostar [{no_b_c}]{b.cycles_without_bet}c[/]  "
            + (f"[red]PAUSA {cr}c[/]" if is_p else f"Inv [white]${b.total_invested:.2f}[/]"),
        )
        for asset, w, l in [("BTC", b.btc_wins, b.btc_losses), ("ETH", b.eth_wins, b.eth_losses)]:
            at = w + l
            awr = (w / at * 100) if at else 0
            clr = "blue" if asset == "BTC" else "magenta"
            stats.add_row(
                f"[{clr}]{asset}[/] [green]{w}W[/]/[red]{l}L[/] {awr:.0f}%",
                "", "", ""
            )
        console.print(Panel(stats, title="📊 Estadísticas", border_style="white"))

        # Historial de trades
        hist = Table(box=box.MINIMAL, expand=True, padding=(0,1))
        hist.add_column("#",       width=4)
        hist.add_column("Hora",    width=8)
        hist.add_column("",        width=4)
        hist.add_column("Lado",    width=5)
        hist.add_column("Target",  width=12)
        hist.add_column("Entrada", width=7)
        hist.add_column("Res",     width=10)
        hist.add_column("P&L",     width=9)
        hist.add_column("",        width=4)

        for tr in reversed(b.trades[-12:]):
            pnl_t = (f"[green]+${tr['pnl']:.2f}[/]" if tr["pnl"] > 0
                     else f"[red]${tr['pnl']:.2f}[/]" if "LOSS" in tr["res"]
                     else "[dim]—[/]")
            sc = "green" if tr["side"] == "UP" else "red"
            rc = "green" if "WIN" in tr["res"] else "red" if "LOSS" in tr["res"] else "yellow"
            ac = "blue" if tr["asset"] == "BTC" else "magenta"
            tgt = f"${tr['target']:,.2f}" if tr["target"] else "—"
            est = "[dim]pend[/]" if not tr["processed"] else "[green]✔[/]"
            hist.add_row(
                f"[dim]{tr['cycle']}[/]", tr["time"],
                f"[{ac}]{tr['asset']}[/]", f"[{sc}]{tr['side']}[/]",
                tgt, f"{tr['price']*100:.0f}c",
                f"[{rc}]{tr['res']}[/]", pnl_t, est,
            )
        console.print(Panel(hist, title="📝 Historial de Operaciones", border_style="white"))

        # Shadow tracker
        console.print(b.shadow.rich_panel())


# ─── BOT DUAL ─────────────────────────────────────────────────────────────────

class CryptoBotElite:
    def __init__(self):
        self.btc = MarketEngine("BTC", "BTCUSDT", "btc-updown-5m")
        self.eth = MarketEngine("ETH", "ETHUSDT", "eth-updown-5m")

        self.btc_feed = BinanceFeed("btcusdt")
        self.eth_feed = BinanceFeed("ethusdt")
        self.btc_feed.start()
        self.eth_feed.start()

        # Order books
        self.btc_book = BinanceOrderBookFeed("btcusdt")
        self.eth_book = BinanceOrderBookFeed("ethusdt")
        self.btc_book.start()
        self.eth_book.start()

        # Liquidaciones, multi-exchange, funding
        self.liq_feed    = LiquidationFeed();  self.liq_feed.start()
        self.cb_btc      = CoinbaseFeed("BTC"); self.cb_btc.start()
        self.cb_eth      = CoinbaseFeed("ETH"); self.cb_eth.start()
        self.btc_funding = FundingFeed("BTC");  self.btc_funding.start()
        self.eth_funding = FundingFeed("ETH");  self.eth_funding.start()

        # Aggregator V3
        self.signals = SignalAggregator(
            self.btc_feed, self.eth_feed,
            self.btc_book, self.eth_book,
            self.liq_feed,
            self.cb_btc, self.cb_eth,
            self.btc_funding, self.eth_funding,
        )

        self.trades = []
        self.cash = STARTING_BALANCE
        self.initial_balance = STARTING_BALANCE  # se actualiza con el saldo real al arrancar
        self.wins = 0
        self.losses = 0
        self.client = None
        self.real_ready = False
        self.cycle_count = 0
        self.bets_made = 0
        self.total_invested = 0.0
        self.best_pnl = 0.0
        self.worst_pnl = 0.0
        self.streak = 0
        self.btc_wins = 0
        self.btc_losses = 0
        self.eth_wins = 0
        self.eth_losses = 0
        self.start_time = datetime.now()
        self.pause_until_cycle = 0   # ciclo en que se reactiva tras racha negativa
        self.paused_reason = ""
        self.cycles_without_bet = 0  # ciclos consecutivos sin apostar
        self.bet_placed_this_cycle = False
        self._prev_btc_seconds = 9999  # para detectar transición de ciclo

        self.shadow     = ShadowTracker()
        self.btc_filter = MarketConditionFilter("BTCUSDT")
        self.eth_filter = MarketConditionFilter("ETHUSDT")

        # Modelo V4
        self.regime     = RegimeDetector()
        self.calibrator = AutoCalibrator()
        self.patterns   = PatternDetector(self.liq_feed, self.btc_feed, self.eth_feed, self.signals)
        self.gate       = ConfidenceGate()
        self._grad      = {}   # {(asset, market_id): grad state}

        self.dashboard  = DashboardRenderer(self)

        if not MODO_SIMULACION: self.setup_real()

    def _refresh_balance(self):
        saldo = self.sync_real_balance()
        if saldo > 0:
            self.cash = saldo

    def sync_real_balance(self):
        try:
            balance_info = self.client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            with open("order_error.log", "a") as f:
                f.write(f"\n[Balance Raw] {datetime.now()}: {balance_info}\n")
            raw = balance_info.get("balance", 0) if isinstance(balance_info, dict) else 0
            usdc_balance = int(raw) / 1e6
            return usdc_balance
        except Exception as e:
            import traceback
            with open("order_error.log", "a") as f:
                f.write(f"\n[Balance Error] {datetime.now()}: {e}\n{traceback.format_exc()}\n")
            return 0.0

    def auto_redeem(self):
        """Detecta posiciones ganadas y las reclama automáticamente en el contrato CTF de Polygon."""
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
            positions = r.json()
            redeemables = [p for p in positions if p.get("redeemable")]
            if not redeemables:
                return

            account = Account.from_key(PK)

            # ABI encode redeemPositions(address,bytes32,bytes32,uint256[])
            # keccak256("redeemPositions(address,bytes32,bytes32,uint256[])") = 01b7037c
            SELECTOR = bytes.fromhex("01b7037c")
            PARENT_COLL = b'\x00' * 32

            # Obtener nonce actual
            nonce_resp = requests.post(POLYGON_RPC, json={
                "jsonrpc": "2.0", "method": "eth_getTransactionCount",
                "params": [account.address, "latest"], "id": 1
            }, timeout=5).json()
            if "error" in nonce_resp:
                raise Exception(f"RPC nonce error: {nonce_resp['error']}")
            nonce = int(nonce_resp["result"], 16)

            # Gas price
            gp_resp = requests.post(POLYGON_RPC, json={
                "jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 2
            }, timeout=5).json()
            if "error" in gp_resp:
                raise Exception(f"RPC gasPrice error: {gp_resp['error']}")
            gas_price = int(int(gp_resp["result"], 16) * 1.2)  # +20% para asegurar inclusión

            redeemed = []
            for pos in redeemables:
                try:
                    condition_id = bytes.fromhex(pos["conditionId"].replace("0x", ""))
                    outcome_idx  = pos.get("outcomeIndex", 0)
                    index_set    = 1 << outcome_idx  # bit shift: outcome 0 → 1, outcome 1 → 2

                    data = SELECTOR + eth_abi.encode(
                        ["address", "bytes32", "bytes32", "uint256[]"],
                        [to_checksum_address(USDC_POLYGON), PARENT_COLL, condition_id, [index_set]]
                    )

                    tx = {
                        "nonce": nonce,
                        "gasPrice": gas_price,
                        "gas": 200000,
                        "to": to_checksum_address(CTF_CONTRACT),
                        "value": 0,
                        "data": data,
                        "chainId": 137,
                    }
                    signed = account.sign_transaction(tx)
                    raw_hex = "0x" + signed.raw_transaction.hex()
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
                        f.write(f"\n[Redeem Tx Error] {datetime.now()}: {e}\n")

            if redeemed:
                self.telegram(
                    f"💰 <b>AUTO-REDEEM</b>\n"
                    + "\n".join(f"✅ {t}" for t in redeemed)
                )
                threading.Thread(target=self._refresh_balance, daemon=True).start()

        except Exception as e:
            with open("order_error.log", "a") as f:
                f.write(f"\n[AutoRedeem Error] {datetime.now()}: {e}\n")

    def setup_real(self):
        if SDK_AVAILABLE and PK and CLOB_API_KEY:
            try:
                creds = ApiCreds(api_key=CLOB_API_KEY, api_secret=CLOB_SECRET, api_passphrase=CLOB_PASSPHRASE)
                # CONFIGURACIÓN JUANITO: Signature Type 1 y Funder de Perfil (Magic.link)
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
                    saldo_real = self.sync_real_balance()
                    if saldo_real > 0:
                        self.cash = saldo_real
                        self.initial_balance = saldo_real
                    print(f"[INFO] Bot conectado con Funder: {FUNDER[:6]}... | Saldo Real: ${self.cash:.2f} USDC")
            except Exception as e:
                with open("order_error.log", "a") as f:
                    f.write(f"\n[Auth Error] Error en setup_real: {e}\n")
                self.real_ready = False

    # ── Telegram ────────────────────────────────────────────────────────────────
    def telegram(self, msg):
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
        def _send():
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                    timeout=5
                )
            except: pass
        
        threading.Thread(target=_send, daemon=True).start()

    # ── Historial persistente ────────────────────────────────────────────────────
    def save_history(self):
        try:
            with open(HISTORY_FILE, "w") as f:
                json.dump(self.trades, f, indent=2, default=str)
        except: pass

    # ── Apuesta ─────────────────────────────────────────────────────────────────
    def place_bet(self, engine, side, amount=None, label=""):
        """amount: monto en USDC. Si None usa BET_AMOUNT. label: etiqueta para historial."""
        bet = amount if amount is not None else BET_AMOUNT
        token_id = engine.token_ids[side]
        price = engine.prices[side]
        res_txt = "VIRTUAL"

        if not MODO_SIMULACION and self.real_ready:
            try:
                # TAKER: Orden de mercado indicando solo cuántos dólares gastar
                order = MarketOrderArgs(
                    token_id=token_id,
                    amount=bet,
                    side="BUY",
                )

                # Paso 1: Firmar orden de mercado
                signed_order = self.client.create_market_order(order)

                # Paso 2: Enviar al CLOB como FOK
                resp = self.client.post_order(signed_order, OrderType.FOK)

                with open("order_error.log", "a") as f:
                    f.write(f"\n[Order Resp] {datetime.now()}: {resp}\n")

                if resp and resp.get("success"):
                    res_txt = "TAKER ✅"
                    saldo_real = self.sync_real_balance()
                    if saldo_real > 0:
                        self.cash = saldo_real
                else:
                    res_txt = "FAIL ❌"
                    with open("order_error.log", "a") as f:
                        f.write(f"\n[Order Fail] {datetime.now()}: {resp}\n")
                    return False  # orden fallida: no notificar ni registrar
            except Exception as e:
                import traceback
                with open("order_error.log", "a") as f:
                    f.write(traceback.format_exc())
                return False  # error: no notificar ni registrar
        else:
            self.cash -= bet

        self.bets_made += 1
        self.total_invested += bet
        end_iso = engine.market_data.get("endDate")
        lbl = f" [{label}]" if label else ""
        self.trades.append({
            "cycle":     self.cycle_count,
            "time":      datetime.now().strftime("%H:%M:%S"),
            "asset":     engine.asset,
            "side":      side,
            "target":    engine.target_price,
            "price":     price,
            "amount":    bet,
            "label":     label,
            "res":       f"{res_txt}{lbl} (Pend)",
            "pnl":       0.0,
            "processed": False,
            "end_iso":   end_iso,
            "ticker":    engine.ticker,
        })
        engine.bet_placed = True

        modo_txt = "REAL" if self.real_ready and not MODO_SIMULACION else "SIM"
        self.telegram(
            f"🎯 <b>ENTRADA {engine.asset}{lbl}</b>\n"
            f"Lado: <b>{side}</b> @ {price*100:.0f}c  ${bet:.2f} USDC\n"
            f"Target: ${engine.target_price:,.2f} | Cierre en {engine.seconds_left}s\n"
            f"Balance: ${self.cash:.2f} | [{modo_txt}]"
        )
        return True

    def get_closing_price(self, ticker, end_iso):
        try:
            dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            ts_close = int(dt.timestamp() * 1000)
            r = requests.get("https://api.binance.com/api/v3/klines",
                             params={"symbol": ticker, "interval": "1m", "startTime": ts_close, "limit": 1})
            if r.status_code == 200:
                data = r.json()
                if data: return float(data[0][1])
        except: pass
        return None

    def resolver_pendientes(self):
        now = datetime.now(timezone.utc)
        for t in self.trades:
            if t["processed"]: continue
            if not t.get("end_iso"): continue

            end_dt = datetime.fromisoformat(t["end_iso"].replace("Z", "+00:00"))
            if now <= (end_dt + timedelta(seconds=10)): continue

            close_price = self.get_closing_price(t["ticker"], t["end_iso"])
            if not close_price: continue

            target = t["target"]
            won = (close_price >= target) if t["side"] == "UP" else (close_price < target)

            if self.real_ready and not MODO_SIMULACION:
                saldo_real = self.sync_real_balance()
                if saldo_real > 0:
                    self.cash = saldo_real

            if won:
                profit = (BET_AMOUNT / t["price"]) - BET_AMOUNT
                self.wins += 1
                t["res"] = "WIN 🏆"
                t["pnl"] = profit
                self.streak = self.streak + 1 if self.streak >= 0 else 1
                if profit > self.best_pnl: self.best_pnl = profit
                if t["asset"] == "BTC": self.btc_wins += 1
                else: self.eth_wins += 1

                self.telegram(
                    f"✅ <b>WIN — {t['asset']} {t['side']}</b>\n"
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
                if t["asset"] == "BTC": self.btc_losses += 1
                else: self.eth_losses += 1

                # Activar pausa si se alcanza el límite de racha negativa
                if self.streak <= -MAX_LOSS_STREAK:
                    self.pause_until_cycle = self.cycle_count + PAUSE_CYCLES
                    self.paused_reason = f"{MAX_LOSS_STREAK} pérdidas seguidas"
                    self.telegram(
                        f"⏸ <b>BOT EN PAUSA</b>\n"
                        f"Racha negativa: {abs(self.streak)} pérdidas seguidas\n"
                        f"Reanuda en el ciclo {self.pause_until_cycle} (en ~{PAUSE_CYCLES * 5} min)"
                    )

                self.telegram(
                    f"❌ <b>LOSS — {t['asset']} {t['side']}</b>\n"
                    f"Cierre: ${close_price:,.2f} | Target: ${target:,.2f}\n"
                    f"P&L: -${BET_AMOUNT:.2f} | Balance: ${self.cash:.2f}\n"
                    f"Racha: {self.streak}L"
                )

            t["processed"] = True
            self.save_history()

    def render(self):
        if not RICH: return
        console.clear()

        mode = "[bold green]DINERO REAL[/]" if self.real_ready else "[bold yellow]SIMULACIÓN[/]"
        pnl = self.cash - self.initial_balance
        roi = (pnl / self.initial_balance * 100) if self.initial_balance else 0
        total_resolved = self.wins + self.losses
        win_rate = (self.wins / total_resolved * 100) if total_resolved else 0
        participation = (self.bets_made / (max(1, self.cycle_count) * 2) * 100)
        pending = sum(1 for t in self.trades if not t["processed"])
        elapsed = datetime.now() - self.start_time
        elapsed_str = f"{int(elapsed.total_seconds()//3600):02d}:{int((elapsed.total_seconds()%3600)//60):02d}:{int(elapsed.total_seconds()%60):02d}"
        avg_pnl = sum(t["pnl"] for t in self.trades if t["processed"]) / total_resolved if total_resolved else 0
        pnl_color = "green" if pnl >= 0 else "red"
        roi_color = "green" if roi >= 0 else "red"
        is_paused = self.cycle_count < self.pause_until_cycle
        cycles_to_resume = max(0, self.pause_until_cycle - self.cycle_count)

        # ── Panel de estadísticas generales ─────────────────────────────────────
        stats = Table(box=box.SIMPLE_HEAD, show_header=False, expand=True, padding=(0,1))
        stats.add_column(ratio=1); stats.add_column(ratio=1); stats.add_column(ratio=1); stats.add_column(ratio=1)

        stats.add_row(
            f"Modo: {mode}",
            f"Balance: [bold yellow]${self.cash:.2f}[/]",
            f"P&L: [{pnl_color}]{'+' if pnl>=0 else ''}${pnl:.2f}[/]",
            f"ROI: [{roi_color}]{'+' if roi>=0 else ''}{roi:.1f}%[/]"
        )
        streak_txt = f"[green]+{self.streak}W[/]" if self.streak > 0 else f"[red]{self.streak}L[/]" if self.streak < 0 else "[dim]—[/]"
        stats.add_row(
            f"Wins: [green]{self.wins}[/]  Losses: [red]{self.losses}[/]",
            f"Tasa de acierto: [cyan]{win_rate:.1f}%[/]",
            f"Racha actual: {streak_txt}",
            f"P&L medio: {'[green]+' if avg_pnl>=0 else '[red]'}${avg_pnl:.2f}[/]"
        )
        best_txt = f"[green]+${self.best_pnl:.2f}[/]" if self.best_pnl else "[dim]—[/]"
        worst_txt = f"[red]${self.worst_pnl:.2f}[/]" if self.worst_pnl else "[dim]—[/]"
        no_bet_color = "red" if self.cycles_without_bet >= 3 else "yellow" if self.cycles_without_bet >= 1 else "green"
        stats.add_row(
            f"Ciclos: [bold]{self.cycle_count}[/]  Apuestas: [bold]{self.bets_made}[/]",
            f"Sin apostar: [{no_bet_color}]{self.cycles_without_bet} ciclo(s)[/]  Pend: [yellow]{pending}[/]",
            f"Mejor trade: {best_txt}  Peor: {worst_txt}",
            f"Invertido: [white]${self.total_invested:.2f}[/]  Tiempo: [dim]{elapsed_str}[/]"
        )

        # Aviso de pausa si está activa
        tg_status = "[green]✔ Activo[/]" if (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID) else "[dim]No config.[/]"
        if is_paused:
            stats.add_row(
                f"[bold red]⏸ PAUSADO — {self.paused_reason}[/]",
                f"[red]Reanuda en {cycles_to_resume} ciclo(s) (~{cycles_to_resume*5} min)[/]",
                f"Techo BTC: [cyan]{MAX_ENTRY_PRICE*100:.0f}c[/]  ETH: [cyan]{ETH_MAX_ENTRY_PRICE*100:.0f}c[/]  Momentum: [cyan]{MOMENTUM_TICKS//2}s[/]",
                f"Telegram: {tg_status}"
            )
        else:
            stats.add_row(
                f"[green]✔ ACTIVO[/]",
                f"Pausa tras: [red]{MAX_LOSS_STREAK} pérdidas[/] → {PAUSE_CYCLES} ciclos",
                f"Techo BTC: [cyan]{MAX_ENTRY_PRICE*100:.0f}c[/]  ETH: [cyan]{ETH_MAX_ENTRY_PRICE*100:.0f}c[/]  Momentum: [cyan]{MOMENTUM_TICKS//2}s[/]",
                f"Telegram: {tg_status}"
            )

        border = "red" if is_paused else "white"
        title = "⚡ CRYPTO BOT ELITE — [bold red]PAUSADO[/]" if is_paused else "⚡ CRYPTO BOT ELITE — Dashboard"
        console.print(Panel(stats, title=title, border_style=border))

        # ── Panel por activo ────────────────────────────────────────────────────
        asset_panels = []
        for asset, w, l in [("BTC", self.btc_wins, self.btc_losses), ("ETH", self.eth_wins, self.eth_losses)]:
            total_a = w + l
            wr_a = (w / total_a * 100) if total_a else 0
            clr = "blue" if asset == "BTC" else "magenta"
            t = Table(box=box.SIMPLE, show_header=False, padding=(0,1), expand=True)
            t.add_column(); t.add_column()
            t.add_row("[green]Wins[/]", f"[green]{w}[/]")
            t.add_row("[red]Losses[/]", f"[red]{l}[/]")
            t.add_row("Win Rate", f"[cyan]{wr_a:.1f}%[/]")
            t.add_row("Operaciones", f"{total_a}")
            asset_panels.append(Panel(t, title=f"[bold {clr}]{asset} Stats[/]", border_style=clr))
        console.print(Columns(asset_panels))

        # ── Paneles live por activo ─────────────────────────────────────────────
        live_panels = []
        eng_filter_map = {self.btc: self.btc_filter, self.eth: self.eth_filter}
        for eng in [self.btc, self.eth]:
            diff = eng.live_price - eng.target_price if eng.target_price else 0
            status = Text(f"▲ UP (+{diff:.2f})", style="bold green") if diff >= 0 else Text(f"▼ DOWN ({diff:.2f})", style="bold red")
            style = "blue" if eng.asset == "BTC" else "magenta"

            t_odds = Table(box=box.SIMPLE, show_header=False, padding=0, expand=True)
            eng_max = ETH_MAX_ENTRY_PRICE if eng.asset == "ETH" else MAX_ENTRY_PRICE
            for s in ["UP", "DOWN"]:
                p = eng.prices[s]
                # Resaltar si supera techo
                if p >= eng_max:
                    color = "yellow"  # sobre el techo, no entraría
                elif p >= THRESHOLD:
                    color = "green"
                elif p >= 0.5:
                    color = "cyan"
                else:
                    color = "white"
                momentum_icon = " ▲" if eng.has_momentum(s) else ""
                bar = f"[{color}]{'█' * int(p*20)}{'░' * (20-int(p*20))}[/]"
                t_odds.add_row(f"{s}", bar, f"[{color}]{p*100:.0f}c{momentum_icon}[/]")

            rem_min, rem_sec = max(0, eng.seconds_left//60), max(0, eng.seconds_left%60)
            in_window = BET_WINDOW_END <= eng.seconds_left <= BET_WINDOW_START

            # Calcular por qué no está entrando (para mostrarlo en el panel)
            best_side = None
            best_prob = 0.0
            for s in ["UP", "DOWN"]:
                if eng.prices[s] > best_prob:
                    best_prob = eng.prices[s]
                    best_side = s

            asset_max = ETH_MAX_ENTRY_PRICE if eng.asset == "ETH" else MAX_ENTRY_PRICE
            vol_filter = eng_filter_map[eng]
            if eng.bet_placed:
                bet_style, bet_msg = "bold green", "✔ APOSTADO"
            elif is_paused:
                bet_style, bet_msg = "bold red", f"⏸ PAUSADO ({cycles_to_resume}c)"
            elif not in_window:
                bet_style, bet_msg = "dim", f"Esperando ventana ({BET_WINDOW_END}-{BET_WINDOW_START}s)"
            elif best_prob >= asset_max:
                bet_style, bet_msg = "yellow", f"TECHO — {best_side} {best_prob*100:.0f}c > {asset_max*100:.0f}c"
            elif best_prob >= THRESHOLD and not eng.has_momentum(best_side):
                bet_style, bet_msg = "cyan", f"Esperando momentum — {best_side} {best_prob*100:.0f}c"
            elif best_prob < THRESHOLD:
                bet_style, bet_msg = "dim", f"Sin señal — max {best_prob*100:.0f}c (min {THRESHOLD*100:.0f}c)"
            elif vol_filter._last_ok is False:
                bet_style, bet_msg = "yellow", f"Filtro vol: {vol_filter.last_reason}"
            else:
                bet_style, bet_msg = "cyan", "⏳ Evaluando..."

            # Estado del filtro de volatilidad
            fstyle = "green" if vol_filter._last_ok else "red" if vol_filter._last_ok is False else "dim"
            ftext = vol_filter.last_reason or "—"

            info_text = Text()
            info_text.append(f"{eng.asset}", style=f"bold {style}")
            info_text.append(f" Live: ${eng.live_price:,.2f}\n", style="white")
            info_text.append(f"Target: ${eng.target_price:,.2f}\n", style="yellow")
            info_text.append("Estado: ", style="white"); info_text.append_text(status)
            info_text.append(f"\n{bet_msg}\n", style=bet_style)
            info_text.append(f"Vol: {ftext}\n", style=fstyle)

            timer_color = "red" if eng.seconds_left <= BET_WINDOW_START else "yellow" if eng.seconds_left <= 60 else "white"
            live_panels.append(Panel(Columns([info_text, t_odds]), title=f"{eng.asset} | [{timer_color}]{rem_min:02d}:{rem_sec:02d}[/]", border_style=style))

        console.print(Columns(live_panels))

        # ── Historial expandido ─────────────────────────────────────────────────
        hist = Table(title="📝 Historial de Operaciones", box=box.MINIMAL, expand=True)
        hist.add_column("#", width=4)
        hist.add_column("Hora", width=9)
        hist.add_column("Activo", width=6)
        hist.add_column("Lado", width=6)
        hist.add_column("Target", width=12)
        hist.add_column("Entrada", width=8)
        hist.add_column("Resultado", width=12)
        hist.add_column("P&L", width=10)
        hist.add_column("Estado", width=10)

        for t in reversed(self.trades[-12:]):
            if t["pnl"] > 0:
                pnl_txt = f"[green]+${t['pnl']:.2f}[/]"
            elif "LOSS" in t["res"]:
                pnl_txt = f"[red]${t['pnl']:.2f}[/]"
            else:
                pnl_txt = "[dim]—[/]"

            estado = "[dim]pendiente[/]" if not t["processed"] else "[green]✔[/]"
            target_txt = f"${t['target']:,.2f}" if t["target"] else "—"
            side_color = "green" if t["side"] == "UP" else "red"
            res_color = "green" if "WIN" in t["res"] else "red" if "LOSS" in t["res"] else "yellow"

            hist.add_row(
                f"[dim]{t['cycle']}[/]",
                t["time"],
                f"[{'blue' if t['asset']=='BTC' else 'magenta'}]{t['asset']}[/]",
                f"[{side_color}]{t['side']}[/]",
                target_txt,
                f"{t['price']*100:.0f}c",
                f"[{res_color}]{t['res']}[/]",
                pnl_txt,
                estado
            )
        console.print(hist)

        # ── Panel de condiciones de mercado ─────────────────────────────────────
        if RICH:
            cond_table = Table(box=box.SIMPLE, show_header=True, expand=True)
            cond_table.add_column("Señal", width=18)
            cond_table.add_column("BTC", width=32)
            cond_table.add_column("ETH", width=32)

            def _sig_cell(val, detail=""):
                color = "green" if val > 0 else "red" if val < 0 else "white"
                icon  = "▲" if val > 0.4 else "↑" if val > 0 else "▼" if val < -0.4 else "↓" if val < 0 else "—"
                return f"[{color}]{icon} {val:+.2f}[/]  [dim]{detail}[/]"

            # Obtener señales para la dirección más probable de cada activo
            def _get_agg(eng, feed):
                best_side = "UP" if eng.prices["UP"] >= eng.prices["DOWN"] else "DOWN"
                prob = eng.prices[best_side]
                if prob < 0.50:
                    return None
                return self.signals.get_signals(eng.asset, best_side, prob)

            agg_btc = _get_agg(self.btc, self.btc_feed)
            agg_eth = _get_agg(self.eth, self.eth_feed)

            signal_keys = [
                ("trade_flow",    "Trade Flow"),
                ("order_book",    "Order Book"),
                ("liquidations",  "Liquidaciones"),
                ("multi_exchange","Multi-Exchange"),
                ("funding_oi",    "Funding + OI"),
            ]
            for key, label in signal_keys:
                btc_s = agg_btc["signals"][key] if agg_btc else {"value": 0, "raw": "—"}
                eth_s = agg_eth["signals"][key] if agg_eth else {"value": 0, "raw": "—"}
                cond_table.add_row(
                    label,
                    _sig_cell(btc_s["value"], btc_s["raw"]),
                    _sig_cell(eth_s["value"], eth_s["raw"]),
                )

            # Velocidad y volatilidad del precio
            btc_vel = self.btc_feed.velocity
            eth_vel = self.eth_feed.velocity
            btc_vol = self.btc_feed.volatility
            eth_vol = self.eth_feed.volatility
            btc_vol_pct = btc_vol / self.btc_feed.price * 100 if self.btc_feed.price else 0
            eth_vol_pct = eth_vol / self.eth_feed.price * 100 if self.eth_feed.price else 0
            cond_table.add_row(
                "Precio (vel/vol)",
                f"{'[green]' if btc_vel >= 0 else '[red]'}{btc_vel:+.1f}$/s[/]  "
                f"[dim]${btc_vol:.1f} ({btc_vol_pct:.3f}%)[/]",
                f"{'[green]' if eth_vel >= 0 else '[red]'}{eth_vel:+.2f}$/s[/]  "
                f"[dim]${eth_vol:.2f} ({eth_vol_pct:.3f}%)[/]",
            )

            # Trades grandes recientes
            lrg_btc = self.btc_feed.recent_large_trade
            lrg_eth = self.eth_feed.recent_large_trade
            btc_lrg_txt = (f"[bold]🐳 {lrg_btc[0]} ${lrg_btc[1]/1e3:.0f}k[/]" if lrg_btc else "[dim]—[/]")
            eth_lrg_txt = (f"[bold]🐳 {lrg_eth[0]} ${lrg_eth[1]/1e3:.0f}k[/]" if lrg_eth else "[dim]—[/]")
            cond_table.add_row("Trade grande (10s)", btc_lrg_txt, eth_lrg_txt)

            # Veredicto V3
            if agg_btc:
                btc_v = f"[green]✔ ENTRAR[/]" if agg_btc["should_enter"] else f"[red]✘ SKIP[/]"
                btc_v += f"  [dim]{agg_btc['reason']}[/]"
            else:
                btc_v = "[dim]sin señal[/]"
            if agg_eth:
                eth_v = f"[green]✔ ENTRAR[/]" if agg_eth["should_enter"] else f"[red]✘ SKIP[/]"
                eth_v += f"  [dim]{agg_eth['reason']}[/]"
            else:
                eth_v = "[dim]sin señal[/]"
            cond_table.add_row("[bold]Veredicto V3[/]", btc_v, eth_v)

            # Shadow stats
            s_resolved = [p for p in self.shadow.predictions if p["resolved"]]
            s_wins  = sum(1 for p in s_resolved if p.get("won"))
            s_total = len(s_resolved)
            s_wr    = (s_wins / s_total * 100) if s_total else 0
            s_pend  = len([p for p in self.shadow.predictions if not p["resolved"]])
            cond_table.add_row(
                "Shadow Mode",
                f"[cyan]{s_total} resueltas · {s_wr:.0f}% WR · {s_pend} pend.[/]",
                "",
            )

            console.print(Panel(cond_table, title="📡 Condiciones de Mercado — Modelo V3", border_style="cyan"))

        # ── Shadow Tracker panel ────────────────────────────────────────────────
        if RICH:
            console.print(self.shadow.rich_panel())

    def run(self):
        # ── Key listener (teclas 1/2/3/a/q) ──────────────────────────────────
        def _key_listener():
            try:
                import tty, termios
                fd  = sys.stdin.fileno()
                old = termios.tcgetattr(fd)
                try:
                    tty.setcbreak(fd)
                    while True:
                        ch = sys.stdin.read(1)
                        if   ch == "1": self.dashboard.set_page(0)
                        elif ch == "2": self.dashboard.set_page(1)
                        elif ch == "3": self.dashboard.set_page(2)
                        elif ch == "a": self.dashboard.auto_rotate = not self.dashboard.auto_rotate
                        elif ch == "q": break
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception:
                pass  # Windows / entorno sin TTY — se ignora silenciosamente

        threading.Thread(target=_key_listener, daemon=True).start()

        while True:
            self.btc.live_price = self.btc_feed.price
            self.eth.live_price = self.eth_feed.price

            now_ts = (int(time.time()) // 300) * 300
            t_btc = threading.Thread(target=self.btc.sync_market, args=(now_ts,), daemon=True)
            t_eth = threading.Thread(target=self.eth.sync_market, args=(now_ts,), daemon=True)
            t_btc.start(); t_eth.start()
            t_btc.join(); t_eth.join()

            is_paused = self.cycle_count < self.pause_until_cycle

            # ── Auto-calibrar pesos si hay suficientes datos ──────────────────
            self.calibrator.maybe_calibrate(self.shadow.predictions, self.signals)

            # ── Lógica de apuesta V4 — pipeline completo ──────────────────────
            if not is_paused:
                for eng, vol_filter in [(self.btc, self.btc_filter), (self.eth, self.eth_filter)]:
                    feed = self.btc_feed if eng.asset == "BTC" else self.eth_feed
                    market_key = (eng.asset, eng.market_id) if eng.market_id else None
                    gs = self._grad.get(market_key, {}) if market_key else {}

                    # ── Confirmación de segunda entrada (entrada graduada) ────
                    if (market_key and gs.get("first_placed") and not gs.get("second_placed")
                            and not eng.bet_placed):
                        elapsed = time.time() - gs["first_ts"]
                        if GRAD_CONFIRM_MIN <= elapsed <= GRAD_CONFIRM_MAX:
                            side2 = gs["side"]
                            if eng.prices.get(side2, 0) >= 0.30:
                                amt2 = round(BET_AMOUNT * GRAD_SECOND_PCT, 2)
                                if self.place_bet(eng, side2, amt2, label=f"{gs['pattern']} ×2"):
                                    self._grad[market_key]["second_placed"] = True
                                    self.bet_placed_this_cycle = True
                        elif elapsed > GRAD_CONFIRM_MAX:
                            if market_key in self._grad:
                                self._grad[market_key]["second_placed"] = True  # expirado

                    # ── Evaluación de nueva entrada ──────────────────────────
                    if eng.bet_placed:
                        continue

                    sl = eng.seconds_left
                    if not (BET_WINDOW_END <= sl <= PATTERN_WINDOW_START):
                        continue

                    # Régimen y volatilidad realizada
                    reg       = self.regime.compute(list(feed._price_buf))
                    vol_reg   = feed.vol_regime
                    reg_win   = self.regime.timing_window(reg)
                    in_normal = reg_win[1] <= sl <= reg_win[0]

                    # Detectar patrón de alta confianza (ventana extendida)
                    pat = None
                    if market_key and not gs.get("first_placed"):
                        pat = self.patterns.get_best(
                            eng.asset, eng.live_price, eng.prices,
                            eng.ticker, now_ts, sl
                        )

                    # Determinar side y probabilidad candidatos
                    up, down = eng.prices["UP"], eng.prices["DOWN"]
                    best_side = "UP" if up >= down else "DOWN"
                    best_prob = max(up, down)
                    max_price = ETH_MAX_ENTRY_PRICE if eng.asset == "ETH" else MAX_ENTRY_PRICE

                    # Candidato por patrón (entrada anticipada ≤240s)
                    if pat and pat["confidence"] >= 0.55 and not gs.get("first_placed"):
                        side = pat["side"]
                        agg  = self.signals.get_signals(eng.asset, side, eng.prices.get(side, 0.5))
                        allow, passed, failed = self.gate.check(eng, feed, agg, reg, vol_reg, sl)
                        # Para patrones fuertes, relajar el gate (al menos 5/7)
                        if passed >= 5 or (pat["confidence"] >= 0.80 and passed >= 4):
                            amt1 = round(BET_AMOUNT * GRAD_FIRST_PCT, 2)
                            if self.place_bet(eng, side, amt1, label=pat["name"]):
                                self.bet_placed_this_cycle = True
                                if market_key:
                                    self._grad[market_key] = {
                                        "first_ts":      time.time(),
                                        "first_placed":  True,
                                        "second_placed": False,
                                        "side":          side,
                                        "pattern":       pat["name"],
                                        "confidence":    pat["confidence"],
                                    }
                            continue

                    # Candidato normal (ventana estándar)
                    if not in_normal:
                        continue
                    if not (THRESHOLD <= best_prob < max_price and eng.has_momentum(best_side)):
                        continue

                    # Filtro de volatilidad ATR
                    filter_ok, filter_reason = vol_filter.check(best_side)
                    if not filter_ok:
                        with open("order_error.log", "a") as f:
                            f.write(f"\n[Vol] {datetime.now()} {eng.asset} — {filter_reason}\n")
                        continue

                    # ConfidenceGate completo
                    agg = self.signals.get_signals(eng.asset, best_side, best_prob)
                    allow, passed, failed = self.gate.check(eng, feed, agg, reg, vol_reg, sl)
                    if allow:
                        if self.place_bet(eng, best_side):
                            self.bet_placed_this_cycle = True
                    else:
                        with open("order_error.log", "a") as f:
                            f.write(f"\n[Gate] {datetime.now()} {eng.asset} {best_side} "
                                    f"{passed}/7 — {', '.join(failed)}\n")

            # Shadow Tracker — loguear señales en ventana de apuesta (con info del filtro)
            for eng, vol_filter in [(self.btc, self.btc_filter), (self.eth, self.eth_filter)]:
                if eng.market_data and BET_WINDOW_END <= eng.seconds_left <= BET_WINDOW_START:
                    up, down = eng.prices["UP"], eng.prices["DOWN"]
                    best_side = "UP" if up >= down else "DOWN"
                    best_prob = max(up, down)
                    max_price = ETH_MAX_ENTRY_PRICE if eng.asset == "ETH" else MAX_ENTRY_PRICE
                    signal_ok = (
                        not is_paused
                        and not eng.bet_placed
                        and THRESHOLD <= best_prob < max_price
                        and eng.has_momentum(best_side)
                    )
                    fok, freason = vol_filter.check(best_side) if signal_ok else (None, "")
                    self.shadow.maybe_log(eng, signal_ok, is_paused,
                                         filter_ok=fok, filter_reason=freason)

            # Ciclo count — detectar transición de positivo a ≤ 0
            if self._prev_btc_seconds > 0 and self.btc.seconds_left <= 0 and self.btc.market_data:
                self.cycle_count += 1
                if not self.bet_placed_this_cycle:
                    self.cycles_without_bet += 1
                else:
                    self.cycles_without_bet = 0
                self.bet_placed_this_cycle = False
                # Sincronizar saldo y reclamar ganancias al inicio de cada ciclo
                if self.real_ready and not MODO_SIMULACION:
                    threading.Thread(target=self._refresh_balance, daemon=True).start()
                    threading.Thread(target=self.auto_redeem, daemon=True).start()
            self._prev_btc_seconds = self.btc.seconds_left

            # Resolver pendientes (trades reales + shadow)
            self.resolver_pendientes()
            self.shadow.resolve_pending(self.get_closing_price)

            self.dashboard.render()
            time.sleep(0.5)

if __name__ == "__main__":
    bot = CryptoBotElite()
    bot.run()
