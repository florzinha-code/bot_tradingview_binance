from flask import Flask, request, jsonify
from binance.client import Client
import json, os, math

app = Flask(__name__)

# 🟢 Chaves Binance (defina em "Environment" no Render)
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

client = Client(API_KEY, API_SECRET)

# ===============================
# 🔹 Função para calcular 100% do saldo disponível
# ===============================
def get_full_balance_qty(symbol="BTCUSDT"):
    try:
        balance = float(client.futures_account_balance()[1]["balance"])  # USDT balance
        ticker = float(client.futures_mark_price(symbol=symbol)["markPrice"])
        qty = balance / ticker  # converte saldo USDT para quantidade BTC
        return round_qty(symbol, qty)
    except Exception as e:
        print("Erro ao calcular quantidade:", e)
        return 0.0

# 🔹 Arredondamento da quantidade mínima válida
def round_qty(symbol, qty):
    info = client.futures_exchange_info()
    lot = next(s for s in info["symbols"] if s["symbol"] == symbol)
    step = float(next(f["stepSize"] for f in lot["filters"] if f["filterType"] == "LOT_SIZE"))
    return math.floor(qty / step) * step

# ===============================
# 🔹 Rota principal (webhook)
# ===============================
@app.route('/', methods=['POST'])
def webhook():
    try:
        data = json.loads(request.data or "{}")
        action = (data.get("action") or "").lower()
        symbol = "BTCUSDT"
        qty = get_full_balance_qty(symbol)

        if qty <= 0:
            return jsonify({"status": "❌ Saldo insuficiente"}), 400

        if action == "buy":
            order = client.futures_create_order(
                symbol=symbol, side="BUY", type="MARKET", quantity=qty
            )
            return jsonify({"status": "✅ BUY executada", "qty": qty, "orderId": order["orderId"]})

        elif action == "sell":
            order = client.futures_create_order(
                symbol=symbol, side="SELL", type="MARKET", quantity=qty
            )
            return jsonify({"status": "✅ SELL executada", "qty": qty, "orderId": order["orderId"]})

        elif action == "stop_buy":
            order = client.futures_create_order(
                symbol=symbol, side="BUY", type="MARKET", quantity=qty, reduceOnly=True
            )
            return jsonify({"status": "✅ CLOSE SHORT (stop_buy)", "qty": qty, "orderId": order["orderId"]})

        elif action == "stop_sell":
            order = client.futures_create_order(
                symbol=symbol, side="SELL", type="MARKET", quantity=qty, reduceOnly=True
            )
            return jsonify({"status": "✅ CLOSE LONG (stop_sell)", "qty": qty, "orderId": order["orderId"]})

        else:
            return jsonify({"status": "❌ Ação inválida"}), 400

    except Exception as e:
        print("⚠️ Erro no webhook:", e)
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
