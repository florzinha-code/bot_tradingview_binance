from flask import Flask, request, jsonify
from binance.um_futures import UMFutures
from binance.websocket.um_futures.websocket_client import UMFuturesWebsocketClient
import os, json, time, threading, queue

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

SYMBOL = "BTCUSDT"
LEVERAGE = 1
MARGIN_TYPE = "CROSSED"
MIN_MS_BETWEEN_ORDERS = 900
DEDUP_WINDOW_MS = 600
USE_BALANCE_PCT = 0.85

client = UMFutures(key=API_KEY, secret=API_SECRET)
app = Flask(__name__)
order_q = queue.Queue()
last_seen = {}
last_exec_ms = 0
rl_lock = threading.Lock()

def now_ms(): return int(time.time() * 1000)
def log(*args): print(*args, flush=True)

def ensure_leverage_and_margin():
    try:
        client.change_margin_type(symbol=SYMBOL, marginType=MARGIN_TYPE)
        log("✅ Margin type CROSS ok")
    except Exception as e:
        if "No need to change margin type" in str(e):
            log("ℹ️ Margin type já CROSS")
        else:
            log("⚠️ change_margin_type:", e)
    try:
        client.change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
        log(f"✅ Leverage {LEVERAGE}x ok")
    except Exception as e:
        log("⚠️ change_leverage:", e)

def get_balance_usdt():
    try:
        acc = client.account()
        for asset in acc["assets"]:
            if asset["asset"] == "USDT":
                bal = float(asset["walletBalance"])
                log(f"💰 Saldo detectado (Futures): {bal} USDT")
                return bal
    except Exception as e:
        log("⚠️ Erro ao consultar saldo:", e)
    return 0.0

def get_market_price():
    try:
        price = float(client.ticker_price(symbol=SYMBOL)["price"])
        log(f"📈 Preço atual: {price}")
        return price
    except Exception as e:
        log("⚠️ Erro ao pegar preço:", e)
        return 0.0

def compute_qty(price, balance):
    qty = round((balance * USE_BALANCE_PCT) / max(price, 1e-9), 3)
    return max(qty, 0.001)

def place_order(action):
    ensure_leverage_and_margin()
    price = get_market_price()
    balance = get_balance_usdt()

    if balance <= 5:
        log("❌ Saldo insuficiente:", balance)
        return {"status": "insufficient_balance"}

    qty = compute_qty(price, balance)
    side = "BUY" if action in ("buy", "stop_sell") else "SELL"

    log(f"🚀 Tentando abrir ordem {side} {qty} {SYMBOL} a {price}")

    try:
        order = client.new_order(symbol=SYMBOL, side=side, type="MARKET", quantity=qty)
        log("✅ Ordem executada com sucesso:", order)
        return {"ok": True, "action": action, "qty": qty}
    except Exception as e:
        log("❌ Erro na ordem:", e)
        return {"ok": False, "error": str(e)}

def order_worker():
    global last_exec_ms
    while True:
        action = order_q.get()
        try:
            with rl_lock:
                gap = now_ms() - last_exec_ms
                wait_ms = max(0, MIN_MS_BETWEEN_ORDERS - gap)
            if wait_ms > 0:
                time.sleep(wait_ms / 1000.0)
            res = place_order(action)
            last_exec_ms = now_ms()
            log("🧾 Resultado:", res)
        except Exception as e:
            log("❌ Erro no worker:", e)
        finally:
            order_q.task_done()

threading.Thread(target=order_worker, daemon=True).start()

@app.route("/health")
def health():
    return jsonify({"ok": True})

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = json.loads(request.data or "{}")
        action = data.get("action")
        log("🚨 ALERTA RECEBIDO:", action)
        if action not in ("buy", "sell"):
            return jsonify({"status": "invalid_action"}), 400
        t = now_ms()
        if action in last_seen and (t - last_seen[action]) < DEDUP_WINDOW_MS:
            log("⛔️ Ignorado (dedupe):", action)
            return jsonify({"status": "ignored_dedupe"}), 200
        last_seen[action] = t
        order_q.put(action)
        return jsonify({"status": "queued", "action": action}), 200
    except Exception as e:
        log("❌ Erro geral webhook:", e)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
