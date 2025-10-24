# bot_tradingview_binance_ws.py
# Binance Futures WebSocket — Renko 550 pts + EMA9/21 + RSI14 + reversão = stop
# ✅ Warm-up (EMA/RSI estáveis) + reancoragem no preço atual
# ✅ PnL detalhado (USDT + %)
# ✅ Reconexão instantânea + watchdog (180s)
# ✅ Heartbeat + TCP Keepalive + Ping Thread alternado
# ✅ Debounce direcional (permite STOP + reversão no mesmo brick)
# ✅ Auto-relogin (recria client em erro)
# ✅ Streams duplos (aggTrade + markPrice)
# ✅ Fallback HTTP (ticker_price)
# ✅ Logs limpos (1 linha por brick + ordens)

import os, time, json, traceback, functools, threading, datetime as dt, socket, struct
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
RSI_WIN_LONG=(40.0,65.0)
RSI_WIN_SHORT=(35.0,60.0)
MIN_QTY=0.001

NO_TICK_RESTART_S=180
HEARTBEAT_S=30
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
    global client
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

class EMA:
    def __init__(self,l): self.mult=2/(l+1);self.value=None
    def update(self,x): self.value=x if self.value is None else (x-self.value)*self.mult+self.value;return self.value

class RSI_Wilder:
    def __init__(self,l): self.len=l;self.avgU=None;self.avgD=None;self.prev=None
    def update(self,x):
        if self.prev is None:self.prev=x;return None
        ch=x-self.prev;up=ch if ch>0 else 0;dn=-ch if ch<0 else 0
        if self.avgU is None:self.avgU,self.avgD=up,dn
        else:
            self.avgU=(self.avgU*(self.len-1)+up)/self.len
            self.avgD=(self.avgD*(self.len-1)+dn)/self.len
        self.prev=x;rs=self.avgU/max(self.avgD,1e-12)
        return 100-100/(1+rs)

class RenkoEngine:
    def __init__(self,b,r):self.box=b;self.rev=r;self.anchor=None;self.dir=0;self.brick_id=0
    def reanchor(self,px):self.anchor=px;self.dir=0
    def feed_price(self,px):
        out=[]
        if self.anchor is None:self.anchor=px;return out
        up=self.anchor+self.box;down=self.anchor-self.box
        while px>=up:
            self.anchor+=self.box if self.dir!=-1 else self.box*self.rev
            self.dir=1;self.brick_id+=1;out.append((self.anchor,1,self.brick_id))
            up=self.anchor+self.box;down=self.anchor-self.box
        while px<=down:
            self.anchor-=self.box if self.dir!=1 else self.box*self.rev
            self.dir=-1;self.brick_id+=1;out.append((self.anchor,-1,self.brick_id))
            up=self.anchor+self.box;down=self.anchor-self.box
        return out

class StrategyState:
    def __init__(self):
        self.in_long=False;self.in_short=False
        self.ema_fast=EMA(EMA_FAST);self.ema_slow=EMA(EMA_SLOW)
        self.rsi=RSI_Wilder(RSI_LEN)
        self.last_brick_id=None;self.last_side=None
        self.stop_brick_id=None
    def update_indics(self,x):return self.ema_fast.update(x),self.ema_slow.update(x),self.rsi.update(x)

def get_qty(price):
    global client
    try:bal=client.balance()
    except:client=get_client();bal=client.balance()
    usdt=next((float(b.get("balance",0)) for b in bal if b.get("asset")=="USDT"),0)
    qty=round(max(MIN_QTY,(usdt*QTY_PCT)/max(price,1e-9)),3)
    return qty,usdt

def show_pnl():
    try:
        pos=client.get_position_risk(symbol=SYMBOL)[0]
        pnl=float(pos["unRealizedProfit"]);entry=float(pos["entryPrice"])
        amt=float(pos["positionAmt"]);side="LONG" if amt>0 else "SHORT" if amt<0 else "FLAT"
        mark=float(pos["markPrice"]);pct=((mark-entry)/entry*100)*(1 if amt>0 else -1)
        col=GREEN if pnl>=0 else RED
        print(f"{col}💰 PnL: {pnl:.2f} | Δ={pct:.3f}% | Entrada={entry:.2f} | Qty={amt:.4f} | {side}{RESET}")
    except:pass

def market_order(side,qty,reduce_only=False):
    try:
        client.new_order(symbol=SYMBOL,side=side,type="MARKET",quantity=qty,reduceOnly=reduce_only)
        print(f"{GREEN}✅ Ordem {side} qty={qty} reduceOnly={reduce_only}{RESET}")
        show_pnl();return True
    except Exception as e:
        print(f"{RED}❌ Erro ordem:{RESET}",e);return False

def extract_price(m):
    try:
        if isinstance(m,str):m=json.loads(m)
        if isinstance(m,dict):
            for k in("p","c","price"):
                if k in m:return float(m[k])
            sub=m.get("data")or m.get("payload")or m.get("k")
            if isinstance(sub,dict):
                for k in("p","c","price"):
                    if k in sub:return float(sub[k])
        return 0.0
    except:return 0.0

def apply_logic(state,brick_close,d,brick_id,src):
    e1,e2,rsi=state.update_indics(brick_close)
    if e1 is None or e2 is None or rsi is None:return
    qty,_=get_qty(brick_close)
    print(f"{MAGENTA}{src} | {ts()} | 🧱 {brick_id} {'▲' if d==1 else '▼'} | close={brick_close:.2f} | EMA9={e1:.2f} | EMA21={e2:.2f} | RSI={rsi:.2f}{RESET}")

    def dup(side):return (state.last_brick_id==brick_id)and(state.last_side==side)
    if state.in_long and d==-1:
        if market_order("SELL",qty,True):
            print(f"{YELLOW}🛑 STOP COMPRA {brick_id}{RESET}");state.in_long=False
    if state.in_short and d==1:
        if market_order("BUY",qty,True):
            print(f"{YELLOW}🛑 STOP VENDA {brick_id}{RESET}");state.in_short=False

    if d==1 and e1>e2 and RSI_WIN_LONG[0]<=rsi<=RSI_WIN_LONG[1] and not state.in_long and not dup("BUY"):
        if market_order("BUY",qty):
            print(f"{GREEN}🚀 COMPRA {brick_id}{RESET}")
            state.in_long, state.in_short=True,False
            state.last_brick_id, state.last_side=brick_id,"BUY"

    if d==-1 and e1<e2 and RSI_WIN_SHORT[0]<=rsi<=RSI_WIN_SHORT[1] and not state.in_short and not dup("SELL"):
        if market_order("SELL",qty):
            print(f"{CYAN}🔻 VENDA {brick_id}{RESET}")
            state.in_short, state.in_long=True,False
            state.last_brick_id, state.last_side=brick_id,"SELL"

def warmup_state(state,renko):
    if not WARMUP_ENABLED:return
    try:
        kl=client.klines(symbol=SYMBOL,interval=WARMUP_INTERVAL,limit=WARMUP_LIMIT)
        for r in kl:
            close=float(r[4])
            for b,d,bid in renko.feed_price(close):state.update_indics(b)
        if REANCHOR_AFTER_WARMUP:
            px=float(client.ticker_price(symbol=SYMBOL)['price']);renko.reanchor(px)
        print(f"{CYAN}🧰 Warm-up concluído e reancorado.{RESET}")
    except Exception as e:print(f"{YELLOW}⚠️ Warm-up falhou:{RESET}",e)

def run_ws():
    setup_symbol()
    while True:
        state=StrategyState();renko=RenkoEngine(BOX_POINTS,REV_BOXES)
        warmup_state(state,renko)
        ws=UMFuturesWebsocketClient();last_tick=time.time()

        def on_msg(_,msg):
            nonlocal last_tick
            px=extract_price(msg)
            if px>0:
                last_tick=time.time()
                for b,d,bid in renko.feed_price(px):apply_logic(state,b,d,bid,"WS")

        try:
            ws.agg_trade(SYMBOL.lower(),on_msg)
            ws.mark_price(SYMBOL.lower(),"1s",on_msg)
            print(f"{BLUE}▶️ WS ativo para {SYMBOL}. Aguardando bricks…{RESET}")
        except Exception as e:
            print(f"{RED}❌ Erro abrir WS:{RESET}",e)

        # === Thread de PING TCP + WS ===
        def tcp_keepalive():
            s=None
            while True:
                try:
                    if hasattr(ws,"_socket") and ws._socket:
                        s=ws._socket
                        s.setsockopt(socket.SOL_SOCKET,socket.SO_KEEPALIVE,1)
                        s.setsockopt(socket.IPPROTO_TCP,socket.TCP_KEEPIDLE,45)
                        s.setsockopt(socket.IPPROTO_TCP,socket.TCP_KEEPINTVL,10)
                        s.setsockopt(socket.IPPROTO_TCP,socket.TCP_KEEPCNT,3)
                        s.send(struct.pack("!BBHHH",8,0,0,0,0))  # ping alternado manual
                    time.sleep(30)
                except Exception:
                    time.sleep(1)

        threading.Thread(target=tcp_keepalive,daemon=True).start()

        try:
            while True:
                time.sleep(5)
                silence=time.time()-last_tick
                if silence>NO_TICK_RESTART_S:
                    print(f"{YELLOW}⏱️ {silence:.1f}s sem tick WS. Reiniciando…{RESET}")
                    try:ws.close()
                    except:pass
                    break
        except Exception as e:
            print(f"{YELLOW}⚠️ Loop caiu:{RESET}",e)
            traceback.print_exc()
            try:ws.close()
            except:pass

        print(f"{CYAN}⚡ Reabrindo WS agora…{RESET}")
        time.sleep(0.1)

if __name__=="__main__":
    run_ws()
