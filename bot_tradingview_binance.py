# bot_tradingview_binance.py
from flask import Flask, request, jsonify
from binance.um_futures import UMFutures
from binance.websocket.um_futures.websocket_client import UMFuturesWebsocketClient
import os, json, time, threading, queue, math

# ------------------ Config básica ------------------
API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

SYMBOL      = "BTCUSDT"
LEVERAGE    = 1
MARGIN_TYPE = "CROSSED"   # CROSS por padrão

# “Modo guerra”: garante no máx. 1 ordem a cada X ms
MIN_MS_BETWEEN_ORDERS = 900

# Em quanto tempo uma mensagem igual é considerada duplicada (ms)
DEDUP_WINDOW_MS = 600

# Percentual do saldo que vira posição
USE_BALANCE_PCT = 0.85

# ------------------ Clientes ------------------
client = UMFutures(key=API_KEY, secret=API_SECRET)

# ------------------ App Flask ------------------
app = Flask(__name__)

# Fila de ordens (uma thread executa)
order_q = queue.Queue()

# Cache simples para deduplicar rajadas (action -> last_ms)
last_seen = {}
last_exec_ms = 0

# Mutex pro rate-limit local
rl_lock = threading.Lock()

def now_ms():
    return int(time.time() * 1000)

def log(*args):
    print(*args, flush=True)

# ------------------ Auxiliares Binance ------------------
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
    bal = client.balance()
    for b in bal:
        if b.get("asset") == "USDT":
            return float(b.get("balance", 0))
    return 0.0

def get_market_price():
    return float(client.ticker_price(symbol=SYMBOL)['price'])

def side_to_reduce_only(action: str) -> bool:
    # Stops só fecham posição
    return action in ("stop_buy", "stop_sell")

def side_from_action(action: str) -> str:
    # stop_buy -> SELL (fecha long); stop_sell -> BUY (fecha short)
    return "BUY" if action in ("buy", "stop_sell") else "SELL"

def compute_qty(price: float, balance: float) -> float:
    qty = (balance * USE_BALANCE_PCT) / max(price, 1e-9)
    # Arredonda para 3 casas (BTCUSDT aceita 3 decimais no Futures)
    qty = round(qty, 3)
    return max(qty, 0.001)

def place_order(action: str):
    """Executa a ordem no REST com retries e reduceOnly quando for stop."""
    ensure_leverage_and_margin()

    price   = get_market_price()
    balance = get_balance_usdt()

    if balance <= 5:
        log("❌ Saldo insuficiente:", balance)
        return {"status": "insufficient_balance"}

    qty  = compute_qty(price, balance)
    side = side_from_action(action)
    reduce_only = side_to_reduce_only(action)

    payload = {
        "symbol": SYMBOL,
        "side": side,
        "type": "MARKET",
        "quantity": qty
    }
    # reduceOnly só é aceito em algumas rotas/futuros; se der erro, cai sem ele
    if reduce_only:
        payload["reduceOnly"] = "true"

    # Retries leves
    for attempt in range(3):
        try:
            order = client.new_order(**payload)
            log(f"✅ {action} OK:", order)
            return {"ok": True, "action": action, "qty": qty}
        except Exception as e:
            msg = str(e)
            log(f"⚠️ new_order erro (tentativa {attempt+1}/3):", msg)
            # Pequeno backoff
            time.sleep(0.6 + attempt * 0.6)
            # se erro mencionar reduceOnly inválido, remove e tenta de novo
            if "reduceOnly" in msg and reduce_only:
                payload.pop("reduceOnly", None)

    return {"ok": False, "error": "new_order_failed"}

# ------------------ Worker de execução ------------------
def order_worker():
    global last_exec_ms
    while True:
        action = order_q.get()  # bloqueia até ter item
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

# start worker
threading.Thread(target=order_worker, daemon=True).start()

# ------------------ WebSocket (confirmações) ------------------
def start_user_ws():
    # Apenas para logar fills/atualizações (não envia ordens)
    def on_msg(_, msg):
        # Loga eventos relevantes
        if isinstance(msg, dict):
            et = msg.get("e")
            if et in ("ORDER_TRADE_UPDATE", "ACCOUNT_UPDATE"):
                log("📡 WS:", json.dumps(msg))
    try:
        ws = UMFuturesWebsocketClient()
        # listenKey privado
        lk = client.new_listen_key()["listenKey"]
        ws.user_data(listen_key=lk, id=1, callback=on_msg)
        log("🔌 WebSocket user-data conectado.")
        # Mantém o listenKey vivo a cada ~30 min
        def keepalive():
            while True:
                try:
                    client.renew_listen_key(listenKey=lk)
                except Exception as e:
                    log("⚠️ renew_listen_key:", e)
                time.sleep(30*60)
        threading.Thread(target=keepalive, daemon=True).start()
    except Exception as e:
        log("⚠️ Falha ao abrir WS (segue só com REST):", e)

threading.Thread(target=start_user_ws, daemon=True).start()

# ------------------ HTTP endpoints ------------------
@app.route("/health")
def health():
    return jsonify({"ok": True})

@app.route("/", methods=["POST"])
def webhook():
    try:
        data = json.loads(request.data or "{}")
        action = data.get("action")
        log("🚨 ALERTA RECEBIDO:", action)

        if action not in ("buy", "sell", "stop_buy", "stop_sell"):
            return jsonify({"status": "invalid_action"}), 400

        # deduplicação curtinha por ação
        t = now_ms()
        if action in last_seen and (t - last_seen[action]) < DEDUP_WINDOW_MS:
            log("⛔️ Ignorado (dedupe):", action)
            return jsonify({"status": "ignored_dedupe"}), 200
        last_seen[action] = t

        # Enfileira e responde imediatamente
        order_q.put(action)
        return jsonify({"status": "queued", "action": action}), 200

    except Exception as e:
        log("❌ Erro geral webhook:", e)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
