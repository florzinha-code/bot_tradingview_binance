from flask import Flask, request, jsonify
from binance.um_futures import UMFutures
import os, json, time, threading, queue

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

SYMBOL = "BTCUSDT"
LEVERAGE = 1
MARGIN_TYPE = "CROSSED"
USE_BALANCE_PCT = 0.85
MIN_MS_BETWEEN_ORDERS = 300
DEDUP_WINDOW_MS = 800

client = UMFutures(key=API_KEY, secret=API_SECRET)
app = Flask(__name__)

order_q = queue.Queue()
last_seen = {}
last_exec_ms = 0
lock = threading.Lock()

def now_ms(): return int(time.time() * 1000)
def log(*args): print(time.strftime("[%H:%M:%S]"), *args, flush=True)

def check_connection():
    try:
        client.ping()
        log("✅ Conexão Binance OK")
        return True
    except Exception as e:
        log("❌ Falha conexão Binance:", e)
        return False

def ensure_leverage_and_margin():
    try:
        client.change_margin_type(symbol=SYMBOL, marginType=MARGIN_TYPE)
        log("ℹ️ Margem CROSS confirmada")
    except Exception as e:
        if "No need" in str(e): log("✅ Já CROSS")
        else: log("⚠️ MarginType:", e)
    try:
        client.change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
        log(f"✅ Alavancagem {LEVERAGE}x")
    except Exception as e:
        log("⚠️ Leverage:", e)

def get_balance():
    try:
        bal = next(float(x["balance"]) for x in client.balance() if x["asset"] == "USDT")
        log(f"💰 Saldo: {bal:.2f} USDT")
        return bal
    except Exception as e:
        log("⚠️ Erro saldo:", e)
        return 0.0

def get_price():
    try:
        p = float(client.ticker_price(symbol=SYMBOL)["price"])
        log(f"📈 Preço: {p}")
        return p
    except Exception as e:
        log("⚠️ Erro preço:", e)
        return 0.0

def compute_qty(price, balance):
    return max(round((balance * USE_BALANCE_PCT) / price, 3), 0.001)

def place_order(action):
    ensure_leverage_and_margin()
    price = get_price()
    bal = get_balance()
    if bal < 5: return {"status": "❌ saldo baixo"}
    qty = compute_qty(price, bal)
    side = "BUY" if action == "buy" else "SELL"
    log(f"🚀 {side} {qty} BTC @ {price}")
    try:
        r = client.new_order(symbol=SYMBOL, side=side, type="MARKET", quantity=qty)
        log("✅ Executado:", r)
        return {"ok": True}
    except Exception as e:
        log("❌ Erro ordem:", e)
        return {"ok": False, "erro": str(e)}

def worker():
    global last_exec_ms
    while True:
        action = order_q.get()
        with lock:
            gap = now_ms() - last_exec_ms
            if gap < MIN_MS_BETWEEN_ORDERS:
                time.sleep((MIN_MS_BETWEEN_ORDERS - gap)/1000)
        result = place_order(action)
        last_exec_ms = now_ms()
        order_q.task_done()
        log("🧾 Fim:", result)

threading.Thread(target=worker, daemon=True).start()

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = json.loads(request.data or "{}")
        action = data.get("action")
        log("🚨 ALERTA:", action)
        if action not in ("buy", "sell"):
            return jsonify({"status": "inválido"}), 400

        t = now_ms()
        if action in last_seen and (t - last_seen[action]) < DEDUP_WINDOW_MS:
            log("⛔️ Ignorado duplicado:", action)
            return jsonify({"status": "ignored"}), 200

        last_seen[action] = t
        order_q.put(action)
        return jsonify({"status": "queued", "action": action})
    except Exception as e:
        log("❌ Erro webhook:", e)
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({"ok": check_connection()})

if __name__ == "__main__":
    log("🚀 Bot ativo em modo seguro.")
    check_connection()
    app.run(host="0.0.0.0", port=5000)
