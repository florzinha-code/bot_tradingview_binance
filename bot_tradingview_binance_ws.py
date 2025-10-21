# ws_bot_renko.py
# Bot WebSocket Binance Futures — Renko 550 pts + EMA9/21 + RSI14 + reversão = stop

import os, math, time, threading, traceback
from collections import deque
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


# --- EMA, RSI, Renko e lógica iguais aos seus ---
# (mantém exatamente o mesmo conteúdo das suas classes e funções)
# ------------------------------------------------
# ⚠️ (pule direto até o final, onde vem a alteração importante)
# ------------------------------------------------

# --- Loop WebSocket: ouvindo ticks e processando estratégia ---
def run_ws():
    setup_symbol()
    state = StrategyState()
    renko = RenkoEngine(BOX_POINTS, REV_BOXES)

    ws = UMFuturesWebsocketClient()

    def on_msg(_, message):
        try:
            px = float(message.get("p") or message.get("c") or 0.0)
            if px <= 0:
                return
            created = renko.feed_price(px)
            for (brick_close, d, brick_id) in created:
                apply_logic_on_brick(state, brick_close, d, brick_id)
        except Exception as e:
            print("⚠️ on_msg error:", e)
            traceback.print_exc()

    ws.start()
    ws.agg_trade(symbol=SYMBOL.lower(), id=1, callback=on_msg)
    print("▶️ WebSocket iniciado — ouvindo aggTrade", SYMBOL)

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        ws.stop()
        print("⏹️ WebSocket parado")


# ===================== RENDER FIX =====================
# Render exige que algo rode na porta $PORT
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Bot WebSocket Binance ativo e rodando."

# Executa o WebSocket em paralelo
threading.Thread(target=run_ws, daemon=True).start()

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=PORT)
