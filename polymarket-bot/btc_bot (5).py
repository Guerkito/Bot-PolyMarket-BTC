"""
BTC Polymarket Bot
==================
- Busca los mercados BTC 5min REALES de Polymarket
- Cuando cualquier lado > 85c faltando 40s -> apuesta $2
- pip install requests
- python btc_bot.py
"""

import os, time, requests, threading, json
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional

# ══════════════════════════
#  CONFIG
# ══════════════════════════
BALANCE_INICIAL = 20.00
BET_AMOUNT      = 2.00
MIN_ODDS        = 85       # centavos minimos para apostar
BET_AT_SECS     = 40       # segundos restantes para evaluar
SESSION_MINUTES = 180
PRICE_INTERVAL  = 5

# Colores
GR="\033[92m"; RD="\033[91m"; YL="\033[93m"
CY="\033[96m"; WH="\033[97m"; DM="\033[2m"; BL="\033[1m"; RS="\033[0m"

def cls(): print("\033[H\033[J", end="", flush=True)
def ts():  return datetime.now().strftime("%H:%M:%S")
def ln(n=58, c="─"): return c * n


# ══════════════════════════
#  PRECIO BTC
# ══════════════════════════
def get_price():
    for name, url, parse in [
        ("Binance",  "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
         lambda d: float(d["price"])),
        ("Coinbase", "https://api.coinbase.com/v2/prices/BTC-USD/spot",
         lambda d: float(d["data"]["amount"])),
        ("CryptoC",  "https://min-api.cryptocompare.com/data/price?fsym=BTC&tsyms=USD",
         lambda d: float(d["USD"])),
    ]:
        try:
            r = requests.get(url, timeout=5)
            p = parse(r.json())
            if p and p > 10000: return p, name
        except: pass
    return None, None


# ══════════════════════════
#  POLYMARKET - ODDS REALES
# ══════════════════════════
def get_poly_odds():
    """
    Busca el mercado BTC up/down de 5 minutos activo.
    Polymarket los lista bajo el evento 'bitcoin-up-or-down'.
    """
    try:
        # Endpoint 1: buscar por slug del evento live de BTC
        r = requests.get(
            "https://gamma-api.polymarket.com/events?slug=bitcoin-up-or-down",
            timeout=7
        )
        if r.status_code == 200:
            events = r.json()
            if not isinstance(events, list):
                events = events.get("data", [])
            for event in events:
                markets = event.get("markets", [])
                for m in markets:
                    prices_raw = m.get("outcomePrices", "[]")
                    try:
                        prices = [float(p) for p in json.loads(
                            prices_raw if isinstance(prices_raw, str) else "[]")]
                        if len(prices) >= 2:
                            up   = round(prices[0] * 100)
                            down = round(prices[1] * 100)
                            if 1 < up < 99 and 1 < down < 99:
                                return up, down, m.get("question", "")[:60]
                    except: pass
    except: pass

    try:
        # Endpoint 2: buscar mercados con "up or down" en titulo
        r = requests.get(
            "https://gamma-api.polymarket.com/markets?closed=false&active=true&limit=200&sort=end_date_min&order=ASC",
            timeout=7
        )
        if r.status_code == 200:
            markets = r.json()
            if not isinstance(markets, list):
                markets = markets.get("data", [])
            for m in markets:
                q = (m.get("question") or "").lower()
                # Los mercados live de BTC tienen "up or down" en el titulo
                if ("up or down" in q or "arriba o abajo" in q) and ("btc" in q or "bitcoin" in q):
                    prices_raw = m.get("outcomePrices", "[]")
                    try:
                        prices = [float(p) for p in json.loads(
                            prices_raw if isinstance(prices_raw, str) else "[]")]
                        if len(prices) >= 2:
                            up   = round(prices[0] * 100)
                            down = round(prices[1] * 100)
                            if 1 < up < 99 and 1 < down < 99:
                                return up, down, m.get("question", "")[:60]
                    except: pass
    except: pass

    try:
        # Endpoint 3: CLOB API - mercados activos ordenados por end date
        r = requests.get(
            "https://clob.polymarket.com/markets?next_cursor=&limit=100",
            timeout=7
        )
        if r.status_code == 200:
            data = r.json()
            markets = data.get("data", []) if isinstance(data, dict) else data
            for m in markets:
                q = (m.get("question") or "").lower()
                if ("up or down" in q or "arriba o abajo" in q) and ("btc" in q or "bitcoin" in q):
                    tokens = m.get("tokens", [])
                    if len(tokens) >= 2:
                        try:
                            up   = round(float(tokens[0].get("price", 0)) * 100)
                            down = round(float(tokens[1].get("price", 0)) * 100)
                            if 1 < up < 99 and 1 < down < 99:
                                return up, down, m.get("question", "")[:60]
                        except: pass
    except: pass

    return None, None, None


# ══════════════════════════
#  VENTANA 5 MIN
# ══════════════════════════
def get_window():
    n   = datetime.now()
    el  = (n.minute % 5) * 60 + n.second
    rem = 300 - el
    wid = (n.hour * 60 + n.minute) // 5
    ms  = (n.minute // 5) * 5
    lbl = f"{n.hour:02d}:{ms:02d} -> {n.hour:02d}:{ms+5:02d}"
    return rem, wid, lbl


# ══════════════════════════
#  ESTADO
# ══════════════════════════
@dataclass
class Trade:
    num: int; side: str; target: float; entry: float
    gap: float; odds: int; amount: float; pot: float
    result: str="PENDING"; pnl: float=0.0
    close: float=0.0; ts: str=""

class St:
    def __init__(self):
        self.price:   Optional[float] = None
        self.src:     str  = "—"
        self.upd:     str  = "—"
        self.history: list = []
        self.secs:    int  = 300
        self.target:  Optional[float] = None
        self.wlbl:    str  = "—"
        self.wid:     Optional[int]   = None
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

    def log_(self, msg, tag=""):
        with self.lock:
            self.log.insert(0, f"{ts()} {tag} {msg}")
            self.log = self.log[:25]

    @property
    def gap(self):
        if self.price and self.target: return self.price - self.target
        return None
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
    @property
    def best(self):
        # Siempre usar Polymarket si disponible
        if self.poly_ok and self.poly_up and self.poly_dn:
            best = max(self.poly_up, self.poly_dn)
            side = "ARRIBA" if self.poly_up >= self.poly_dn else "ABAJO"
            return best, side, "Poly"
        # Fallback: gap vs target
        g = self.gap
        if g is None: return None, None, None
        side = "ABAJO" if g < 0 else "ARRIBA"
        odds = min(93, round(50 + abs(g) * 1.2))
        return odds, side, "gap"

st = St()


# ══════════════════════════
#  THREADS
# ══════════════════════════
def th_price():
    while not st.done:
        p, src = get_price()
        if p:
            prev = st.price
            with st.lock:
                st.price   = p
                st.src     = src
                st.upd     = ts()
                st.history = (st.history + [p])[-60:]
                if st.target is None and st.wid is not None:
                    st.target = p
            if prev:
                d = p - prev
                st.log_(f"BTC ${p:,.2f}  {'▲' if d>=0 else '▼'}${abs(d):.2f}  [{src}]",
                        "✓" if d>=0 else "↓")
            else:
                st.log_(f"Conectado — BTC ${p:,.2f}  [{src}]", "✓")
        else:
            st.log_("Sin precio BTC", "!")
        time.sleep(PRICE_INTERVAL)


def th_poly():
    while not st.done:
        up, dn, q = get_poly_odds()
        if up and dn:
            changed = (st.poly_up != up or st.poly_dn != dn)
            with st.lock:
                st.poly_up = up
                st.poly_dn = dn
                st.poly_q  = q or ""
                st.poly_ok = True
            if changed:
                best = max(up, dn)
                side = "ARRIBA" if up >= dn else "ABAJO"
                flag = f"  <- APOSTARA!" if best >= MIN_ODDS else ""
                st.log_(f"POLY  ARRIBA {up}c  ABAJO {dn}c  -> {side} {best}c{flag}", "🟢")
        else:
            with st.lock:
                st.poly_ok = False
            st.log_("Polymarket: buscando mercado BTC 5min...", "⏳")
        time.sleep(4)


def th_clock():
    while not st.done:
        rem, wid, lbl = get_window()
        with st.lock:
            st.secs = rem

            # Nueva ventana
            if wid != st.wid:
                if st.active and st.price:
                    b    = st.active
                    fp   = st.price
                    won  = (fp < b.target) if b.side=="ABAJO" else (fp > b.target)
                    gain = round(b.amount*(100/max(b.odds,1)-1),2) if won else -b.amount
                    b.result = "WIN" if won else "LOSS"
                    b.pnl    = gain
                    b.close  = fp
                    st.balance = round(st.balance + gain, 2)
                    st.active  = None
                    ps = f"+${gain:.2f}" if gain>=0 else f"-${abs(gain):.2f}"
                    st.log_(
                        f"{'WIN' if won else 'LOSS'}  "
                        f"cierre ${fp:,.2f}  target ${b.target:,.2f}  "
                        f"{ps}  bal ${st.balance:.2f}",
                        "✓" if won else "✗"
                    )
                st.wid    = wid
                st.placed = False
                st.wlbl   = lbl
                st.target = st.price
                if st.price:
                    st.log_(f"Mercado {lbl}  target ${st.price:,.2f}", "◆")

            # Apostar a los 40s
            if (rem <= BET_AT_SECS and rem > (BET_AT_SECS-4)
                    and not st.placed
                    and st.balance >= BET_AMOUNT
                    and st.price and st.target
                    and not st.done):

                odds, side, src = st.best
                if odds and odds >= MIN_ODDS:
                    pot  = round(BET_AMOUNT*(100/odds-1), 2)
                    gap  = st.price - st.target
                    st.tnum += 1
                    b = Trade(
                        num=st.tnum, side=side, target=st.target,
                        entry=st.price, gap=round(gap,2), odds=odds,
                        amount=BET_AMOUNT, pot=pot, ts=ts()
                    )
                    st.placed  = True
                    st.active  = b
                    st.balance = round(st.balance - BET_AMOUNT, 2)
                    st.trades.insert(0, b)
                    st.log_(
                        f"APUESTA #{st.tnum} {side}  {odds}c [{src}]  "
                        f"gap ${abs(gap):.2f}  +${pot}  bal ${st.balance:.2f}",
                        "⚡"
                    )
                elif odds:
                    st.placed   = True
                    st.skipped += 1
                    st.log_(f"Sin apuesta — {side} {odds}c < {MIN_ODDS}c", "⊘")

        time.sleep(1)


def th_timer():
    st.start = datetime.now()
    st.end   = st.start + timedelta(minutes=SESSION_MINUTES)
    st.log_(f"Sesion {SESSION_MINUTES}min  termina {st.end.strftime('%H:%M:%S')}", "🕐")
    while datetime.now() < st.end and not st.done:
        time.sleep(1)
    st.done = True
    st.log_("Sesion completada!", "🏁")


# ══════════════════════════
#  DISPLAY
# ══════════════════════════
def bbar(f, t, w=44):
    n = min(w, int(f/max(t,1)*w))
    return CY+"█"*n+RS+DM+"░"*(w-n)+RS

def display():
    cls()
    g   = st.gap
    ag  = abs(g) if g is not None else 0
    inz = st.secs <= BET_AT_SECS
    mw  = st.secs // 60
    sw  = st.secs % 60
    net = st.pnl
    sl  = st.sess_left
    sm, ss = sl//60, sl%60
    nc  = GR if net>=0 else RD
    bc  = GR if st.balance>=BALANCE_INICIAL else RD
    wc  = YL if inz else CY

    # ── Header ────────────────────────────────────
    print(f"{CY}{BL}{ln(58,'═')}{RS}")
    print(f"{CY}{BL}  ₿ BTC BOT · {SESSION_MINUTES} MIN{RS}  "
          f"{DM}inicio {st.start.strftime('%H:%M:%S') if st.start else '—'}  "
          f"termina {st.end.strftime('%H:%M:%S') if st.end else '—'}{RS}")
    total = SESSION_MINUTES*60
    print(f"  {bbar(total-sl, total)}")
    print(f"  {DM}Tiempo:{RS} {BL}{sm:02d}:{ss:02d}{RS}  "
          f"{DM}Trades:{RS} {YL}{st.done_n}{RS}  "
          f"{DM}Saltados:{RS} {RD}{st.skipped}{RS}  "
          f"{DM}Balance:{RS} {bc}{BL}${st.balance:.2f}{RS}  "
          f"{DM}P&L:{RS} {nc}{BL}{'+'if net>=0 else ''}{net:.2f}{RS}  "
          f"{DM}{st.src}{RS}")

    # ── Mercado ───────────────────────────────────
    print(f"\n{wc}{ln()}{RS}")
    print(f"{wc}  MERCADO  {st.wlbl} ET  ·  5 MIN{RS}")
    print(f"{wc}{ln()}{RS}")

    if st.poly_ok and st.poly_up and st.poly_dn:
        uc = f"{GR}{BL}" if st.poly_up  >= MIN_ODDS else GR
        dc = f"{RD}{BL}" if st.poly_dn >= MIN_ODDS else RD
        ug = round(BET_AMOUNT*(100/max(st.poly_up,1)-1), 2)
        dg = round(BET_AMOUNT*(100/max(st.poly_dn,1)-1), 2)
        tgt = f"${st.target:,.2f}" if st.target else "—"
        print(f"  {uc}ARRIBA {st.poly_up}c{RS} +${ug:.2f}   "
              f"{dc}ABAJO  {st.poly_dn}c{RS} +${dg:.2f}   "
              f"{YL}{BL}TARGET {tgt}{RS}")
        print(f"  {GR}{DM}🟢 Polymarket live{RS}")
        if st.poly_q:
            print(f"  {DM}{st.poly_q}{RS}")
    else:
        tgt  = f"${st.target:,.2f}" if st.target else "esperando..."
        gc   = GR if g and g>=0 else RD
        diff = f"({'▲' if g and g>=0 else '▼'} ${ag:.2f})" if g is not None else ""
        print(f"  {GR}ARRIBA —c{RS}    {RD}ABAJO —c{RS}    "
              f"{YL}{BL}TARGET {tgt}{RS}  {gc}{diff}{RS}")
        print(f"  {YL}⏳ Buscando mercado BTC 5min en Polymarket...{RS}")

    # Barra ventana
    bz  = int((300-BET_AT_SECS)/300*44)
    fil = int((300-st.secs)/300*44)
    wb  = ""
    for i in range(44):
        if i < fil:   wb += (YL+"█"+RS if i>=bz else CY+"█"+RS)
        else:         wb += DM+"░"+RS

    if inz and not st.placed:   bst = f"  {YL}{BL}⚡ EVALUANDO{RS}"
    elif st.placed and st.active: bst = f"  {GR}✓ APOSTADO{RS}"
    elif st.placed:               bst = f"  {RD}⊘ SALTADO{RS}"
    else:                         bst = f"  {DM}apuesta en {max(0,st.secs-BET_AT_SECS)}s{RS}"

    print(f"\n  Cierra en {wc}{BL}{mw:02d}:{sw:02d}{RS}{bst}")
    print(f"  {wb}")
    print(f"  {DM}0:00{'':14}↑ zona apuesta (40s){'':5}5:00{RS}")

    # ── Precios ───────────────────────────────────
    pc  = GR if g and g>=0 else RD
    pr  = f"{pc}{BL}${st.price:,.2f}{RS}" if st.price else f"{YL}conectando...{RS}"
    dif = f"  {pc}({'▲' if g and g>=0 else '▼'} ${ag:.2f} vs target){RS}" if g is not None else ""
    tgt = f"{YL}{BL}${st.target:,.2f}{RS}" if st.target else f"{DM}—{RS}"
    ods = (f"{GR}{BL}{st.poly_up}c{RS} ARRIBA  /  {RD}{BL}{st.poly_dn}c{RS} ABAJO"
           if st.poly_ok and st.poly_up else f"{YL}buscando...{RS}")

    print(f"\n{CY}{ln()}{RS}")
    print(f"{CY}  PRECIOS EN VIVO{RS}")
    print(f"{CY}{ln()}{RS}")
    print(f"  {DM}BINANCE{RS}  {pr}{dif}  {DM}upd {st.upd}{RS}")
    print(f"  {DM}TARGET {RS}  {tgt}")
    print(f"  {DM}ODDS   {RS}  {ods}")
    wr = st.wr
    if wr is not None:
        wrc = GR if wr>=60 else (RD if wr<50 else YL)
        print(f"  {DM}Win Rate{RS} {wrc}{BL}{wr}%{RS} {DM}({st.wins}W · {st.losses}L){RS}")
    else:
        print(f"  {DM}Sin trades aun{RS}")

    # Panel EN RESOLUCION
    if st.active:
        b   = st.active
        sc  = RD if b.side=="ABAJO" else GR
        cur = f"${st.price:,.2f}" if st.price else "—"
        si  = max(0, b.odds - st.secs)
        rb  = YL+"█"*min(44,int(si/max(b.odds,1)*44))+RS+DM+"░"*max(0,44-int(si/max(b.odds,1)*44))+RS
        print(f"\n  {CY}┌── EN RESOLUCION {'─'*33}┐{RS}")
        print(f"  {CY}│{RS}  {sc}{BL}{b.side}{RS}  $2  {b.odds}c  si gana {GR}+${b.pot}{RS}  {YL}⟳ VIVO{RS}")
        print(f"  {CY}│{RS}  Entry {WH}${b.entry:,.2f}{RS}  Target {YL}${b.target:,.2f}{RS}  Actual {pc}{cur}{RS}")
        print(f"  {CY}│{RS}  {rb}")
        print(f"  {CY}└{'─'*51}┘{RS}")

    # ── Trades ────────────────────────────────────
    print(f"\n{CY}{ln()}{RS}")
    print(f"{CY}  TRADES  {DM}balance: {BL}${st.balance:.2f}{RS}")
    print(f"{CY}{ln()}{RS}")
    shown = list(reversed(sorted(st.trades, key=lambda x: x.num)))[:8]
    if not shown:
        print(f"  {DM}— esperando primera apuesta...{RS}")
    for t in shown:
        sc = GR if t.side=="ARRIBA" else RD
        rs, ps = ((f"{YL}VIVO{RS}",f"{DM}—{RS}") if t.result=="PENDING"
                  else (f"{GR}WIN{RS}",f"{GR}+${t.pnl:.2f}{RS}") if t.result=="WIN"
                  else (f"{RD}LOSS{RS}",f"{RD}-${abs(t.pnl):.2f}{RS}"))
        print(f"  {DM}{t.num:>2}{RS}  {DM}{t.ts}{RS}  "
              f"{sc}{t.side:7}{RS}  {DM}${t.target:>10,.2f}{RS}  "
              f"{DM}{t.gap:>+7.2f}{RS}  {DM}{t.odds:>3}c{RS}  {rs}  {ps}")

    # ── Log ───────────────────────────────────────
    print(f"\n{CY}{ln()}{RS}")
    print(f"{CY}  LOG{RS}")
    print(f"{CY}{ln()}{RS}")
    for entry in st.log[:10]:
        print(f"  {DM}{entry}{RS}")


# ══════════════════════════
#  REPORTE FINAL
# ══════════════════════════
def report():
    cls()
    net = st.pnl
    nc  = GR if net>=0 else RD
    wr  = st.wr or 0
    wrc = GR if wr>=60 else (RD if wr<50 else YL)
    em  = "🟢" if net>=0 else "🔴"

    print(f"\n{YL}{BL}{ln(58,'═')}{RS}")
    print(f"{YL}{BL}  ★  REPORTE FINAL — {SESSION_MINUTES} MIN  ★{RS}")
    print(f"{YL}{BL}{ln(58,'═')}{RS}\n")
    print(f"  {em}  Balance inicial  :  ${BALANCE_INICIAL:.2f}")
    print(f"  {em}  Balance final    :  {nc}{BL}${st.balance:.2f}{RS}")
    print(f"  {em}  P&L neto         :  {nc}{BL}{'+'if net>=0 else ''}{net:.2f} USD{RS}")
    print(f"  {em}  Rentabilidad     :  {nc}{BL}{net/BALANCE_INICIAL*100:+.1f}%{RS}")
    print(f"\n       Trades          :  {st.done_n}")
    print(f"       Saltados        :  {RD}{st.skipped}{RS}  (ningun lado >= {MIN_ODDS}c)")
    print(f"       Wins / Losses   :  {GR}{st.wins}{RS} / {RD}{st.losses}{RS}")
    print(f"       Win Rate        :  {wrc}{BL}{wr}%{RS}")
    print(f"       Total apostado  :  ${st.done_n*BET_AMOUNT:.2f}")
    ga = sum(t.pnl for t in st.trades if t.pnl>0)
    lo = abs(sum(t.pnl for t in st.trades if t.pnl<0))
    print(f"       Ganancia bruta  :  {GR}${ga:.2f}{RS}")
    print(f"       Perdida bruta   :  {RD}${lo:.2f}{RS}")

    if st.done_n > 0:
        print(f"\n{CY}{ln()}{RS}")
        print(f"{'#':>3}  {'Hora':9}  {'Lado':7}  {'Target':10}  {'Entry':10}  "
              f"{'Cierre':10}  {'Odds':5}  {'Res':6}  {'P&L'}")
        print(f"{DM}{ln()}{RS}")
        for t in sorted(st.trades, key=lambda x: x.num):
            if t.result=="PENDING": continue
            sc  = GR if t.side=="ARRIBA" else RD
            rc  = GR if t.result=="WIN"  else RD
            print(f"  {t.num:>2}  {DM}{t.ts}{RS}  {sc}{t.side:7}{RS}  "
                  f"${t.target:>9,.2f}  ${t.entry:>9,.2f}  "
                  f"{'$'+f'{t.close:,.2f}':>10}  {t.odds:>4}c  "
                  f"{rc}{'WIN' if t.result=='WIN' else 'LOSS':6}{RS}  "
                  f"{rc}{'+'if t.result=='WIN' else '-'}${abs(t.pnl):.2f}{RS}")

        daily   = net * (60*24/SESSION_MINUTES)
        monthly = daily * 30
        print(f"\n{CY}{ln()}{RS}")
        print(f"  Proyeccion basada en {st.done_n} trades reales:")
        dc = GR if daily>=0 else RD
        mc = GR if monthly>=0 else RD
        print(f"  Por dia   :  {dc}{BL}{'+'if daily>=0 else ''}{daily:.2f} USD{RS}")
        print(f"  Por mes   :  {mc}{BL}{'+'if monthly>=0 else ''}{monthly:.2f} USD{RS}")

    print(f"\n{DM}{ln()}{RS}\n")


# ══════════════════════════
#  MAIN
# ══════════════════════════
if __name__ == "__main__":
    cls()
    print(f"\n{CY}{BL}  ₿ BTC POLYMARKET BOT{RS}")
    print(f"  Balance: {BL}${BALANCE_INICIAL:.2f}{RS}  "
          f"Apuesta: {BL}${BET_AMOUNT:.2f}{RS}  "
          f"Duracion: {BL}{SESSION_MINUTES} min{RS}  "
          f"Min odds: {BL}{MIN_ODDS}c{RS}")
    print(f"\n  {DM}Ctrl+C para detener{RS}\n")
    time.sleep(2)

    for fn in [th_price, th_poly, th_clock, th_timer]:
        threading.Thread(target=fn, daemon=True).start()

    try:
        while not st.done:
            display()
            time.sleep(1)
    except KeyboardInterrupt:
        st.done = True
        print(f"\n{YL}Sesion interrumpida.{RS}")

    if st.active:
        print(f"{DM}Resolviendo ultima apuesta...{RS}")
        time.sleep(6)

    report()
