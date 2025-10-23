# bot_tradingview_binance_ws.py
# Binance Futures WebSocket — Renko 550 pts + EMA9/21 + RSI14 + reversão = stop
# ✅ Warm-up de histórico (EMA/RSI estáveis) + reancoragem no preço atual
# ✅ PnL detalhado (USDT + %)
# ✅ Reconexão instantânea + watchdog
# ✅ Sem “ping-ping” no log (sem heartbeat manual)
# ✅ Debounce direcional (impede duas ENTRADAS iguais no mesmo brick)
# ✅ STOP + reversão permitidos no mesmo brick
# ✅ Auto-relogin (recria UMFutures em erro)
# ✅ Streams (aggTrade + markPrice quando disponível) + Fallback HTTP discreto
# ✅ Logs enxutos (1 linha por brick + ordens)

import os, time, json, traceback, functools, threading, datetime as dt
from binance.um_futures import UMFutures
from binance.websocket.um_futures.websocket_client import UMFuturesWebsocketClient

# ---------- cores ----------
RESET="\033[0m"; RED="\033[91m"; GREEN="\033[92m"; YELLOW="\033[93m"
BLUE="\033[94m"; MAGENTA="\033[95m"; CYAN="\033[96m"; GRAY="\033[90m"

# ---------- print flush ----------
print = functools.partial(print, flush=True)

# ---------- config ----------
SYMBOL       = "BTCUSDT"
LEVERAGE     = 1
MARGIN_TYPE  = "CROSSED"
QTY_PCT      = 0.85
BOX_POINTS   = 550.0
REV_BOXES    = 2
EMA_FAST     = 9
EMA_SLOW     = 21
RSI_LEN      = 14
RSI_WIN_LONG = (40.0, 65.0)
RSI_WIN_SHORT= (35.0, 60.0)
MIN_QTY      = 0.001

# Robustez (sem flood)
NO_TICK_RESTART_S       = 120      # watchdog do WS (se ficar mudo por >120s, reconecta)
HTTP_FALLBACK           = True
HTTP_FALLBACK_AFTER_S   = 2.0      # se WS ficar >2s sem tick, ativa HTTP feeder
HTTP_FALLBACK_INTERVAL  = 0.6      # frequência do feeder HTTP (discreto)

# Warm-up
WARMUP_ENABLED          = True
WARMUP_INTERVAL         = "1m"
WARMUP_LIMIT            = 1000
REANCHOR_AFTER_WARMUP   = True     # reancora no preço atual ao final do warm-up

API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

# ---------- helpers ----------
def ts():
    return dt.datetime.utcnow().strftime("%H:%M:%S.%f")[:-3] + "Z"

def get_client():
    return UMFutures(key=API_KEY, secret=API_SECRET)

client = get_client()

def setup_symbol():
    global client
    try:
        client.change_margin_type(symbol=SYMBOL, marginType=MARGIN_TYPE)
        print(f"{GREEN}✅ Modo de margem: {MARGIN_TYPE}{RESET}")
    except Exception as e:
        if "No need to change margin type" in str(e):
            print(f"{CYAN}ℹ️ Margem já configurada.{RESET}")
        else:
            print(f"{YELLOW}⚠️ change_margin_type:{RESET}", e)
    try:
        client.change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
        print(f"{GREEN}✅ Alavancagem: {LEVERAGE}x{RESET}")
    except Exception as e:
        print(f"{YELLOW}⚠️ change_leverage:{RESET}", e)

# ---------- EMA ----------
class EMA:
    def __init__(self, length:int):
        self.mult = 2.0/(length+1.0); self.value=None
    def update(self, x:float):
        self.value = x if self.value is None else (x-self.value)*self.mult + self.value
        return self.value

# ---------- RSI (Wilder) ----------
class RSI_Wilder:
    def __init__(self, length:int):
        self.len=length; self.avgU=None; self.avgD=None; self.prev=None
    def update(self, x:float):
        if self.prev is None:
            self.prev = x; return None
        ch = x - self.prev
        up = ch if ch>0 else 0.0
        dn = -ch if ch<0 else 0.0
        if self.avgU is None:
            self.avgU, self.avgD = up, dn
        else:
            self.avgU = (self.avgU*(self.len-1)+up)/self.len
            self.avgD = (self.avgD*(self.len-1)+dn)/self.len
        self.prev = x
        rs = self.avgU/max(self.avgD,1e-12)
        return 100.0 - 100.0/(1.0+rs)

# ---------- Renko ----------
class RenkoEngine:
    def __init__(self, box_points:float, rev_boxes:int):
        self.box=float(box_points); self.rev=int(rev_boxes)
        self.anchor=None; self.dir=0; self.brick_id=0
    def reanchor(self, px:float):
        self.anchor = px; self.dir = 0
    def feed_price(self, px:float):
        created=[]
        if self.anchor is None:
            self.anchor = px
            return created
        up_th=self.anchor+self.box; down_th=self.anchor-self.box
        while px >= up_th:
            self.anchor += self.box if self.dir!=-1 else self.box*self.rev
            self.dir=1; self.brick_id+=1
            created.append((self.anchor,self.dir,self.brick_id))
            up_th=self.anchor+self.box; down_th=self.anchor-self.box
        while px <= down_th:
            self.anchor -= self.box if self.dir!=1 else self.box*self.rev
            self.dir=-1; self.brick_id+=1
            created.append((self.anchor,self.dir,self.brick_id))
            up_th=self.anchor+self.box; down_th=self.anchor-self.box
        return created

# ---------- estado ----------
class StrategyState:
    def __init__(self):
        self.in_long=False; self.in_short=False
        self.ema_fast=EMA(EMA_FAST); self.ema_slow=EMA(EMA_SLOW)
        self.rsi=RSI_Wilder(RSI_LEN)
        # debounce direcional (apenas ENTRADAS; STOP não marca → permite STOP + reversão no mesmo brick)
        self.last_brick_id=None
        self.last_side=None      # "BUY" / "SELL"
    def update_indics(self, x:float):
        return self.ema_fast.update(x), self.ema_slow.update(x), self.rsi.update(x)

# ---------- quantidade ----------
def get_qty(price:float):
    global client
    try:
        bal = client.balance()
    except Exception:
        try:
            print(f"{YELLOW}⚠️ Recriando sessão (balance falhou)…{RESET}")
            client = get_client()
            bal = client.balance()
        except Exception as e2:
            print(f"{RED}❌ Falha ao obter saldo:{RESET}", e2)
            return MIN_QTY, 0.0
    usdt = next((float(b.get("balance",0)) for b in bal if b.get("asset")=="USDT"), 0.0)
    qty  = round(max(MIN_QTY, (usdt*QTY_PCT)/max(price,1e-9)), 3)
    return qty, usdt

# ---------- PnL ----------
def show_pnl():
    global client
    try:
        data = client.get_position_risk(symbol=SYMBOL)
        if data:
            pos = data[0]
            pnl=float(pos.get("unRealizedProfit",0)); entry=float(pos.get("entryPrice",0))
            amt=float(pos.get("positionAmt",0)); side="LONG" if amt>0 else "SHORT" if amt<0 else "FLAT"
            if amt!=0 and entry>0:
                mark=float(pos.get("markPrice",0))
                pct=((mark-entry)/entry*100)*(1 if amt>0 else -1)
            else:
                pct=0.0
            color = GREEN if pnl>=0 else RED
            print(f"{color}💰 PnL: {pnl:.2f} USDT | Δ={pct:.3f}% | Entrada: {entry:.2f} | Qty: {amt:.4f} | {side}{RESET}")
    except Exception as e:
        print(f"{YELLOW}⚠️ Erro ao obter PnL:{RESET}", e)

# ---------- posição snapshot ----------
def _position_snapshot():
    try:
        d = client.get_position_risk(symbol=SYMBOL)
        if d:
            p=d[0]; return float(p.get("positionAmt",0.0))
    except Exception:
        pass
    return 0.0

# ---------- ordens (com confirmação e relogin) ----------
def market_order(side:str, qty:float, reduce_only:bool=False):
    """
    Envia ordem; trata erro -2019 (margem) e confirma alteração de posição.
    Retorna True somente se a posição mudar de fato (para entradas) ou reduzir (para STOP).
    """
    global client
    params=dict(symbol=SYMBOL, side=side, type="MARKET", quantity=qty)
    if reduce_only: params["reduceOnly"]="true"

    before_amt = _position_snapshot()
    try:
        resp = client.new_order(**params)
        if isinstance(resp, dict) and resp.get("code"):
            print(f"{RED}❌ Binance recusou: {resp.get('code')} {resp.get('msg')}{RESET}")
            return False
    except Exception:
        try:
            print(f"{YELLOW}⚠️ Sessão pode ter expirado. Recriando client e reenviando…{RESET}")
            client = get_client()
            resp = client.new_order(**params)
            if isinstance(resp, dict) and resp.get("code"):
                print(f"{RED}❌ Binance recusou: {resp.get('code')} {resp.get('msg')}{RESET}")
                return False
        except Exception as e2:
            msg = str(e2)
            if "Margin is insufficient" in msg or "-2019" in msg:
                print(f"{RED}❌ Erro ao enviar ordem: Margin is insufficient (-2019).{RESET}")
            else:
                print(f"{RED}❌ Erro ao enviar ordem:{RESET}", e2)
            return False

    # confirmação simples
    for _ in range(6):
        time.sleep(0.15)
        after_amt = _position_snapshot()
        if reduce_only:
            if abs(after_amt) <= max(abs(before_amt) - 1e-9, 0.0) or (before_amt!=0 and after_amt==0):
                print(f"{GREEN}✅ Ordem {side} qty={qty} reduceOnly={reduce_only}{RESET}")
                show_pnl()
                return True
        else:
            if abs(after_amt - before_amt) > 1e-9:
                print(f"{GREEN}✅ Ordem {side} qty={qty} reduceOnly={reduce_only}{RESET}")
                show_pnl()
                return True

    print(f"{YELLOW}⚠️ Ordem enviada mas não confirmou alteração de posição.{RESET}")
    return False

# ---------- extração de preço (robusta) ----------
def _first_number(*vals):
    for v in vals:
        try:
            if v is None: continue
            return float(v)
        except Exception:
            continue
    return 0.0

def extract_price(message):
    try:
        if isinstance(message, str):
            message = json.loads(message)
        if isinstance(message, (list, tuple)) and message:
            message = message[0]
        if isinstance(message, dict):
            v = _first_number(message.get("p"), message.get("c"), message.get("price"))
            if v: return v
            for key in ("data","payload"):
                sub = message.get(key)
                if isinstance(sub, dict):
                    v = _first_number(sub.get("p"), sub.get("c"), sub.get("price"))
                    if v: return v
                    k = sub.get("k")
                    if isinstance(k, dict):
                        v = _first_number(k.get("c"), k.get("p"), k.get("price"))
                        if v: return v
            k = message.get("k")
            if isinstance(k, dict):
                v = _first_number(k.get("c"), k.get("p"), k.get("price"))
                if v: return v
        return 0.0
    except Exception:
        return 0.0

# ---------- lógica ----------
def apply_logic_on_brick(state:StrategyState, brick_close:float, d:int, brick_id:int, source:str):
    e1,e2,rsi = state.update_indics(brick_close)
    if e1 is None or e2 is None or rsi is None:
        return

    try:
        price = float(client.ticker_price(symbol=SYMBOL)['price'])
    except Exception:
        price = brick_close

    qty,_ = get_qty(price)
    renkoVerde   = (d== 1)
    renkoVermelh = (d==-1)

    # 1 linha por brick + origem
    print(f"{MAGENTA}{'WS' if source=='WS' else 'HTTP'} {ts()} | "
          f"🧱 {brick_id} {'▲' if renkoVerde else '▼'} | close={brick_close:.2f} "
          f"| EMA9={e1:.2f} EMA21={e2:.2f} | RSI={rsi:.2f}{RESET}")

    def dup_block(side:str)->bool:
        return (state.last_brick_id == brick_id) and (state.last_side == side)

    # ---- STOPs (não gravam last_side → permitem reversão no mesmo brick) ----
    did_stop_long = False
    did_stop_short= False

    if state.in_long and renkoVermelh:
        if market_order("SELL", qty, True):
            print(f"{YELLOW}🛑 STOP COMPRA #{brick_id} @ {brick_close:.2f}{RESET}")
            state.in_long = False
            did_stop_long = True

    if state.in_short and renkoVerde:
        if market_order("BUY", qty, True):
            print(f"{YELLOW}🛑 STOP VENDA  #{brick_id} @ {brick_close:.2f}{RESET}")
            state.in_short = False
            did_stop_short = True

    # ---- Entradas (permite reversão no MESMO brick se as condições baterem) ----
    long_ok  = renkoVerde   and (e1 > e2) and (RSI_WIN_LONG[0]  <= rsi <= RSI_WIN_LONG[1]) and not state.in_long
    short_ok = renkoVermelh and (e1 < e2) and (RSI_WIN_SHORT[0] <= rsi <= RSI_WIN_SHORT[1]) and not state.in_short

    # Se parou LONG e virou vermelho, pode abrir SHORT neste mesmo brick (e vice-versa)
    if long_ok and not dup_block("BUY"):
        if market_order("BUY", qty):
            print(f"{GREEN}🚀 COMPRA #{brick_id} @ {brick_close:.2f}{RESET}")
            state.in_long, state.in_short = True, False
            state.last_brick_id, state.last_side = brick_id, "BUY"

    if short_ok and not dup_block("SELL"):
        if market_order("SELL", qty):
            print(f"{CYAN}🔻 VENDA  #{brick_id} @ {brick_close:.2f}{RESET}")
            state.in_short, state.in_long = True, False
            state.last_brick_id, state.last_side = brick_id, "SELL"

# ---------- warm-up ----------
def warmup_state(state:StrategyState, renko:RenkoEngine):
    if not WARMUP_ENABLED:
        return
    try:
        kl = client.klines(symbol=SYMBOL, interval=WARMUP_INTERVAL, limit=WARMUP_LIMIT)
        if not kl:
            print(f"{YELLOW}⚠️ Warm-up: sem klines retornados.{RESET}")
            return
        for row in kl:
            close = float(row[4])  # kline[4] = close
            for brick_close, d, brick_id in renko.feed_price(close):
                state.update_indics(brick_close)
        if REANCHOR_AFTER_WARMUP:
            try:
                px = float(client.ticker_price(symbol=SYMBOL)['price'])
                renko.reanchor(px)
            except Exception:
                pass
        formed = renko.brick_id
        print(f"{CYAN}🧰 Warm-up concluído: {formed} bricks (EMA/RSI estáveis) | anchor reancorado no preço atual{RESET}")
    except Exception as e:
        print(f"{YELLOW}⚠️ Warm-up falhou:{RESET}", e)

# ---------- loop WS + fallback (sem heartbeat manual) ----------
def run_ws():
    setup_symbol()
    while True:
        state = StrategyState()
        renko = RenkoEngine(BOX_POINTS, REV_BOXES)

        # warm-up Renko/indicadores com histórico recente
        warmup_state(state, renko)

        ws = UMFuturesWebsocketClient()  # usa ping/pong internos do client (sem logs)
        last_tick_ts = time.time()

        # feeder via WS (aggTrade + markPrice se disponível)
        def on_msg(_, message):
            nonlocal last_tick_ts
            try:
                px = extract_price(message)
                if px > 0:
                    last_tick_ts = time.time()
                    for brick_close, d, brick_id in renko.feed_price(px):
                        apply_logic_on_brick(state, brick_close, d, brick_id, source="WS")
            except Exception as e:
                print(f"{YELLOW}⚠️ on_msg error:{RESET}", e)
                traceback.print_exc()

        # fallback HTTP (entra somente em silêncio real do WS)
        stop_flag = {"stop": False}
        def http_feeder():
            while not stop_flag["stop"]:
                time.sleep(HTTP_FALLBACK_INTERVAL)
                if not HTTP_FALLBACK:
                    continue
                if time.time() - last_tick_ts < HTTP_FALLBACK_AFTER_S:
                    continue  # WS está saudável
                try:
                    px = float(client.ticker_price(symbol=SYMBOL)['price'])
                    for brick_close, d, brick_id in renko.feed_price(px):
                        apply_logic_on_brick(state, brick_close, d, brick_id, source="HTTP")
                except Exception:
                    pass

        start_ts = time.time()
        try:
            try:
                ws.agg_trade(symbol=SYMBOL.lower(), id=1, callback=on_msg)
            except TypeError:
                ws.agg_trade(symbol=SYMBOL.lower(), stream_id="t", callback=on_msg)
            try:
                ws.mark_price(symbol=SYMBOL.lower(), id=2, callback=on_msg)
            except Exception:
                pass
            threading.Thread(target=http_feeder, daemon=True).start()
            print(f"{BLUE}▶️ WS ativo para {SYMBOL}. Aguardando bricks…{RESET}")
        except Exception as e:
            print(f"{RED}❌ Erro ao abrir WS:{RESET}", e)

        # watchdog de silêncio (sem prints periódicos)
        try:
            while True:
                time.sleep(5)
                silence = time.time() - last_tick_ts
                if silence > NO_TICK_RESTART_S:
                    print(f"{YELLOW}⏱️ {silence:.1f}s sem tick WS. Reiniciando WS…{RESET}")
                    try: ws.stop()
                    except Exception: pass
                    break
        except Exception as e:
            print(f"{YELLOW}⚠️ Loop principal caiu:{RESET}", e)
            traceback.print_exc()
            try: ws.stop()
            except Exception: pass

        stop_flag["stop"] = True
        print(f"{CYAN}⚡ Reabrindo WS agora… (instantâneo){RESET}")
        time.sleep(0.1)

if __name__=="__main__":
    run_ws()
