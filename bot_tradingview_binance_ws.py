# bot_tradingview_binance_ws.py — FINAL
# Binance Futures — Renko 550 pts + EMA9/21 + RSI14
# - Warm-up (EMA/RSI estáveis) + reancoragem no preço atual
# - PnL detalhado só nas ordens
# - STOP + reversão no MESMO tijolo (sem bloquear)
# - Reconexão robusta (watchdog 180s)
# - Keepalive real: ping WS 45s + TCP keepalive
# - Fallback HTTP (ticker_price) se WS ficar mudo > 2s
# - Logs: 1 linha por tijolo + ordens

import os, time, json, threading, datetime as dt, socket, functools, traceback
from binance.um_futures import UMFutures
from binance.websocket.um_futures.websocket_client import UMFuturesWebsocketClient

# ---------- estilo ----------
RESET="\033[0m"; RED="\033[91m"; GREEN="\033[92m"; YELLOW="\033[93m"
BLUE="\033[94m"; MAGENTA="\033[95m"; CYAN="\033[96m"; GRAY="\033[90m"
print = functools.partial(print, flush=True)

# ---------- config ----------
SYMBOL = "BTCUSDT"
LEVERAGE = 1
MARGIN_TYPE = "CROSSED"
QTY_PCT = 0.85
BOX_POINTS = 550.0
REV_BOXES = 2
EMA_FAST = 9
EMA_SLOW = 21
RSI_LEN = 14
RSI_WIN_LONG  = (40.0, 65.0)
RSI_WIN_SHORT = (35.0, 60.0)
MIN_QTY = 0.001

# robustez
NO_TICK_RESTART_S       = 180        # watchdog
HTTP_FALLBACK_AFTER_S   = 2.0        # se WS ficar mudo > 2s, liga HTTP feeder
HTTP_FALLBACK_INTERVAL  = 0.5

API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

# ---------- util ----------
def ts(): return dt.datetime.utcnow().strftime("%H:%M:%S.%f")[:-3]+"Z"

def get_client(): return UMFutures(key=API_KEY, secret=API_SECRET)
client = get_client()

def setup_symbol():
    try:
        client.change_margin_type(symbol=SYMBOL, marginType=MARGIN_TYPE)
        print(f"{GREEN}✅ Modo de margem: {MARGIN_TYPE}{RESET}")
    except Exception as e:
        if "No need" in str(e): print(f"{CYAN}ℹ️ Margem já configurada.{RESET}")
        else: print(f"{YELLOW}⚠️ change_margin_type:{RESET}", e)
    try:
        client.change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
        print(f"{GREEN}✅ Alavancagem: {LEVERAGE}x{RESET}")
    except Exception as e:
        print(f"{YELLOW}⚠️ change_leverage:{RESET}", e)

# ---------- EMA / RSI ----------
class EMA:
    def __init__(self, n): self.mult = 2.0/(n+1.0); self.value=None
    def update(self, x):
        self.value = x if self.value is None else (x-self.value)*self.mult + self.value
        return self.value

class RSI:
    def __init__(self, n): self.n=n; self.up=None; self.dn=None; self.prev=None
    def update(self, x):
        if self.prev is None:
            self.prev=x; return None
        ch=x-self.prev; u=ch if ch>0 else 0.0; d=-ch if ch<0 else 0.0
        if self.up is None: self.up, self.dn = u, d
        else:
            self.up=(self.up*(self.n-1)+u)/self.n
            self.dn=(self.dn*(self.n-1)+d)/self.n
        self.prev=x
        rs=self.up/max(self.dn,1e-12)
        return 100.0 - 100.0/(1.0+rs)

# ---------- Renko ----------
class Renko:
    def __init__(self, box, rev):
        self.box=float(box); self.rev=int(rev)
        self.anchor=None; self.dir=0; self.id=0
    def reanchor(self, px): self.anchor=px; self.dir=0
    def feed(self, px):
        bricks=[]
        if self.anchor is None:
            self.anchor=px; return bricks
        up=self.anchor+self.box; dn=self.anchor-self.box
        while px>=up:
            self.anchor += self.box if self.dir!=-1 else self.box*self.rev
            self.dir=1; self.id+=1; bricks.append((self.anchor, 1, self.id))
            up=self.anchor+self.box; dn=self.anchor-self.box
        while px<=dn:
            self.anchor -= self.box if self.dir!=1 else self.box*self.rev
            self.dir=-1; self.id+=1; bricks.append((self.anchor,-1, self.id))
            up=self.anchor+self.box; dn=self.anchor-self.box
        return bricks

# ---------- estado ----------
class State:
    def __init__(self):
        self.in_long=False; self.in_short=False
        self.ema1=EMA(EMA_FAST); self.ema2=EMA(EMA_SLOW); self.rsi=RSI(RSI_LEN)
        # debounce de ENTRADA (evita repetir a MESMA direção no MESMO brick)
        self.last_entry_brick=None
        self.last_entry_side=None  # "BUY"/"SELL"
    def upd(self, x): return self.ema1.update(x), self.ema2.update(x), self.rsi.update(x)

# ---------- preço / qty ----------
def _first_number(*vals):
    for v in vals:
        try:
            if v is None: continue
            return float(v)
        except Exception:
            pass
    return 0.0

def extract_px(msg):
    try:
        if isinstance(msg, str): msg=json.loads(msg)
        if isinstance(msg, dict):
            v=_first_number(msg.get("p"), msg.get("c"), msg.get("price"))
            if v: return v
            for k in ("data","payload","k"):
                sub=msg.get(k)
                if isinstance(sub, dict):
                    v=_first_number(sub.get("p"), sub.get("c"), sub.get("price"))
                    if v: return v
        return 0.0
    except Exception:
        return 0.0

def calc_qty(p):
    try:
        bal=client.balance()
        usdt=float(next((b.get("balance") for b in bal if b.get("asset")=="USDT"), 0.0))
        q = (usdt*QTY_PCT)/max(p,1e-9)
        return max(MIN_QTY, round(q,3))
    except Exception:
        return MIN_QTY

# ---------- PnL (só em ordens) ----------
def show_pnl():
    try:
        d=client.get_position_risk(symbol=SYMBOL)
        if not d: return
        p=d[0]
        pnl=float(p.get("unRealizedProfit",0)); entry=float(p.get("entryPrice",0))
        amt=float(p.get("positionAmt",0)); mark=float(p.get("markPrice",0))
        side="LONG" if amt>0 else "SHORT" if amt<0 else "FLAT"
        if entry>0 and amt!=0:
            pct=((mark-entry)/entry*100)*(1 if amt>0 else -1)
            color=GREEN if pnl>=0 else RED
            print(f"{color}💰 PnL {pnl:.2f} USDT | Δ {pct:.3f}% | {side}{RESET}")
    except Exception as e:
        print(f"{YELLOW}⚠️ Erro PnL:{RESET}", e)

# ---------- ordens ----------
def market_order(side, q, reduce=False):
    try:
        params=dict(symbol=SYMBOL, side=side, type="MARKET", quantity=q)
        if reduce: params["reduceOnly"]="true"
        client.new_order(**params)
        tag = (YELLOW if reduce else GREEN)
        print(f"{tag}✅ {side} qty={q} reduceOnly={reduce}{RESET}")
        time.sleep(0.2)
        show_pnl()
        return True
    except Exception as e:
        # tenta recriar sessão 1x
        try:
            print(f"{YELLOW}⚠️ Recriando sessão e reenviando…{RESET}")
            global client
            client = get_client()
            params=dict(symbol=SYMBOL, side=side, type="MARKET", quantity=q)
            if reduce: params["reduceOnly"]="true"
            client.new_order(**params)
            print(f"{GREEN}✅ {side} qty={q} reduceOnly={reduce}{RESET}")
            time.sleep(0.2); show_pnl()
            return True
        except Exception as e2:
            msg=str(e2)
            if "-2019" in msg or "Margin is insufficient" in msg:
                print(f"{RED}❌ Ordem recusada: margem insuficiente (-2019).{RESET}")
            else:
                print(f"{RED}❌ Erro ordem:{RESET}", e2)
            return False

# ---------- lógica por tijolo ----------
def on_brick(state, close, d, brick_id):
    e1,e2,r = state.upd(close)
    if e1 is None or e2 is None or r is None: return

    verde = (d== 1)
    verm  = (d==-1)
    print(f"{MAGENTA}{ts()} | 🧱 {brick_id} {'▲' if verde else '▼'} | close={close:.2f} | EMA9={e1:.2f} EMA21={e2:.2f} | RSI={r:.2f}{RESET}")

    q = calc_qty(close)

    # 1) STOP (não grava debounce → permite reversão no MESMO tijolo)
    if state.in_long and verm:
        if market_order("SELL", q, True):
            print(f"{YELLOW}🛑 STOP COMPRA #{brick_id} @ {close:.2f}{RESET}")
            state.in_long=False
    if state.in_short and verde:
        if market_order("BUY", q, True):
            print(f"{YELLOW}🛑 STOP VENDA  #{brick_id} @ {close:.2f}{RESET}")
            state.in_short=False

    # 2) ENTRADAS (debounce por brick/direção; permite stop+reversão no mesmo brick)
    def dup(side): return (state.last_entry_brick==brick_id and state.last_entry_side==side)

    long_ok  =  verde and (e1>e2) and (RSI_WIN_LONG[0]  <= r <= RSI_WIN_LONG[1])  and not state.in_long
    short_ok =  verm  and (e1<e2) and (RSI_WIN_SHORT[0] <= r <= RSI_WIN_SHORT[1]) and not state.in_short

    if long_ok and not dup("BUY"):
        if market_order("BUY", q):
            print(f"{GREEN}🚀 COMPRA #{brick_id} @ {close:.2f}{RESET}")
            state.in_long=True; state.in_short=False
            state.last_entry_brick=brick_id; state.last_entry_side="BUY"

    if short_ok and not dup("SELL"):
        if market_order("SELL", q):
            print(f"{CYAN}🔻 VENDA  #{brick_id} @ {close:.2f}{RESET}")
            state.in_short=True; state.in_long=False
            state.last_entry_brick=brick_id; state.last_entry_side="SELL"

# ---------- warm-up ----------
def warmup(state, renko):
    try:
        kl = client.klines(symbol=SYMBOL, interval="1m", limit=500)
        for k in kl:
            close=float(k[4])
            for brick_close, d, brick_id in renko.feed(close):
                state.upd(brick_close)
        # reancora no preço atual
        try:
            px = float(client.ticker_price(symbol=SYMBOL)['price'])
            renko.reanchor(px)
        except Exception:
            pass
        print(f"{CYAN}🧰 Warm-up concluído e reancorado.{RESET}")
    except Exception as e:
        print(f"{YELLOW}⚠️ Warm-up:{RESET}", e)

# ---------- loop principal ----------
def run():
    setup_symbol()
    while True:
        state = State()
        renko = Renko(BOX_POINTS, REV_BOXES)
        warmup(state, renko)

        ws = UMFuturesWebsocketClient()
        last_tick = time.time()
        stop_ev = threading.Event()

        # feeder WS
        def on_msg(_, message):
            nonlocal last_tick
            try:
                p = extract_px(message)
                if p > 0:
                    last_tick = time.time()
                    for c, d, i in renko.feed(p):
                        on_brick(state, c, d, i)
            except Exception as e:
                print(f"{YELLOW}⚠️ on_msg:{RESET}", e)

        # ping de manutenção (WS + TCP keepalive)
        def keepalive():
            while not stop_ev.is_set():
                try:
                    ws.ping()
                    if hasattr(ws, "_socket") and ws._socket:
                        try:
                            ws._socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                            ws._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 45)
                            ws._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 15)
                            ws._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
                        except Exception:
                            pass
                except Exception:
                    break
                time.sleep(45)

        # fallback HTTP se WS silenciar
        def http_fallback():
            while not stop_ev.is_set():
                time.sleep(HTTP_FALLBACK_INTERVAL)
                silence = time.time() - last_tick
                if silence < HTTP_FALLBACK_AFTER_S:
                    continue
                try:
                    p = float(client.ticker_price(symbol=SYMBOL)['price'])
                    for c, d, i in renko.feed(p):
                        on_brick(state, c, d, i)
                except Exception:
                    pass

        try:
            # assinaturas — cobre diferenças de versões do SDK
            ws.agg_trade(id=1, symbol=SYMBOL.lower(), callback=on_msg)
            try:
                ws.mark_price(id=2, symbol=SYMBOL.lower(), speed=1, callback=on_msg)
            except TypeError:
                ws.mark_price(id=2, symbol=SYMBOL.lower(), callback=on_msg)

            threading.Thread(target=keepalive, daemon=True).start()
            threading.Thread(target=http_fallback, daemon=True).start()
            print(f"{BLUE}▶️ WS ativo para {SYMBOL}. Aguardando bricks…{RESET}")
        except Exception as e:
            print(f"{RED}❌ Erro ao abrir WS:{RESET}", e)

        # watchdog
        try:
            while True:
                time.sleep(5)
                if time.time() - last_tick > NO_TICK_RESTART_S:
                    print(f"{YELLOW}⏱️ Sem tick, reiniciando…{RESET}")
                    stop_ev.set()
                    try: ws.stop()
                    except Exception: pass
                    break
        except Exception:
            stop_ev.set()
            try: ws.stop()
            except Exception: pass

        print(f"{CYAN}⚡ Reabrindo WS…{RESET}")
        time.sleep(1)

if __name__ == "__main__":
    run()
