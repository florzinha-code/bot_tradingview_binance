# bot_tradingview_binance_ws.py — COMBAT READY++
# Binance Futures — Renko 550 pts + EMA9/21 + RSI14
# - Warm-up (EMA/RSI estáveis) + reancoragem no preço atual
# - PnL detalhado só nas ordens
# - STOP + reversão no MESMO tijolo
# - Reconexão robusta (watchdog 180s)
# - Keepalive real: ping WS + TCP keepalive
# - Fallback HTTP se WS ficar mudo > 2s
# - Logs limpos + anti-spam + extractor resiliente
# - Callback compatível (1 ou 2 args) + sniffer RAW (até 3 amostras)

import os, time, json, threading, datetime as dt, socket, functools, logging, warnings, traceback
from binance.um_futures import UMFutures
from binance.websocket.um_futures.websocket_client import UMFuturesWebsocketClient

# ── silencia avisos do SDK
warnings.filterwarnings("ignore", category=SyntaxWarning)
for name in ("binance", "binance.websocket", "binance.websocket.websocket_client"):
    logging.getLogger(name).setLevel(logging.ERROR)

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

NO_TICK_RESTART_S = 180
HTTP_FALLBACK_AFTER_S = 2.0
HTTP_FALLBACK_INTERVAL = 0.5
QUIET_RESTART = True
RESTART_SLEEP_S = 0.2
DEBUG_HEARTBEAT = False
HEARTBEAT_EVERY_S = 10

# sniffer: quantas mensagens RAW mostrar quando não extrair preço
RAW_SNIFF_MAX = 3

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
    def __init__(self, n): self.mult=2.0/(n+1.0); self.value=None
    def update(self,x):
        self.value=x if self.value is None else (x-self.value)*self.mult+self.value
        return self.value

class RSI:
    def __init__(self,n): self.n=n; self.up=self.dn=self.prev=None
    def update(self,x):
        if self.prev is None: self.prev=x; return None
        ch=x-self.prev; u=max(ch,0); d=max(-ch,0)
        if self.up is None: self.up,self.dn=u,d
        else:
            self.up=(self.up*(self.n-1)+u)/self.n
            self.dn=(self.dn*(self.n-1)+d)/self.n
        self.prev=x
        rs=self.up/max(self.dn,1e-12)
        return 100.0-100.0/(1.0+rs)

# ---------- Renko ----------
class Renko:
    def __init__(self,box,rev):
        self.box=float(box); self.rev=int(rev)
        self.anchor=None; self.dir=0; self.id=0
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

# ---------- estado ----------
class State:
    def __init__(self):
        self.in_long=False; self.in_short=False
        self.ema1=EMA(EMA_FAST); self.ema2=EMA(EMA_SLOW); self.rsi=RSI(RSI_LEN)
        self.last_entry_brick=None; self.last_entry_side=None
    def upd(self,x): return self.ema1.update(x),self.ema2.update(x),self.rsi.update(x)

# ---------- preço ----------
def extract_px(msg):
    """Extrai preço de vários formatos possíveis (aggTrade, markPrice, payload/data/k)."""
    try:
        if isinstance(msg,str): 
            try: msg=json.loads(msg)
            except: return 0.0
        if isinstance(msg, (list, tuple)) and msg:
            msg = msg[-1]  # pega o último item se vier em lista

        def pick(d):
            if not isinstance(d,dict): return 0.0
            # formatos comuns
            for k in ("p","c","price","ap","mp","P","C"):
                v = d.get(k)
                if v is not None:
                    try: return float(v)
                    except: pass
            # mark price update costuma vir com "p" ou "markPrice"
            for k in ("markPrice","indexPrice","lastPrice"):
                v = d.get(k)
                if v is not None:
                    try: return float(v)
                    except: pass
            # nested
            for k in ("data","payload","k"):
                sub = d.get(k)
                if isinstance(sub,dict):
                    val = pick(sub)
                    if val: return val
            return 0.0

        return pick(msg) or 0.0
    except:
        return 0.0

def calc_qty(p):
    try:
        bal=client.balance()
        usdt=float(next((b.get("balance") for b in bal if b.get("asset")=="USDT"),0.0))
        q=(usdt*QTY_PCT)/max(p,1e-9)
        return max(MIN_QTY,round(q,3))
    except: 
        return MIN_QTY

# ---------- PnL ----------
def show_pnl():
    try:
        d=client.get_position_risk(symbol=SYMBOL)
        if not d: return
        p=d[0]
        pnl=float(p.get("unRealizedProfit",0))
        entry=float(p.get("entryPrice",0))
        amt=float(p.get("positionAmt",0))
        mark=float(p.get("markPrice",0))
        side="LONG" if amt>0 else "SHORT" if amt<0 else "FLAT"
        if entry>0 and amt!=0:
            pct=((mark-entry)/entry*100)*(1 if amt>0 else -1)
            color=GREEN if pnl>=0 else RED
            print(f"{color}💰 PnL {pnl:.2f} USDT | Δ {pct:.3f}% | {side}{RESET}")
    except Exception as e:
        print(f"{YELLOW}⚠️ Erro PnL:{RESET}",e)

# ---------- ordens ----------
def market_order(side,q,reduce=False):
    global client
    try:
        params=dict(symbol=SYMBOL,side=side,type="MARKET",quantity=q)
        if reduce: params["reduceOnly"]="true"
        client.new_order(**params)
        tag=(YELLOW if reduce else GREEN)
        print(f"{tag}✅ {side} qty={q} reduceOnly={reduce}{RESET}")
        time.sleep(0.2); show_pnl(); return True
    except Exception:
        try:
            print(f"{YELLOW}⚠️ Recriando sessão e reenviando…{RESET}")
            client=get_client()
            client.new_order(**params)
            print(f"{GREEN}✅ {side} qty={q} reduceOnly={reduce}{RESET}")
            time.sleep(0.2); show_pnl(); return True
        except Exception as e2:
            msg=str(e2)
            if "-2019" in msg or "Margin is insufficient" in msg:
                print(f"{RED}❌ Ordem recusada: margem insuficiente (-2019).{RESET}")
            else: 
                print(f"{RED}❌ Erro ordem:{RESET}",e2)
            return False

# ---------- lógica ----------
def on_brick(state,close,d,brick_id):
    e1,e2,r=state.upd(close)
    if e1 is None or e2 is None or r is None: return
    verde=(d==1); verm=(d==-1)
    print(f"{MAGENTA}{ts()} | 🧱 {brick_id} {'▲' if verde else '▼'} | close={close:.2f} | EMA9={e1:.2f} EMA21={e2:.2f} | RSI={r:.2f}{RESET}")
    q=calc_qty(close)

    # STOP (permite reversão no mesmo tijolo)
    if state.in_long and verm:
        if market_order("SELL",q,True):
            print(f"{YELLOW}🛑 STOP COMPRA #{brick_id}{RESET}")
            state.in_long=False
    if state.in_short and verde:
        if market_order("BUY",q,True):
            print(f"{YELLOW}🛑 STOP VENDA #{brick_id}{RESET}")
            state.in_short=False

    # ENTRADAS (debounce: evita MESMA direção no MESMO tijolo)
    def dup(s): return state.last_entry_brick==brick_id and state.last_entry_side==s
    if verde and e1>e2 and RSI_WIN_LONG[0]<=r<=RSI_WIN_LONG[1] and not state.in_long and not dup("BUY"):
        if market_order("BUY",q):
            print(f"{GREEN}🚀 COMPRA #{brick_id}{RESET}")
            state.in_long=True; state.in_short=False; state.last_entry_brick=brick_id; state.last_entry_side="BUY"
    if verm and e1<e2 and RSI_WIN_SHORT[0]<=r<=RSI_WIN_SHORT[1] and not state.in_short and not dup("SELL"):
        if market_order("SELL",q):
            print(f"{CYAN}🔻 VENDA #{brick_id}{RESET}")
            state.in_short=True; state.in_long=False; state.last_entry_brick=brick_id; state.last_entry_side="SELL"

# ---------- warmup ----------
def warmup(state,renko):
    try:
        kl=client.klines(symbol=SYMBOL,interval="1m",limit=500)
        for k in kl:
            c=float(k[4])
            for close,d,i in renko.feed(c): state.upd(close)
        px=float(client.ticker_price(symbol=SYMBOL)['price'])
        renko.reanchor(px)
        print(f"{CYAN}🧰 Warm-up concluído e reancorado.{RESET}")
    except Exception as e: 
        print(f"{YELLOW}⚠️ Warm-up:{RESET}",e)

# ---------- loop principal ----------
def run():
    setup_symbol()
    while True:
        state=State(); renko=Renko(BOX_POINTS,REV_BOXES); warmup(state,renko)
        ws=UMFuturesWebsocketClient()
        last_tick=time.time()
        stop_ev=threading.Event()
        raw_sniffed=[0]  # contador mutável simples

        # callback compatível: algumas versões chamam (message), outras (id, message)
        def on_msg(*args):
            nonlocal last_tick
            try:
                msg = args[-1]  # pega SEMPRE o último argumento como mensagem
                p   = extract_px(msg)
                if p>0:
                    last_tick=time.time()
                    for c,d,i in renko.feed(p): on_brick(state,c,d,i)
                else:
                    # sniffer: mostra até RAW_SNIFF_MAX amostras pra gente ver formato real
                    if raw_sniffed[0] < RAW_SNIFF_MAX:
                        raw_sniffed[0]+=1
                        try:
                            preview = msg if isinstance(msg,str) else json.dumps(msg)[:300]
                        except Exception:
                            preview = str(msg)[:300]
                        print(f"{YELLOW}🧩 RAW WS message (sem preço) [{raw_sniffed[0]}]:{RESET} {preview}")
            except Exception as e: 
                print(f"{YELLOW}⚠️ on_msg:{RESET}", e)

        def keepalive():
            while not stop_ev.is_set():
                try:
                    ws.ping()
                    if hasattr(ws,"_socket") and ws._socket:
                        try:
                            ws._socket.setsockopt(socket.SOL_SOCKET,socket.SO_KEEPALIVE,1)
                        except Exception: pass
                except Exception:
                    stop_ev.set(); break
                time.sleep(45)

        def http_fallback():
            while not stop_ev.is_set():
                time.sleep(HTTP_FALLBACK_INTERVAL)
                if time.time()-last_tick<HTTP_FALLBACK_AFTER_S: continue
                try:
                    p=float(client.ticker_price(symbol=SYMBOL)['price'])
                    for c,d,i in renko.feed(p): on_brick(state,c,d,i)
                except: pass

        def heartbeat():
            last_print=0.0; last_price=0.0
            while not stop_ev.is_set():
                now=time.time()
                if DEBUG_HEARTBEAT and now-last_print>=HEARTBEAT_EVERY_S:
                    age=now-last_tick
                    try: last_price=float(client.ticker_price(symbol=SYMBOL)['price'])
                    except Exception: pass
                    print(f"{GRAY}⏳ tick_age={age:.1f}s | px≈{last_price:.2f}{RESET}")
                    last_print=now
                time.sleep(1)

        try:
            # algumas versões exigem ws.start() antes das inscrições
            try: ws.start()
            except Exception: pass

            ws.agg_trade(id=1, symbol=SYMBOL.lower(), callback=on_msg)
            try:
                ws.mark_price(id=2, symbol=SYMBOL.lower(), speed=1, callback=on_msg)
            except TypeError:
                ws.mark_price(id=2, symbol=SYMBOL.lower(), callback=on_msg)

            threading.Thread(target=keepalive,daemon=True).start()
            threading.Thread(target=http_fallback,daemon=True).start()
            threading.Thread(target=heartbeat,daemon=True).start()
            print(f"{BLUE}▶️ WS ativo para {SYMBOL}. Aguardando bricks…{RESET}")
        except Exception as e: 
            print(f"{RED}❌ Erro ao abrir WS:{RESET}", e)

        try:
            while True:
                time.sleep(5)
                if time.time()-last_tick>NO_TICK_RESTART_S:
                    if not QUIET_RESTART:
                        print(f"{YELLOW}⏱️ Sem tick, reiniciando…{RESET}")
                    stop_ev.set()
                    try: ws.stop()
                    except Exception: pass
                    break
        except Exception:
            stop_ev.set()
            try: ws.stop()
            except Exception: pass

        if not QUIET_RESTART:
            print(f"{CYAN}⚡ Reabrindo WS…{RESET}")
        time.sleep(RESTART_SLEEP_S)

if __name__=="__main__":
    run()
