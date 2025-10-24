# bot_tradingview_binance_ws.py
# Binance Futures WS – Renko 550pts + EMA9/21 + RSI14 + reversão
# ✅ Ping WS real (mantém conexão viva)
# ✅ Warm-up e reancoragem
# ✅ Reconexão 180s sem tick
# ✅ Logs limpos e claros

import os, time, json, threading, datetime as dt, traceback, functools
from binance.um_futures import UMFutures
from binance.websocket.um_futures.websocket_client import UMFuturesWebsocketClient

RESET="\033[0m"; RED="\033[91m"; GREEN="\033[92m"; YELLOW="\033[93m"
BLUE="\033[94m"; MAGENTA="\033[95m"; CYAN="\033[96m"

print = functools.partial(print, flush=True)

SYMBOL="BTCUSDT"; LEVERAGE=1; MARGIN_TYPE="CROSSED"; QTY_PCT=0.85
BOX_POINTS=550.0; REV_BOXES=2; EMA_FAST=9; EMA_SLOW=21; RSI_LEN=14
RSI_WIN_LONG=(40,65); RSI_WIN_SHORT=(35,60); MIN_QTY=0.001
NO_TICK_RESTART_S=180; API_KEY=os.getenv("API_KEY"); API_SECRET=os.getenv("API_SECRET")

def ts(): return dt.datetime.utcnow().strftime("%H:%M:%S")

client=UMFutures(key=API_KEY, secret=API_SECRET)

# === Indicadores simples ===
class EMA:
    def __init__(s,n): s.k=2/(n+1); s.v=None
    def update(s,x): s.v=x if s.v is None else s.v+(x-s.v)*s.k; return s.v

class RSI:
    def __init__(s,n): s.n=n; s.u=s.d=s.prev=None
    def update(s,x):
        if s.prev is None: s.prev=x; return None
        ch=x-s.prev; u=ch if ch>0 else 0; d=-ch if ch<0 else 0
        if s.u is None: s.u,u; s.d=d
        else: s.u=(s.u*(s.n-1)+u)/s.n; s.d=(s.d*(s.n-1)+d)/s.n
        s.prev=x; rs=s.u/max(s.d,1e-9); return 100-100/(1+rs)

class Renko:
    def __init__(s,box,rev): s.box=box; s.rev=rev; s.anchor=None; s.dir=0; s.id=0
    def reanchor(s,px): s.anchor=px; s.dir=0
    def feed(s,px):
        out=[]
        if s.anchor is None: s.anchor=px; return out
        up=s.anchor+s.box; dn=s.anchor-s.box
        while px>=up:
            s.anchor+=s.box if s.dir!=-1 else s.box*s.rev
            s.dir=1; s.id+=1; out.append((s.anchor,1,s.id))
            up=s.anchor+s.box; dn=s.anchor-s.box
        while px<=dn:
            s.anchor-=s.box if s.dir!=1 else s.box*s.rev
            s.dir=-1; s.id+=1; out.append((s.anchor,-1,s.id))
            up=s.anchor+s.box; dn=s.anchor-s.box
        return out

class State:
    def __init__(s):
        s.in_long=s.in_short=False; s.e1=EMA(EMA_FAST); s.e2=EMA(EMA_SLOW); s.r=RSI(RSI_LEN)
    def upd(s,x): return s.e1.update(x), s.e2.update(x), s.r.update(x)

def extract_px(m):
    try:
        if isinstance(m,str): m=json.loads(m)
        for k in ("p","c","price"): 
            if k in m: return float(m[k])
        if "data" in m: return extract_px(m["data"])
    except: pass
    return 0.0

def order(side,qty,reduce=False):
    try:
        client.new_order(symbol=SYMBOL,side=side,type="MARKET",quantity=qty,reduceOnly=reduce)
        print(f"{GREEN}✅ {side} {qty}{RESET}")
        return True
    except Exception as e:
        print(f"{RED}❌ ordem:{RESET}",e); return False

def logic(s,close,d,i):
    e1,e2,r=s.upd(close)
    if not all((e1,e2,r)): return
    q=0.001
    verde=(d==1); vermelho=(d==-1)
    print(f"{MAGENTA}{ts()} | 🧱 {i} {'▲' if verde else '▼'} | close={close:.2f} | EMA9={e1:.2f} EMA21={e2:.2f} | RSI={r:.1f}{RESET}")
    if s.in_long and vermelho: 
        if order("SELL",q,True): s.in_long=False
    if s.in_short and verde: 
        if order("BUY",q,True): s.in_short=False
    if verde and (e1>e2) and 40<=r<=65 and not s.in_long:
        if order("BUY",q): s.in_long=True; s.in_short=False
    if vermelho and (e1<e2) and 35<=r<=60 and not s.in_short:
        if order("SELL",q): s.in_short=True; s.in_long=False

def warmup(s,r):
    try:
        kl=client.klines(symbol=SYMBOL,interval="1m",limit=500)
        for k in kl:
            c=float(k[4])
            for close,d,i in r.feed(c): s.upd(close)
        px=float(client.ticker_price(symbol=SYMBOL)['price'])
        r.reanchor(px)
        print(f"{CYAN}🧰 Warm-up concluído e reancorado.{RESET}")
    except Exception as e: print(f"{YELLOW}⚠️ warmup:{RESET}",e)

def run():
    while True:
        s=State(); r=Renko(BOX_POINTS,REV_BOXES); warmup(s,r)
        ws=UMFuturesWebsocketClient()
        last=time.time()

        def on_msg(_,msg):
            nonlocal last
            p=extract_px(msg)
            if p>0:
                last=time.time()
                for c,d,i in r.feed(p): logic(s,c,d,i)

        # Ping WS real
        def ping_ws():
            while True:
                try:
                    ws.ping()
                except: break
                time.sleep(30)
        threading.Thread(target=ping_ws,daemon=True).start()

        try:
            ws.agg_trade(id=1, symbol=SYMBOL.lower(), callback=on_msg)
            ws.mark_price(id=2, symbol=SYMBOL.lower(), speed=1, callback=on_msg)
            print(f"{BLUE}▶️ WS ativo para {SYMBOL}. Aguardando bricks…{RESET}")
        except Exception as e:
            print(f"{RED}❌ Erro abrir WS:{RESET}",e)

        # watchdog
        while True:
            time.sleep(5)
            if time.time()-last>NO_TICK_RESTART_S:
                print(f"{YELLOW}⏱️ Sem tick, reiniciando WS…{RESET}")
                try: ws.stop()
                except: pass
                break
        print(f"{CYAN}⚡ Reabrindo WS…{RESET}")
        time.sleep(1)

if __name__=="__main__":
    run()
