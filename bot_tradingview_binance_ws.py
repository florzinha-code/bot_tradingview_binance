# bot_tradingview_binance_ws.py — versão estável
# Binance Futures WebSocket — Renko 550 pts + EMA9/21 + RSI14 + reversão = stop

import os, time, json, threading, datetime as dt, socket, traceback, functools
from binance.um_futures import UMFutures
from binance.websocket.um_futures.websocket_client import UMFuturesWebsocketClient

# ---------- cores ----------
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
RSI_WIN_LONG = (40.0,65.0)
RSI_WIN_SHORT= (35.0,60.0)
MIN_QTY = 0.001
NO_TICK_RESTART_S = 180
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

# ---------- helpers ----------
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
    except Exception as e: print(f"{YELLOW}⚠️ change_leverage:{RESET}", e)

# ---------- EMA / RSI ----------
class EMA:
    def __init__(self,n): self.mult=2/(n+1); self.value=None
    def update(self,x):
        self.value=x if self.value is None else (x-self.value)*self.mult+self.value
        return self.value

class RSI:
    def __init__(self,n): self.n=n; self.up=None; self.dn=None; self.prev=None
    def update(self,x):
        if self.prev is None: self.prev=x; return None
        ch=x-self.prev; u=ch if ch>0 else 0; d=-ch if ch<0 else 0
        if self.up is None: self.up, self.dn = u, d
        else:
            self.up=(self.up*(self.n-1)+u)/self.n
            self.dn=(self.dn*(self.n-1)+d)/self.n
        self.prev=x
        rs=self.up/max(self.dn,1e-9)
        return 100-100/(1+rs)

# ---------- Renko ----------
class Renko:
    def __init__(self,box,rev): self.box=box; self.rev=rev; self.anchor=None; self.dir=0; self.id=0
    def reanchor(self,px): self.anchor=px; self.dir=0
    def feed(self,px):
        bricks=[]
        if self.anchor is None: self.anchor=px; return bricks
        up=self.anchor+self.box; dn=self.anchor-self.box
        while px>=up:
            self.anchor+=self.box if self.dir!=-1 else self.box*self.rev
            self.dir=1; self.id+=1; bricks.append((self.anchor,1,self.id))
            up=self.anchor+self.box; dn=self.anchor-self.box
        while px<=dn:
            self.anchor-=self.box if self.dir!=1 else self.box*self.rev
            self.dir=-1; self.id+=1; bricks.append((self.anchor,-1,self.id))
            up=self.anchor+self.box; dn=self.anchor-self.box
        return bricks

# ---------- State ----------
class State:
    def __init__(self):
        self.in_long=False; self.in_short=False
        self.ema1=EMA(EMA_FAST); self.ema2=EMA(EMA_SLOW); self.rsi=RSI(RSI_LEN)
        self.last_id=None; self.last_side=None
    def upd(self,x): return self.ema1.update(x), self.ema2.update(x), self.rsi.update(x)

# ---------- price / qty ----------
def px(msg):
    try:
        if isinstance(msg,str): msg=json.loads(msg)
        if "p" in msg: return float(msg["p"])
        if "c" in msg: return float(msg["c"])
        if "data" in msg: return px(msg["data"])
        return 0.0
    except: return 0.0

def qty(p):
    try:
        bal=client.balance()
        usdt=float(next(b["balance"] for b in bal if b["asset"]=="USDT"))
        return round(max(MIN_QTY,(usdt*QTY_PCT)/p),3)
    except: return MIN_QTY

# ---------- orders ----------
def order(side, q, reduce=False):
    try:
        client.new_order(symbol=SYMBOL, side=side, type="MARKET", quantity=q, reduceOnly=reduce)
        print(f"{GREEN}✅ {side} {q} reduceOnly={reduce}{RESET}")
        return True
    except Exception as e:
        print(f"{RED}❌ Erro ordem:{RESET}", e)
        return False

# ---------- logic ----------
def logic(s,close,d,i):
    e1,e2,r=s.upd(close)
    if e1 is None or e2 is None or r is None: return
    q=qty(close)
    verde=(d==1); vermelho=(d==-1)
    print(f"{MAGENTA}{ts()} | 🧱 {i} {'▲' if verde else '▼'} | close={close:.2f} | EMA9={e1:.2f} EMA21={e2:.2f} | RSI={r:.2f}{RESET}")

    if s.in_long and vermelho:
        if order("SELL",q,True):
            print(f"{YELLOW}🛑 STOP COMPRA {i}{RESET}"); s.in_long=False
    if s.in_short and verde:
        if order("BUY",q,True):
            print(f"{YELLOW}🛑 STOP VENDA {i}{RESET}"); s.in_short=False

    if verde and (e1>e2) and (RSI_WIN_LONG[0]<=r<=RSI_WIN_LONG[1]) and not s.in_long:
        if order("BUY",q): s.in_long=True; s.in_short=False
    if vermelho and (e1<e2) and (RSI_WIN_SHORT[0]<=r<=RSI_WIN_SHORT[1]) and not s.in_short:
        if order("SELL",q): s.in_short=True; s.in_long=False

# ---------- warmup ----------
def warmup(s,r):
    try:
        kl=client.klines(symbol=SYMBOL,interval="1m",limit=500)
        for k in kl:
            c=float(k[4])
            for close,d,i in r.feed(c): s.upd(close)
        ticker=float(client.ticker_price(symbol=SYMBOL)['price'])
        if not ticker: raise ValueError("Preço inválido")
        r.reanchor(ticker)
        print(f"{CYAN}🧰 Warm-up concluído e reancorado.{RESET}")
    except Exception as e:
        print(f"{YELLOW}⚠️ Warm-up:{RESET}", e)

# ---------- WS ----------
def run():
    setup_symbol()
    while True:
        s=State(); r=Renko(BOX_POINTS,REV_BOXES); warmup(s,r)
        ws=UMFuturesWebsocketClient()
        last=time.time()
        stop_event=threading.Event()

        def on_msg(_,msg):
            nonlocal last
            try:
                p=px(msg)
                if p>0:
                    last=time.time()
                    for c,d,i in r.feed(p): logic(s,c,d,i)
            except Exception as e:
                print(f"{YELLOW}⚠️ on_msg:{RESET}", e)

        # --- keepalive TCP + ping interno Binance ---
        def ping_thread():
            while not stop_event.is_set():
                try:
                    ws.ping()  # ping interno
                    if hasattr(ws,"_socket") and ws._socket:
                        ws._socket.setsockopt(socket.SOL_SOCKET,socket.SO_KEEPALIVE,1)
                    time.sleep(45)
                except Exception: break

        threading.Thread(target=ping_thread,daemon=True).start()

        try:
            ws.start()
            ws.agg_trade(symbol=SYMBOL.lower(), id=1, callback=on_msg)
            ws.mark_price(symbol=SYMBOL.lower(), id=2, speed=1, callback=on_msg)
            print(f"{BLUE}▶️ WS ativo para {SYMBOL}. Aguardando bricks…{RESET}")
        except Exception as e:
            print(f"{RED}❌ Erro ao abrir WS:{RESET}", e)

        try:
            while not stop_event.is_set():
                time.sleep(5)
                if time.time()-last>NO_TICK_RESTART_S:
                    print(f"{YELLOW}⏱️ Sem tick, reiniciando…{RESET}")
                    stop_event.set(); ws.stop()
        except Exception: pass

        print(f"{CYAN}⚡ Reabrindo WS…{RESET}")
        time.sleep(2)

if __name__=="__main__":
    run()
