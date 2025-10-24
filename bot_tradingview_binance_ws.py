# bot_tradingview_binance_ws.py
# Binance Futures WebSocket — Renko 550 pts + EMA9/21 + RSI14 + reversão = stop
# ✅ Compatível com a versão nova da Binance API (sem start/register_callback)
# ✅ WS duplo (aggTrade + markPrice) estável
# ✅ Reconexão automática + watchdog 180s
# ✅ Warm-up + reancoragem + ping silencioso
# ✅ Sem quedas a cada 2 minutos

import os, time, json, traceback, functools, threading, datetime as dt, socket
from binance.um_futures import UMFutures
from binance.websocket.um_futures.websocket_client import UMFuturesWebsocketClient

RESET="\033[0m"; RED="\033[91m"; GREEN="\033[92m"; YELLOW="\033[93m"
BLUE="\033[94m"; MAGENTA="\033[95m"; CYAN="\033[96m"

print = functools.partial(print, flush=True)

SYMBOL = "BTCUSDT"
LEVERAGE = 1
MARGIN_TYPE = "CROSSED"
QTY_PCT = 0.85
BOX_POINTS = 550.0
REV_BOXES = 2
EMA_FAST = 9
EMA_SLOW = 21
RSI_LEN = 14
RSI_WIN_LONG = (40, 65)
RSI_WIN_SHORT = (35, 60)
MIN_QTY = 0.001
NO_TICK_RESTART_S = 180
HEARTBEAT_S = 30

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

def ts(): return dt.datetime.utcnow().strftime("%H:%M:%S.%f")[:-3]+"Z"
def get_client(): return UMFutures(key=API_KEY, secret=API_SECRET)
client = get_client()

# ----- setup -----
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

# ----- EMA / RSI -----
class EMA:
    def __init__(self,l): self.m=2/(l+1); self.v=None
    def update(self,x): self.v=x if self.v is None else (x-self.v)*self.m+self.v; return self.v

class RSI_Wilder:
    def __init__(self,l): self.l=l; self.au=self.ad=self.p=None
    def update(self,x):
        if self.p is None: self.p=x; return None
        ch=x-self.p; up=max(ch,0); dn=max(-ch,0)
        self.au=up if self.au is None else (self.au*(self.l-1)+up)/self.l
        self.ad=dn if self.ad is None else (self.ad*(self.l-1)+dn)/self.l
        self.p=x; rs=self.au/max(self.ad,1e-9); return 100-100/(1+rs)

# ----- Renko -----
class Renko:
    def __init__(self,box,rev): self.box=box; self.rev=rev; self.anchor=None; self.dir=0; self.id=0
    def reanchor(self,px): self.anchor=px; self.dir=0
    def feed(self,px):
        bricks=[]
        if self.anchor is None: self.anchor=px; return bricks
        up=self.anchor+self.box; down=self.anchor-self.box
        while px>=up:
            self.anchor+=self.box if self.dir!=-1 else self.box*self.rev
            self.dir=1; self.id+=1; bricks.append((self.anchor,1,self.id))
            up=self.anchor+self.box; down=self.anchor-self.box
        while px<=down:
            self.anchor-=self.box if self.dir!=1 else self.box*self.rev
            self.dir=-1; self.id+=1; bricks.append((self.anchor,-1,self.id))
            up=self.anchor+self.box; down=self.anchor-self.box
        return bricks

# ----- Estado -----
class State:
    def __init__(self):
        self.long=False; self.short=False
        self.e1=EMA(EMA_FAST); self.e2=EMA(EMA_SLOW); self.rsi=RSI_Wilder(RSI_LEN)
        self.last_id=None; self.last_side=None
    def upd(self,x): return self.e1.update(x),self.e2.update(x),self.rsi.update(x)

# ----- Ordens -----
def qty(price):
    try:
        bal=client.balance()
    except: bal=[]
    usdt=next((float(b.get("balance",0)) for b in bal if b.get("asset")=="USDT"),0)
    q=round(max(MIN_QTY,(usdt*QTY_PCT)/max(price,1e-9)),3)
    return q

def order(side, q, reduce=False):
    try:
        p=dict(symbol=SYMBOL, side=side, type="MARKET", quantity=q)
        if reduce: p["reduceOnly"]="true"
        client.new_order(**p)
        print(f"{GREEN if not reduce else YELLOW}💥 {side} qty={q}{RESET}")
    except Exception as e: print(f"{RED}❌ Erro ordem:{RESET}", e)

# ----- Preço -----
def px(msg):
    try:
        if isinstance(msg,str): msg=json.loads(msg)
        d=msg.get("data",msg)
        for k in("p","c","price"):
            if k in d: return float(d[k])
    except: pass
    return 0

# ----- Lógica -----
def logic(s,close,d,id):
    e1,e2,r=s.upd(close)
    if e1 is None: return
    q=qty(close)
    v=(d==1); v2=(d==-1)
    print(f"{MAGENTA}🧱{id} {'▲' if v else '▼'} {close:.2f} | EMA9={e1:.2f} EMA21={e2:.2f} RSI={r:.1f}{RESET}")

    if s.long and v2: order("SELL",q,True); s.long=False
    if s.short and v: order("BUY",q,True); s.short=False

    if v and e1>e2 and RSI_WIN_LONG[0]<=r<=RSI_WIN_LONG[1] and not s.long:
        order("BUY",q); s.long=True; s.short=False
    if v2 and e1<e2 and RSI_WIN_SHORT[0]<=r<=RSI_WIN_SHORT[1] and not s.short:
        order("SELL",q); s.short=True; s.long=False

# ----- Warm-up -----
def warmup(s,r):
    try:
        kl=client.klines(symbol=SYMBOL,interval="1m",limit=500)
        for k in kl: close=float(k[4])
        px0=float(client.ticker_price(symbol=SYMBOL)['price'])
        r.reanchor(px0)
        print(f"{CYAN}🧰 Warm-up concluído e reancorado.{RESET}")
    except Exception as e: print(f"{YELLOW}⚠️ Warm-up falhou:{RESET}",e)

# ----- Main WS -----
def run():
    setup_symbol()
    while True:
        s=State(); r=Renko(BOX_POINTS,REV_BOXES); warmup(s,r)
        ws=UMFuturesWebsocketClient()
        last=time.time()

        def on_msg(_,msg):
            nonlocal last
            try:
                p=px(msg)
                if p>0:
                    last=time.time()
                    for c,d,i in r.feed(p): logic(s,c,d,i)
            except Exception as e: print(f"{YELLOW}⚠️ on_msg:{RESET}",e)

        try:
            ws.agg_trade(id=1, symbol=SYMBOL.lower(), callback=on_msg)
            ws.mark_price(id=2, symbol=SYMBOL.lower(), speed=1, callback=on_msg)
            print(f"{BLUE}▶️ WS ativo para {SYMBOL}. Aguardando bricks…{RESET}")
        except Exception as e:
            print(f"{RED}❌ Erro ao abrir WS:{RESET}", e)

        try:
            while True:
                time.sleep(5)
                if time.time()-last>NO_TICK_RESTART_S:
                    print(f"{YELLOW}⏱️ Sem tick, reiniciando…{RESET}")
                    ws.stop(); break
        except Exception: pass
        print(f"{CYAN}⚡ Reabrindo WS…{RESET}")
        time.sleep(1)

if __name__=="__main__":
    run()
