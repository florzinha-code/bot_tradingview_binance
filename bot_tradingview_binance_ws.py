# bot_tradingview_binance_ws.py
# Binance Futures WebSocket — Renko 550 pts + EMA9/21 + RSI14 + reversão = stop
#  ✅ PnL detalhado (USDT + %)
#  ✅ Reconexão instantânea (0.1s)
#  ✅ Heartbeat a cada 30s (SILENCIADO)
#  ✅ Debounce inteligente — bloqueia duplicidade na MESMA direção
#  ✅ Auto-relogin se a sessão cair
#  ✅ Logs enxutos (1 linha por brick)
#  ✅ Extração robusta de preço (aggTrade e kline)

import os, time, json, traceback, functools, threading
from binance.um_futures import UMFutures
from binance.websocket.um_futures.websocket_client import UMFuturesWebsocketClient

# ---------- cores ----------
RESET = "\033[0m"; RED = "\033[91m"; GREEN = "\033[92m"; YELLOW = "\033[93m"
BLUE = "\033[94m"; MAGENTA = "\033[95m"; CYAN = "\033[96m"; GRAY = "\033[90m"

# ---------- print flush ----------
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

NO_TICK_RESTART_S = 120
HEARTBEAT_S = 30
SILENT_HEARTBEAT = True

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

# ---------- client helper ----------
def get_client():
    return UMFutures(key=API_KEY, secret=API_SECRET)
client = get_client()

# ---------- setup símbolo ----------
def setup_symbol():
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
        self.mult = 2.0 / (length + 1.0)
        self.value = None
    def update(self, x:float):
        self.value = x if self.value is None else (x - self.value) * self.mult + self.value
        return self.value

# ---------- RSI ----------
class RSI_Wilder:
    def __init__(self, length:int):
        self.len = length
        self.avgU = self.avgD = self.prev = None
    def update(self, x:float):
        if self.prev is None:
            self.prev = x; return None
        ch = x - self.prev
        up, dn = max(ch, 0), max(-ch, 0)
        if self.avgU is None:
            self.avgU, self.avgD = up, dn
        else:
            self.avgU = (self.avgU*(self.len-1) + up) / self.len
            self.avgD = (self.avgD*(self.len-1) + dn) / self.len
        self.prev = x
        rs = self.avgU / max(self.avgD, 1e-12)
        return 100 - 100 / (1 + rs)

# ---------- Renko ----------
class RenkoEngine:
    def __init__(self, box_points:float, rev_boxes:int):
        self.box = float(box_points)
        self.rev = int(rev_boxes)
        self.anchor = None
        self.dir = 0
        self.brick_id = 0
    def feed_price(self, px:float):
        created = []
        if self.anchor is None:
            self.anchor = px; return created
        up_th = self.anchor + self.box
        down_th = self.anchor - self.box
        while px >= up_th:
            self.anchor += self.box if self.dir != -1 else self.box * self.rev
            self.dir = 1; self.brick_id += 1
            created.append((self.anchor, self.dir, self.brick_id))
            up_th = self.anchor + self.box; down_th = self.anchor - self.box
        while px <= down_th:
            self.anchor -= self.box if self.dir != 1 else self.box * self.rev
            self.dir = -1; self.brick_id += 1
            created.append((self.anchor, self.dir, self.brick_id))
            up_th = self.anchor + self.box; down_th = self.anchor - self.box
        return created

# ---------- estado ----------
class StrategyState:
    def __init__(self):
        self.in_long = False
        self.in_short = False
        self.ema_fast = EMA(EMA_FAST)
        self.ema_slow = EMA(EMA_SLOW)
        self.rsi      = RSI_Wilder(RSI_LEN)
        self.last_side = None     # "BUY" ou "SELL"
        self.last_brick = None    # evita duplicidade mesma direção
    def update_indics(self, brick_close:float):
        return self.ema_fast.update(brick_close), self.ema_slow.update(brick_close), self.rsi.update(brick_close)

# ---------- quantidade ----------
def get_qty(price:float):
    try:
        bal = client.balance()
    except Exception:
        client = get_client()
        bal = client.balance()
    usdt = next((float(b["balance"]) for b in bal if b.get("asset") == "USDT"), 0.0)
    qty  = round(max(MIN_QTY, (usdt * QTY_PCT) / max(price, 1e-9)), 3)
    return qty, usdt

# ---------- PnL ----------
def show_pnl():
    try:
        pos_data = client.get_position_risk(symbol=SYMBOL)
        if pos_data:
            pos = pos_data[0]
            pnl   = float(pos.get("unRealizedProfit", 0))
            entry = float(pos.get("entryPrice", 0))
            amt   = float(pos.get("positionAmt", 0))
            side  = "LONG" if amt > 0 else "SHORT" if amt < 0 else "FLAT"
            if amt != 0 and entry > 0:
                mark = float(pos.get("markPrice", 0))
                pct  = ((mark - entry) / entry * 100) * (1 if amt > 0 else -1)
            else: pct = 0.0
            color = GREEN if pnl >= 0 else RED
            print(f"{color}💰 PnL: {pnl:.2f} USDT | Δ={pct:.3f}% | {side}{RESET}")
    except Exception as e:
        print(f"{YELLOW}⚠️ Erro ao obter PnL:{RESET}", e)

# ---------- ordens ----------
def market_order(side:str, qty:float, reduce_only:bool=False):
    params = dict(symbol=SYMBOL, side=side, type="MARKET", quantity=qty)
    if reduce_only:
        params["reduceOnly"] = "true"
    try:
        client.new_order(**params)
    except Exception:
        client = get_client()
        client.new_order(**params)
    print(f"{GREEN}✅ {side} qty={qty} reduceOnly={reduce_only}{RESET}")
    time.sleep(0.2); show_pnl(); return True

# ---------- preço ----------
def _first_number(*vals):
    for v in vals:
        try:
            if v is None: continue
            return float(v)
        except Exception: continue
    return 0.0

def extract_price(message):
    try:
        if isinstance(message, str): message = json.loads(message)
        if isinstance(message, (list, tuple)) and message: message = message[0]
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
    except Exception: return 0.0

# ---------- lógica ----------
def apply_logic_on_brick(state, brick_close, dir, brick_id):
    e1,e2,rsi = state.update_indics(brick_close)
    if e1 is None or e2 is None or rsi is None: return
    try: price = float(client.ticker_price(symbol=SYMBOL)['price'])
    except: price = brick_close
    qty,_ = get_qty(price)
    renkoVerde = dir == 1; renkoVermelho = dir == -1
    print(f"{MAGENTA}🧱 Brick {brick_id} {'▲' if renkoVerde else '▼'} "
          f"| EMA9={e1:.2f} EMA21={e2:.2f} RSI={rsi:.2f}{RESET}")

    # --- STOPs ---
    if state.in_long and renkoVermelho:
        if market_order("SELL", qty, True):
            print(f"{YELLOW}🛑 STOP COMPRA @ {brick_close:.2f}{RESET}")
            state.in_long = False; state.last_side = "SELL"; state.last_brick = brick_id

    if state.in_short and renkoVerde:
        if market_order("BUY", qty, True):
            print(f"{YELLOW}🛑 STOP VENDA  @ {brick_close:.2f}{RESET}")
            state.in_short = False; state.last_side = "BUY"; state.last_brick = brick_id

    # --- ENTRADAS (com debounce por direção) ---
    long_ok  = renkoVerde and (e1>e2) and (RSI_WIN_LONG[0]<=rsi<=RSI_WIN_LONG[1]) and not state.in_long
    short_ok = renkoVermelho and (e1<e2) and (RSI_WIN_SHORT[0]<=rsi<=RSI_WIN_SHORT[1]) and not state.in_short

    if long_ok and not (state.last_side=="BUY" and state.last_brick==brick_id):
        if market_order("BUY", qty):
            print(f"{GREEN}🚀 COMPRA @ {brick_close:.2f}{RESET}")
            state.in_long, state.in_short = True, False
            state.last_side, state.last_brick = "BUY", brick_id

    if short_ok and not (state.last_side=="SELL" and state.last_brick==brick_id):
        if market_order("SELL", qty):
            print(f"{CYAN}🔻 VENDA  @ {brick_close:.2f}{RESET}")
            state.in_short, state.in_long = True, False
            state.last_side, state.last_brick = "SELL", brick_id

# ---------- loop WS ----------
def run_ws():
    setup_symbol()
    while True:
        state = StrategyState()
        renko = RenkoEngine(BOX_POINTS, REV_BOXES)
        ws = UMFuturesWebsocketClient()
        last_tick_ts = time.time()
        def on_msg(_, message):
            nonlocal last_tick_ts
            try:
                px = extract_price(message)
                if px>0:
                    last_tick_ts = time.time()
                    for brick_close,d,brick_id in renko.feed_price(px):
                        apply_logic_on_brick(state, brick_close, d, brick_id)
            except Exception as e:
                print(f"{YELLOW}⚠️ on_msg error:{RESET}", e)
        def heartbeat():
            while True:
                time.sleep(HEARTBEAT_S)
                try:
                    ws.ping()
                    if not SILENT_HEARTBEAT:
                        uptime = round(time.time()-start_ts,1)
                        print(f"{MAGENTA}💓 Heartbeat {uptime}s{RESET}")
                except: break
        start_ts = time.time()
        try:
            ws.agg_trade(symbol=SYMBOL.lower(), id=1, callback=on_msg)
        except TypeError:
            ws.agg_trade(symbol=SYMBOL.lower(), stream_id="main", callback=on_msg)
        threading.Thread(target=heartbeat, daemon=True).start()
        print(f"{BLUE}▶️ WS ativo para {SYMBOL}. Aguardando bricks…{RESET}")
        try:
            while True:
                time.sleep(5)
                if time.time()-last_tick_ts>NO_TICK_RESTART_S:
                    print(f"{YELLOW}⏱️ {NO_TICK_RESTART_S}s sem tick — reiniciando WS…{RESET}")
                    ws.stop(); break
        except Exception as e:
            print(f"{YELLOW}⚠️ Loop caiu:{RESET}", e)
            ws.stop()
        print(f"{CYAN}⚡ Reabrindo WS instantaneamente…{RESET}")
        time.sleep(0.1)

if __name__ == "__main__":
    run_ws()
