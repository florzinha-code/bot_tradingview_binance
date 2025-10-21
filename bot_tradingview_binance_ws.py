# ws_bot_renko.py
# Bot WebSocket Binance Futures — Renko 550 pts + EMA9/21 + RSI14 + reversão = stop
# Mantém toda a lógica original com correções para rodar 24/7 no Render

import os, math, time, threading, traceback, requests
from flask import Flask
from binance.um_futures import UMFutures
from binance.websocket.um_futures.websocket_client import UMFuturesWebsocketClient

# ===================== CONFIG =====================
SYMBOL        = "BTCUSDT"
LEVERAGE      = 1
MARGIN_TYPE   = "CROSSED"
QTY_PCT       = 0.85
BOX_POINTS    = 550.0
REV_BOXES     = 2
EMA_FAST      = 9
EMA_SLOW      = 21
RSI_LEN       = 14
RSI_WIN_LONG  = (40.0, 65.0)
RSI_WIN_SHORT = (35.0, 60.0)
MIN_QTY       = 0.001
# ===================== CONFIG =====================

API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
client = UMFutures(key=API_KEY, secret=API_SECRET)

# --- setup margin/leverage ---
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

# --- RSI ---
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
        up = max(ch, 0.0)
        dn = max(-ch, 0.0)
        if self.avgU is None:
            self.avgU, self.avgD = up, dn
        else:
            self.avgU = (self.avgU*(self.len - 1) + up) / self.len
            self.avgD = (self.avgD*(self.len - 1) + dn) / self.len
        self.prev = x
        denom = max(self.avgD, 1e-12)
        rs = self.avgU / denom
        return 100.0 - 100.0/(1.0+rs)

# --- Renko Engine ---
class RenkoEngine:
    def __init__(self, box_points:float, rev_boxes:int):
        self.box = float(box_points)
        self.rev = int(rev_boxes)
        self.anchor = None
        self.dir = 0
        self.last_brick_close = None
        self.brick_id = 0
    def feed_price(self, px:float):
        created = []
        if self.anchor is None:
            self.anchor = px
            self.last_brick_close = px
            return created
        up_th = self.anchor + self.box
        down_th = self.anchor - self.box
        while px >= up_th:
            if self.dir == -1:
                self.anchor += self.box*self.rev
            else:
                self.anchor += self.box
            self.dir = 1
            self.brick_id += 1
            created.append((self.anchor, self.dir, self.brick_id))
            up_th = self.anchor + self.box
            down_th = self.anchor - self.box
        while px <= down_th:
            if self.dir == 1:
                self.anchor -= self.box*self.rev
            else:
                self.anchor -= self.box
            self.dir = -1
            self.brick_id += 1
            created.append((self.anchor, self.dir, self.brick_id))
            up_th = self.anchor + self.box
            down_th = self.anchor - self.box
        return created

# --- Estado e indicadores ---
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
        order = client.new_order(**params)
        print(f"✅ Ordem {side} qty={qty} reduceOnly={reduce_only}")
        return True
    except Exception as e:
        print("❌ Erro ao enviar ordem:", e)
        return False

# --- Lógica da estratégia ---
def apply_logic_on_brick(state:StrategyState, brick_close:float, dir:int, brick_id:int):
    e1, e2, rsi = state.update_indics(brick_close)
    if e1 is None or e2 is None or rsi is None:
        return
    price = float(client.ticker_price(symbol=SYMBOL)['price'])
    qty, usdt = get_qty(price)
    renkoVerde = (dir == +1)
    renkoVermelho = (dir == -1)
    if state.in_long and renkoVermelho:
        print(f"🛑 STOP COMPRA @{brick_close:.2f}")
        if market_order("SELL", qty, True): state.in_long = False
    if state.in_short and renkoVerde:
        print(f"🛑 STOP VENDA @{brick_close:.2f}")
        if market_order("BUY", qty, True): state.in_short = False
    can_long = renkoVerde and (e1 > e2) and (RSI_WIN_LONG[0] <= rsi <= RSI_WIN_LONG[1])
    can_short = renkoVermelho and (e1 < e2) and (RSI_WIN_SHORT[0] <= rsi <= RSI_WIN_SHORT[1])
    if can_long and not state.in_long:
        print(f"🚀 COMPRA @{brick_close:.2f} | EMA9={e1:.2f} EMA21={e2:.2f} RSI={rsi:.2f}")
        if market_order("BUY", qty):
            state.in_long, state.in_short = True, False
    if can_short and not state.in_short:
        print(f"🔻 VENDA @{brick_close:.2f} | EMA9={e1:.2f} EMA21={e2:.2f} RSI={rsi:.2f}")
        if market_order("SELL", qty):
            state.in_short, state.in_long = True, False

# --- WebSocket principal ---
def run_ws():
    setup_symbol()
    state = StrategyState()
    renko = RenkoEngine(BOX_POINTS, REV_BOXES)
    ws = UMFuturesWebsocketClient()

    def on_msg(_, message):
        try:
            px = float(message.get("p") or message.get("c") or 0.0)
            if px > 0:
                for brick_close, d, brick_id in renko.feed_price(px):
                    apply_logic_on_brick(state, brick_close, d, brick_id)
        except Exception as e:
            print("⚠️ on_msg error:", e)
            traceback.print_exc()

    ws.start()
    ws.agg_trade(symbol=SYMBOL.lower(), id=1, callback=on_msg)
    print("▶️ WebSocket iniciado — ouvindo aggTrade", SYMBOL)
    while True:
        try:
            time.sleep(1)
        except Exception:
            print("⚠️ Loop principal reiniciado...")
            ws.stop()
            time.sleep(3)
            run_ws()

# ===================== RENDER FIX =====================
app = Flask(__name__)
@app.route("/")
def home():
    return "✅ Bot WebSocket Binance ativo e rodando."

threading.Thread(target=run_ws, daemon=True).start()

# ===================== KEEPALIVE INTERNO =====================
def keep_alive():
    url = "https://bot-tradingview-binance.onrender.com/"
    while True:
        try:
            requests.get(url, timeout=10)
            print("🌐 Keepalive ping enviado com sucesso.")
        except Exception as e:
            print("⚠️ Erro no keepalive:", e)
        time.sleep(600)  # 10 minutos

threading.Thread(target=keep_alive, daemon=True).start()

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=PORT)

