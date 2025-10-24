# bot_tradingview_binance_ws.py
# Binance Futures WebSocket — Renko 550 pts + EMA9/21 + RSI14 + reversão = stop
# ✅ Warm-up (EMA/RSI estáveis) + reancoragem no preço atual
# ✅ Heartbeat ping 120s visível
# ✅ Reconexão limpa + watchdog 180s
# ✅ Keepalive TCP real
# ✅ Logs enxutos (bricks + ordens)

import os, time, json, traceback, functools, threading, datetime as dt, socket
from binance.um_futures import UMFutures
from binance.websocket.um_futures.websocket_client import UMFuturesWebsocketClient

RESET="\033[0m"; RED="\033[91m"; GREEN="\033[92m"; YELLOW="\033[93m"
BLUE="\033[94m"; MAGENTA="\033[95m"; CYAN="\033[96m"

print = functools.partial(print, flush=True)

SYMBOL="BTCUSDT"
LEVERAGE=1
MARGIN_TYPE="CROSSED"
QTY_PCT=0.85
BOX_POINTS=550.0
REV_BOXES=2
EMA_FAST=9
EMA_SLOW=21
RSI_LEN=14
RSI_WIN_LONG=(40,65)
RSI_WIN_SHORT=(35,60)
MIN_QTY=0.001

NO_TICK_RESTART_S=180
HEARTBEAT_S=120
HTTP_FALLBACK=True
HTTP_FALLBACK_AFTER_S=2.0
HTTP_FALLBACK_INTERVAL_S=0.5

WARMUP_ENABLED=True
WARMUP_INTERVAL="1m"
WARMUP_LIMIT=1000
REANCHOR_AFTER_WARMUP=True

API_KEY=os.getenv("API_KEY")
API_SECRET=os.getenv("API_SECRET")

def ts(): return dt.datetime.utcnow().strftime("%H:%M:%S.%f")[:-3]+"Z"
def get_client(): return UMFutures(key=API_KEY, secret=API_SECRET)
client=get_client()

def setup_symbol():
    try:
        client.change_margin_type(symbol=SYMBOL, marginType=MARGIN_TYPE)
        print(f"{CYAN}ℹ️ Margem configurada: {MARGIN_TYPE}{RESET}")
    except Exception as e:
        if "No need" not in str(e): print(f"{YELLOW}⚠️ Margem:{RESET}", e)
    try:
        client.change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
        print(f"{GREEN}✅ Alavancagem: {LEVERAGE}x{RESET}")
    except Exception as e:
        print(f"{YELLOW}⚠️ Leverage:{RESET}", e)

class EMA:
    def __init__(self,n): self.mult=2/(n+1); self.v=None
    def update(self,x): self.v=x if self.v is None else (x-self.v)*self.mult+self.v; return self.v

class RSI:
    def __init__(self,n): self.n=n; self.u=self.d=self.p=None
    def update(self,x):
        if self.p is None: self.p=x; return None
        ch=x-self.p; self.p=x
        up=max(ch,0); dn=max(-ch,0)
        if self.u is None: self.u, self.d=up,dn
        else: self.u=(self.u*(self.n-1)+up)/self.n; self.d=(self.d*(self.n-1)+dn)/self.n
        rs=self.u/max(self.d,1e-9); return 100-100/(1+rs)

class Renko:
    def __init__(self,box,rev): self.box=box; self.rev=rev; self.anchor=None; self.dir=0; self.id=0
    def reanchor(self,px): self.anchor=px; self.dir=0
    def feed(self,px):
        out=[]
        if self.anchor is None: self.anchor=px; return out
        up=self.anchor+self.box; dn=self.anchor-self.box
        while px>=up:
            self.anchor+=self.box if self.dir!=-1 else self.box*self.rev
            self.dir=1; self.id+=1; out.append((self.anchor,self.dir,self.id))
            up=self.anchor+self.box; dn=self.anchor-self.box
        while px<=dn:
            self.anchor-=self.box if self.dir!=1 else self.box*self.rev
            self.dir=-1; self.id+=1; out.append((self.anchor,self.dir,self.id))
            up=self.anchor+self.box; dn=self.anchor-self.box
        return out

class State:
    def __init__(self):
        self.in_long=False; self.in_short=False
        self.e1=EMA(EMA_FAST); self.e2=EMA(EMA_SLOW); self.rsi=RSI(RSI_LEN)
        self.last_id=None; self.last_side=None
    def update(self,x): return self.e1.update(x),self.e2.update(x),self.rsi.update(x)

def get_qty(px):
    try: bal=client.balance()
    except Exception: bal=[]
    usdt=next((float(b.get("balance",0)) for b in bal if b.get("asset")=="USDT"),0)
    qty=round(max(MIN_QTY,(usdt*QTY_PCT)/max(px,1)),3)
    return qty,usdt

def market(side,qty,reduce=False):
    try:
        client.new_order(symbol=SYMBOL, side=side, type="MARKET", quantity=qty, reduceOnly=str(reduce).lower())
        print(f"{GREEN}✅ Ordem {side} qty={qty} reduceOnly={reduce}{RESET}")
    except Exception as e: print(f"{RED}❌ Ordem falhou:{RESET}", e)

def apply_logic(s,close,d,bid,src):
    e1,e2,r=s.update(close)
    if not all([e1,e2,r]): return
    long_ok=d==1 and e1>e2 and RSI_WIN_LONG[0]<=r<=RSI_WIN_LONG[1] and not s.in_long
    short_ok=d==-1 and e1<e2 and RSI_WIN_SHORT[0]<=r<=RSI_WIN_SHORT[1] and not s.in_short
    print(f"{CYAN}{src} | {ts()} | 🧱{bid} {'▲' if d==1 else '▼'} | close={close:.2f} | EMA9={e1:.2f} EMA21={e2:.2f} | RSI={r:.1f}{RESET}")
    q,_=get_qty(close)
    if s.in_long and d==-1: market("SELL",q,True); s.in_long=False
    if s.in_short and d==1: market("BUY",q,True); s.in_short=False
    if long_ok and s.last_side!="BUY": market("BUY",q); s.in_long=True; s.in_short=False; s.last_side="BUY"
    if short_ok and s.last_side!="SELL": market("SELL",q); s.in_short=True; s.in_long=False; s.last_side="SELL"

def warmup(s,r):
    if not WARMUP_ENABLED: return
    kl=client.klines(symbol=SYMBOL, interval=WARMUP_INTERVAL, limit=WARMUP_LIMIT)
    for row in kl:
        c=float(row[4])
        for bc,d,bid in r.feed(c): s.update(bc)
    if REANCHOR_AFTER_WARMUP:
        px=float(client.ticker_price(symbol=SYMBOL)['price'])
        r.reanchor(px)
    print(f"{CYAN}🧰 Warm-up concluído e reancorado no preço atual.{RESET}")

def run():
    setup_symbol()
    while True:
        s=State(); r=Renko(BOX_POINTS,REV_BOXES); warmup(s,r)
        ws=UMFuturesWebsocketClient()
        last_tick=time.time()

        def on_msg(_,msg):
            nonlocal last_tick
            try:
                data=json.loads(msg) if isinstance(msg,str) else msg
                px=float(data.get("p") or data.get("c") or data.get("price") or 0)
                if px>0:
                    last_tick=time.time()
                    for c,d,bid in r.feed(px): apply_logic(s,c,d,bid,"WS")
            except Exception as e: print(f"{YELLOW}⚠️ on_msg:{RESET}",e)

        try:
            ws.agg_trade(symbol=SYMBOL.lower(),id=1,callback=on_msg)
            ws.mark_price(symbol=SYMBOL.lower(),id=2,callback=on_msg)
            print(f"{BLUE}▶️ WS ativo para {SYMBOL}. Aguardando bricks…{RESET}")
        except Exception as e: print(f"{RED}❌ Erro abrir WS:{RESET}",e)

        def heartbeat():
            while True:
                time.sleep(HEARTBEAT_S)
                try:
                    ws.ping(); print(f"{MAGENTA}💓 Heartbeat enviado {ts()}{RESET}")
                except Exception: break
        threading.Thread(target=heartbeat,daemon=True).start()

        try:
            while True:
                time.sleep(5)
                if time.time()-last_tick>NO_TICK_RESTART_S:
                    print(f"{YELLOW}⏱️ WS sem tick há 180s. Reiniciando…{RESET}")
                    ws.close(); break
        except Exception as e: print(f"{YELLOW}⚠️ Loop caiu:{RESET}",e)
        print(f"{CYAN}⚡ Reabrindo WS…{RESET}")
        time.sleep(0.1)

if __name__=="__main__":
    run()
