# bot_tradingview_binance_ws.py
# Binance Futures WebSocket — Renko 550 pts + EMA9/21 + RSI14 + reversão = stop
# ✅ Warm-up de histórico (alinha com TradingView)
# ✅ PnL detalhado (USDT + %)
# ✅ Reconexão instantânea + watchdog
# ✅ Heartbeat 30s (silencioso por padrão)
# ✅ Debounce direcional (permite STOP + reversão no mesmo brick)
# ✅ Auto-relogin (recria UMFutures em erro)
# ✅ Streams duplos (aggTrade + markPrice)
# ✅ Fallback HTTP (ticker_price) quando WS fica silencioso
# ✅ Logs enxutos (1 linha por brick + ordens)
# ✅ TELEMETRIA: loga pacotes WS (full-debug), com amostra crua do JSON

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

# Robustez
NO_TICK_RESTART_S       = 90       # watchdog do WS
HEARTBEAT_S             = 30       # ping silencioso
SILENT_HEARTBEAT        = True

# Fallback HTTP
HTTP_FALLBACK            = True
HTTP_FALLBACK_AFTER_S    = 2.0     # se WS ficar >2s sem tick, ativa HTTP feeder
HTTP_FALLBACK_INTERVAL_S = 0.5

# Warm-up
WARMUP_ENABLED     = True
WARMUP_INTERVAL    = "1m"
WARMUP_LIMIT       = 1000
WARMUP_LOG_EVERY   = 0           # 0 = não loga cada passo

# Telemetria
TELEMETRY_FULL       = True      # liga/desliga logs de pacote WS
TELEMETRY_RAW_PREVIEW= 220       # chars de JSON bruto pra amostra
LOG_WS_SOURCE_TAG    = True      # mostra de qual stream veio (aggTrade/markPrice)

API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

# ---------- helpers ----------
def ts():
    return dt.datetime.utcnow().strftime("%H:%M:%S.%f")[:-3] + "Z"

def preview(obj, limit=TELEMETRY_RAW_PREVIEW):
    try:
        raw = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    except Exception:
        raw = str(obj)
    return (raw[:limit] + "…") if len(raw) > limit else raw

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
        # debounce direcional (apenas ENTRADAS)
        self.last_brick_id=None
        self.last_side=None  # "BUY" / "SELL"
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

# ---------- ordens ----------
def market_order(side:str, qty:float, reduce_only:bool=False):
    global client
    params=dict(symbol=SYMBOL, side=side, type="MARKET", quantity=qty)
    if reduce_only: params["reduceOnly"]="true"
    try:
        client.new_order(**params)
    except Exception:
        try:
            print(f"{YELLOW}⚠️ Sessão pode ter expirado. Recriando client e reenviando…{RESET}")
            client = get_client()
            client.new_order(**params)
        except Exception as e2:
            print(f"{RED}❌ Erro ao enviar ordem:{RESET}", e2)
            return False
    print(f"{GREEN}✅ Ordem {side} qty={qty} reduceOnly={reduce_only}{RESET}")
    time.sleep(0.2); show_pnl()
    return True

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

    # 1 linha por brick + origem do tick
    print(f"{MAGENTA}{'🛰️WS' if source=='WS' else '🌐HTTP'} | "
          f"{ts()} | 🧱 {brick_id} {'▲' if renkoVerde else '▼'} | close={brick_close:.2f} "
          f"| EMA9={e1:.2f} EMA21={e2:.2f} | RSI={rsi:.2f}{RESET}")

    def dup_block(side:str)->bool:
        return (state.last_brick_id == brick_id) and (state.last_side == side)

    # STOPs (não marcam last_side → permitem reversão no mesmo brick)
    if state.in_long and renkoVermelh and market_order("SELL", qty, True):
        print(f"{YELLOW}🛑 STOP COMPRA #{brick_id} @ {brick_close:.2f}{RESET}")
        state.in_long = False
        state.last_brick_id = brick_id

    if state.in_short and renkoVerde and market_order("BUY", qty, True):
        print(f"{YELLOW}🛑 STOP VENDA  #{brick_id} @ {brick_close:.2f}{RESET}")
        state.in_short = False
        state.last_brick_id = brick_id

    # Entradas
    long_ok  = renkoVerde   and (e1 > e2) and (RSI_WIN_LONG[0]  <= rsi <= RSI_WIN_LONG[1]) and not state.in_long
    short_ok = renkoVermelh and (e1 < e2) and (RSI_WIN_SHORT[0] <= rsi <= RSI_WIN_SHORT[1]) and not state.in_short

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
        bricks_before = renko.brick_id
        for i, row in enumerate(kl):
            close = float(row[4])  # kline[4] = close
            for brick_close, d, brick_id in renko.feed_price(close):
                e1,e2,rsi = state.update_indics(brick_close)
                if WARMUP_LOG_EVERY and brick_id % WARMUP_LOG_EVERY == 0:
                    print(f"{GRAY}↺ Warm-up: brick {brick_id} close={brick_close:.2f} | EMA9={e1:.2f} EMA21={e2:.2f}{RESET}")
        formed = renko.brick_id - bricks_before
        anchor = renko.anchor
        print(f"{CYAN}🧰 Warm-up concluído: {formed} bricks formados | anchor={anchor:.2f} | base={WARMUP_INTERVAL} x {WARMUP_LIMIT}{RESET}")
    except Exception as e:
        print(f"{YELLOW}⚠️ Warm-up falhou:{RESET}", e)

# ---------- loop WS + fallback + TELEMETRIA ----------
def run_ws():
    setup_symbol()
    while True:
        state = StrategyState()
        renko = RenkoEngine(BOX_POINTS, REV_BOXES)

        # warm-up Renko/indicadores com histórico recente
        warmup_state(state, renko)

        ws = UMFuturesWebsocketClient()
        last_tick_ts = time.time()
        ws_msg_count = {"total": 0}

        # feeder via WS (com telemetria)
        def on_msg(_, message):
            nonlocal last_tick_ts
            try:
                ws_msg_count["total"] += 1
                if TELEMETRY_FULL:
                    # Mostra origem quando possível
                    origin = "?"
                    try:
                        if isinstance(message, str):
                            m = json.loads(message)
                        else:
                            m = message
                        if isinstance(m, dict):
                            if m.get("e") == "aggTrade" or "a" in m and "p" in m:
                                origin = "aggTrade"
                            elif m.get("e") == "markPriceUpdate":
                                origin = "markPrice"
                    except Exception:
                        pass
                    print(f"{GRAY}🛰️ WS[{ws_msg_count['total']}] {ts()} | origem={origin} | bytes≈{len(preview(message))} | {preview(message)}{RESET}")

                px = extract_price(message)
                if px > 0:
                    last_tick_ts = time.time()
                    for brick_close, d, brick_id in renko.feed_price(px):
                        apply_logic_on_brick(state, brick_close, d, brick_id, source="WS")
                else:
                    if TELEMETRY_FULL:
                        print(f"{YELLOW}⛔ Tick ignorado (sem preço válido) | {preview(message)}{RESET}")
            except Exception as e:
                print(f"{YELLOW}⚠️ on_msg error:{RESET}", e)
                traceback.print_exc()

        # feeder HTTP fallback (liga quando WS fica mudo)
        stop_flag = {"stop": False}
        def http_feeder():
            while not stop_flag["stop"]:
                time.sleep(HTTP_FALLBACK_INTERVAL_S)
                if not HTTP_FALLBACK:
                    continue
                silence = time.time() - last_tick_ts
                if silence < HTTP_FALLBACK_AFTER_S:
                    continue  # WS está saudável
                try:
                    px = float(client.ticker_price(symbol=SYMBOL)['price'])
                    if TELEMETRY_FULL:
                        print(f"{GRAY}🌐 HTTP {ts()} | silêncio WS={silence:.2f}s | px={px}{RESET}")
                    for brick_close, d, brick_id in renko.feed_price(px):
                        apply_logic_on_brick(state, brick_close, d, brick_id, source="HTTP")
                except Exception:
                    # ignora erro momentâneo
                    pass

        # heartbeat (pode ser silenciado)
        def heartbeat():
            while not stop_flag["stop"]:
                time.sleep(HEARTBEAT_S)
                try:
                    ws.ping()
                    if not SILENT_HEARTBEAT:
                        uptime = round(time.time()-start_ts,1)
                        print(f"{MAGENTA}💓 Heartbeat (ping {HEARTBEAT_S}s) | Uptime {uptime}s | WS_msgs={ws_msg_count['total']}{RESET}")
                except Exception:
                    break

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
            threading.Thread(target=heartbeat, daemon=True).start()
            threading.Thread(target=http_feeder, daemon=True).start()
            print(f"{BLUE}▶️ WS ativo para {SYMBOL}. Aguardando bricks…{RESET}")
        except Exception as e:
            print(f"{RED}❌ Erro ao abrir WS:{RESET}", e)

        try:
            while True:
                time.sleep(5)
                silence = time.time() - last_tick_ts
                if silence > NO_TICK_RESTART_S:
                    print(f"{YELLOW}⏱️ {silence:.1f}s sem tick WS (limite {NO_TICK_RESTART_S}s). Reiniciando WS…{RESET}")
                    try: ws.stop()
                    except Exception: pass
                    break
        except Exception as e:
            print(f"{YELLOW}⚠️ Loop principal caiu:{RESET}", e)
            traceback.print_exc()
            try: ws.stop()
            except Exception: pass

        stop_flag["stop"] = True  # encerra heartbeat/feeder antes de reconectar
        print(f"{CYAN}⚡ Reabrindo WS agora… (instantâneo){RESET}")
        time.sleep(0.1)

if __name__=="__main__":
    run_ws()
