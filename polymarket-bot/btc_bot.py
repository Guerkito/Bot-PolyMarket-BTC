import os, time, requests, threading, json, re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Optional

# ══════════════════════════
#  CONFIG
# ══════════════════════════
BALANCE_INICIAL = 20.00
BET_AMOUNT      = 2.00
MIN_ODDS        = 85       # centavos minimos para apostar
BET_AT_SECS     = 60       # Aumentado a 60s para mayor seguridad
SESSION_MINUTES = 180
PRICE_INTERVAL  = 5

# Colores
GR="\033[92m"; RD="\033[91m"; YL="\033[93m"
CY="\033[96m"; WH="\033[97m"; DM="\033[2m"; BL="\033[1m"; RS="\033[0m"

def cls(): print("\033[H\033[J", end="", flush=True)
def ts():  return datetime.now().strftime("%H:%M:%S")
def ln(n=58, c="─"): return c * n

# ══════════════════════════
#  MODELS
# ══════════════════════════
@dataclass
class Trade:
    num: int
    side: str
    target: float
    entry: float
    gap: float
    odds: int
    amount: float
    pot: float
    result: str = "PENDING"
    pnl: float = 0.0
    close: float = 0.0
    ts: str = ""

# ══════════════════════════
#  ESTADO
# ══════════════════════════
class St:
    def __init__(self):
        self.price:   Optional[float] = None
        self.src:     str  = "—"
        self.upd:     str  = "—"
        self.secs:    int  = 300
        self.target:  Optional[float] = None
        self.wlbl:    str  = "—"
        self.wid:     Optional[str]   = None
        self.placed:  bool = False
        self.poly_up: Optional[int]   = None
        self.poly_dn: Optional[int]   = None
        self.poly_q:  str  = ""
        self.poly_ok: bool = False
        self.trades:  list = []
        self.active:  Optional[Trade] = None
        self.balance: float = BALANCE_INICIAL
        self.tnum:    int  = 0
        self.skipped: int  = 0
        self.log:     list = []
        self.start:   Optional[datetime] = None
        self.end:     Optional[datetime] = None
        self.done:    bool = False
        self.lock = threading.Lock()
        self.clob_ids: list = []

    def log_(self, msg, tag=""):
        with self.lock:
            self.log.insert(0, f"{ts()} {tag} {msg}")
            self.log = self.log[:15]

    @property
    def wins(self):   return sum(1 for t in self.trades if t.result=="WIN")
    @property
    def losses(self): return sum(1 for t in self.trades if t.result=="LOSS")
    @property
    def done_n(self): return self.wins + self.losses
    @property
    def wr(self): return round(self.wins/self.done_n*100) if self.done_n>0 else None
    @property
    def pnl(self): return round(self.balance - BALANCE_INICIAL, 2)
    @property
    def sess_left(self):
        if not self.end: return SESSION_MINUTES*60
        return max(0, int((self.end - datetime.now()).total_seconds()))

st = St()

# ══════════════════════════
#  THREADS
# ══════════════════════════

def th_price():
    """Obtiene el precio real de Binance para el Dashboard."""
    while not st.done:
        try:
            r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5)
            p = float(r.json()["price"])
            with st.lock:
                st.price = p
                st.upd = ts()
                st.src = "Binance"
        except: pass
        time.sleep(2)

def th_poly():
    """Busca mercados y extrae ODDS y TARGET reales de Polymarket."""
    while not st.done:
        try:
            # 1. Encontrar mercado activo de 5m
            now_ts = int(time.time())
            current_window = (now_ts // 300) * 300
            slug = f"btc-updown-5m-{current_window}"
            
            r = requests.get(f"https://gamma-api.polymarket.com/events/slug/{slug}", timeout=5)
            if r.status_code == 200:
                event = r.json()
                market = event.get("markets", [{}])[0]
                m_id = market.get("id")
                
                # Extraer Target oficial
                target = 0
                meta = event.get("eventMetadata")
                if isinstance(meta, str): meta = json.loads(meta)
                if meta and meta.get("priceToBeat"): target = float(meta['priceToBeat'])
                
                # Obtener Precios del CLOB
                c_ids = market.get("clobTokenIds")
                if isinstance(c_ids, str): c_ids = json.loads(c_ids)
                
                if c_ids:
                    p_up = float(requests.get("https://clob.polymarket.com/price", params={"token_id": c_ids[0], "side": "BUY"}).json().get("price", 0))
                    p_dn = float(requests.get("https://clob.polymarket.com/price", params={"token_id": c_ids[1], "side": "BUY"}).json().get("price", 0))
                    
                    with st.lock:
                        st.poly_up = round(p_up * 100)
                        st.poly_dn = round(p_dn * 100)
                        st.target  = target
                        st.poly_q  = market.get("question", "")
                        st.wid     = m_id
                        st.clob_ids = c_ids
                        st.poly_ok = True
                        
                        # Actualizar tiempo restante
                        end_dt = datetime.fromisoformat(market.get("endDate").replace("Z", "+00:00"))
                        st.secs = int((end_dt - datetime.now(timezone.utc)).total_seconds())
            else:
                with st.lock: st.poly_ok = False
        except:
            with st.lock: st.poly_ok = False
        time.sleep(3)

def th_logic():
    """Evalúa y ejecuta apuestas."""
    while not st.done:
        with st.lock:
            # Detectar cambio de mercado para resetear 'placed'
            if st.secs > 280:
                st.placed = False

            # Lógica de apuesta a los 60s
            if st.secs == BET_AT_SECS and not st.placed and st.poly_ok:
                up, dn = st.poly_up, st.poly_dn
                side = "ARRIBA" if up >= MIN_ODDS else "ABAJO" if dn >= MIN_ODDS else None
                odds = up if side == "ARRIBA" else dn
                
                if side:
                    pot = round(BET_AMOUNT * (100/odds - 1), 2)
                    st.tnum += 1
                    b = Trade(num=st.tnum, side=side, target=st.target, entry=st.price or 0, 
                              gap=(st.price - st.target) if st.price else 0, odds=odds, 
                              amount=BET_AMOUNT, pot=pot, ts=ts())
                    st.active = b
                    st.balance -= BET_AMOUNT
                    st.trades.insert(0, b)
                    st.placed = True
                    st.log_(f"APUESTA {side} @ {odds}c", "⚡")
                else:
                    st.placed = True
                    st.skipped += 1
                    st.log_("Sin señal clara", "⊘")
        
        # Simular resolución al llegar a 0
        if st.secs <= 0 and st.active:
            with st.lock:
                b = st.active
                # En simulación, si el lado elegido tenía >85c, asumimos que ganó si el precio está de su lado
                # (Para real, aquí consultaríamos el resultado oficial)
                won = (st.price > b.target) if b.side == "ARRIBA" else (st.price < b.target)
                gain = b.amount + b.pot if won else 0
                b.result = "WIN" if won else "LOSS"
                b.pnl = b.pot if won else -b.amount
                st.balance += gain
                st.active = None
                st.log_(f"Resultado: {b.result}", "✓" if won else "✗")

        time.sleep(1)

def th_timer():
    st.start = datetime.now()
    st.end   = st.start + timedelta(minutes=SESSION_MINUTES)
    while datetime.now() < st.end and not st.done:
        time.sleep(1)
    st.done = True

# ══════════════════════════
#  DISPLAY (Tu diseño)
# ══════════════════════════
def display():
    cls()
    net = st.pnl
    sl  = st.sess_left
    sm, ss = sl//60, sl%60
    nc  = GR if net>=0 else RD
    bc  = GR if st.balance>=BALANCE_INICIAL else RD

    print(f"{CY}{BL}{ln(58,'═')}{RS}")
    print(f"{CY}{BL}  ₿ BTC BOT · {SESSION_MINUTES} MIN{RS}  "
          f"{DM}inicio {st.start.strftime('%H:%M:%S') if st.start else '—'}  "
          f"termina {st.end.strftime('%H:%M:%S') if st.end else '—'}{RS}")
    
    print(f"  {DM}Tiempo:{RS} {BL}{sm:02d}:{ss:02d}{RS}  "
          f"{DM}Trades:{RS} {YL}{st.done_n}{RS}  "
          f"{DM}Balance:{RS} {bc}${st.balance:.2f}{RS}  "
          f"{DM}P&L:{RS} {nc}{'+' if net>=0 else ''}{net:.2f}{RS}")

    print(f"\n{CY}{ln()}{RS}")
    if st.poly_ok:
        tgt = f"${st.target:,.2f}" if st.target else "Calculando..."
        print(f"  {GR}ARRIBA {st.poly_up}c{RS}    {RD}ABAJO {st.poly_dn}c{RS}    {YL}TARGET {tgt}{RS}")
        print(f"  {DM}{st.poly_q}{RS}")
    else:
        print(f"  {YL}⏳ Buscando mercado activo en Polymarket...{RS}")

    # Barra de tiempo
    rem_min, rem_sec = st.secs // 60, st.secs % 60
    fill = int(((300-st.secs)/300)*44)
    wb = (YL if st.secs <= 60 else CY) + "█"*fill + RS + DM + "░"*(44-fill) + RS
    print(f"\n  Cierre en: {BL}{rem_min:02d}:{rem_sec:02d}{RS}  {'[⚡ EVALUANDO]' if st.secs <= 60 and not st.placed else ''}")
    print(f"  {wb}")

    # Trades y Logs
    print(f"\n{CY}  HISTORIAL RECIENTE{RS}")
    for t in st.trades[:3]:
        res_c = GR if t.result == "WIN" else RD if t.result == "LOSS" else YL
        print(f"  {t.ts}  {t.side:7} {t.odds}c  {res_c}{t.result}{RS}  {res_c}{t.pnl:+.2f}{RS}")

    print(f"\n{CY}  LOG{RS}")
    for entry in st.log[:5]:
        print(f"  {DM}{entry}{RS}")

# ══════════════════════════
#  MAIN
# ══════════════════════════
if __name__ == "__main__":
    for fn in [th_price, th_poly, th_logic, th_timer]:
        threading.Thread(target=fn, daemon=True).start()

    try:
        while not st.done:
            display()
            time.sleep(1)
    except KeyboardInterrupt:
        st.done = True
