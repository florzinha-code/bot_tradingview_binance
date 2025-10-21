# ws_bot_renko.py / bot_tradingview_binance_ws.py
# WebSocket Binance Futures — Renko 550 pts + EMA9/21 + RSI14 + reversão = stop
# Versão 24/7 para Render Worker (com watchdog, heartbeat e reconexão estável)

import os, time, math, json, traceback, functools, threading
from binance.um_futures import UMFutures
from binance.websocket.um_futures.websocket_client import UMFuturesWebsocketClient

# ======== LOGS IMEDIATOS NO RENDER ========
print = functools.partial(print, flush=True)

# ===================== CONFIG =====================
SYMBOL = "BTCUSDT"
LEVERAGE = 1
MARGIN_TYPE = "CROSSED"
QTY_PCT = 0.85
BOX_POINTS = 550.0
REV_BOXES = 2
EMA_FAST = 9
EMA_SLOW = 21
RSI_LEN = 14
RSI_WIN_LONG = (40.0, 65.0)
RSI_WIN_SHORT = (35.0, 60.0)
MIN_QTY = 0.001
NO_TICK_RESTART_S = 120
HEARTBEAT_S = 30   # ✅ reduzido para 30 segundos
# ===================== CONFIG =====================

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
client = UMFutures(key=API_KEY, secret=API_SECRET)

# --- Setup inicial ---
def setup_symbol():
    try:
        client.change_margin_type(symbol=SYMBOL, marginType=MARGIN_TYPE)
        print(f"✅ Modo de margem: {MARGIN_TYPE}")
    except Exception as e:
        if "No need to change margin type" in str(e):
            print("ℹ️ Margem já configurada.")
        else:
            print("⚠️ change_margin_type:", e)
    try:
        client.change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
        print(f"✅ Alavancagem: {LEVERAGE}x")
    except Exception as e:
        print("⚠️ change_leverage:", e)

# --- EMA ---
class EMA:
    def __init__(self, length:int):
        self.len = length
        self.mult = 2.0/(length+1.0)
        self.value = None
    def update(self, x:float):
        if self.value is None:
            self.value = x
        else:
            self.value = (x - self.value)*self.mult + self.value
        return self.value

# --- RSI (Wilder) ---
class RSI_Wilder:
    def __init__(self, length:int):
        self.len = length
        self.avgU = None
        self.avgD = None
        self.prev = None
    def update(self, x:float):
        if self.prev is None:
            self.prev = x
            return None
        ch = x - self.prev
        up = max(ch, 0.0); dn = max(-ch, 0.0)
        if self.avgU is None:
            self.avgU, self.avgD = up, dn
        else:
            self.avgU = (self.avgU*(self.len - 1) + up) / self.len
            self.avgD = (self.avgD*(self.len - 1) + dn) / self.len
        self.prev = x
        rs = self.avgU / max(self.avgD, 1e-12)
        return 100.0 - 100.0/(1.0+rs)

# --- Renko Engine ---
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
            self.anchor = px
            return created
        up_th = self.anchor + self.box
        down_th = self.anchor - self.box
        while px >= up_th:
            self.anchor += self.box if self.dir != -1 else self.box*self.rev
            self.dir = 1
            self.brick_id += 1
            created.append((self.anchor, self.dir, self.brick_id))
            up_th = self.anchor + self.box
            down_th = self.anchor - self.box
        while px <= down_th:
            self.anchor -= self.box if self.dir != 1 else self.box*self.rev
            self.dir = -1
            self.brick_id += 1
            created.append((self.anchor, self.dir, self.brick_id))
            up_th = self.anchor + self.box
            down_th = self.anchor - self.box
        return created

# --- Estado da estratégia ---
class StrategyState:
    def __init__(self):
        self.in_long = False
        self.in_short = False
        self.ema_fast = EMA(EMA_FAST)
        self.ema_slow = EMA(EMA_SLOW)
        self.rsi = RSI_Wilder(RSI_LEN)
    def update_indics(self, brick_close:float):
        e1 = self.ema_fast.update(brick_close)
        e2 = self.ema_slow.update(brick_close)
        rsi = self.rsi.update(brick_close)
        return e1, e2, rsi

# --- Quantidade dinâmica ---
def get_qty(price:float):
    bal = client.balance()
    usdt = next((float(b["balance"]) for b in bal if b["asset"]=="USDT"), 0.0)
    qty = round(max(MIN_QTY, (usdt * QTY_PCT) / price), 3)
    return qty, usdt

# --- Execução de ordens ---
def market_order(side:str, qty:float, reduce_only:bool=False):
    params = dict(symbol=SYMBOL, side=side, type="MARKET", quantity=qty)
    if reduce_only:
        params["reduceOnly"] = "true"
    try:
        client.new_order(**params)
        print(f"✅ Ordem {side} qty={qty} reduceOnly={reduce_only}")
        return True
    except Exception as e:
        print("❌ Erro ao enviar ordem:", e)
        return False

# --- Extração segura de preço ---
def extract_price(message):
    if isinstance(message, str):
        try: message = json.loads(message)
        except Exception: return 0.0
    val = message.get("p") or message.get("c") or message.get("price")
    try: return float(val)
    except Exception: return 0.0

# --- Lógica principal ---
def apply_logic_on_brick(state, brick_close, dir, brick_id):
    e1, e2, rsi = state.update_indics(brick_close)
    if e1 is None or e2 is None or rsi is None:
        return
    price = float(client.ticker_price(symbol=SYMBOL)['price'])
    qty, _ = get_qty(price)

    renkoVerde = (dir == +1)
    renkoVermelho = (dir == -1)

    # Stops
    if state.in_long and renkoVermelho:
        print(f"🛑 STOP COMPRA #{brick_id} @ {brick_close:.2f}")
        if market_order("SELL", qty, True): state.in_long = False
    if state.in_short and renkoVerde:
        print(f"🛑 STOP VENDA  #{brick_id} @ {brick_close:.2f}")
        if market_order("BUY", qty, True): state.in_short = False

    # Entradas
    can_long = renkoVerde and (e1 > e2) and (RSI_WIN_LONG[0] <= rsi <= RSI_WIN_LONG[1])
    can_short = renkoVermelho and (e1 < e2) and (RSI_WIN_SHORT[0] <= rsi <= RSI_WIN_SHORT[1])

    if can_long and not state.in_long:
        print(f"🚀 COMPRA #{brick_id} @ {brick_close:.2f} | EMA9={e1:.2f} EMA21={e2:.2f} RSI={rsi:.2f}")
        if market_order("BUY", qty): state.in_long, state.in_short = True, False
    if can_short and not state.in_short:
        print(f"🔻 VENDA  #{brick_id} @ {brick_close:.2f} | EMA9={e1:.2f} EMA21={e2:.2f} RSI={rsi:.2f}")
        if market_order("SELL", qty): state.in_short, state.in_long = True, False

# --- Loop principal com reconexão e heartbeat ---
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
                if px > 0:
                    last_tick_ts = time.time()
                    for brick_close, d, brick_id in renko.feed_price(px):
                        print(f"🧱 Brick {brick_id} {'▲' if d==1 else '▼'} close={brick_close:.2f}")
                        apply_logic_on_brick(state, brick_close, d, brick_id)
            except Exception as e:
                print("⚠️ on_msg error:", e)
                traceback.print_exc()

        # heartbeat pings (a cada 30s)
        def heartbeat():
            while True:
                time.sleep(HEARTBEAT_S)
                try:
                    ws.ping()
                    print("💓 Heartbeat enviado (ping 30s)")
                except Exception:
                    print("⚠️ Heartbeat falhou, encerrando thread…")
                    break

        try:
            ws.agg_trade(symbol=SYMBOL.lower(), id=1, callback=on_msg)
            threading.Thread(target=heartbeat, daemon=True).start()
            print(f"▶️ WS assinado em aggTrade {SYMBOL}. Aguardando ticks…")
        except Exception as e:
            print("❌ Erro ao abrir WS:", e)

        # watchdog
        try:
            while True:
                time.sleep(5)
                if time.time() - last_tick_ts > NO_TICK_RESTART_S:
                    print(f"⏱️ {NO_TICK_RESTART_S}s sem tick. Reiniciando WS…")
                    try: ws.stop()
                    except Exception: pass
                    break
        except Exception as e:
            print("⚠️ Loop principal caiu:", e)
            traceback.print_exc()
            try: ws.stop()
            except Exception: pass

        print("⏳ Reabrindo WS em 3s…")
        time.sleep(3)

if __name__ == "__main__":
    run_ws()
