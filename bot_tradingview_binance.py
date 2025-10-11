from flask import Flask, request, jsonify
from binance.client import Client
import json, os, math

app = Flask(__name__)

# Chaves da Binance (Render → Environment: API_KEY / API_SECRET)
API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

# Quantidade padrão (em BTC). Ajuste aqui se quiser.
DEFAULT_QTY = float(os.getenv("QTY", "0.001"))

client = Client(API_KEY, API_SECRET)

# Utilitário: arredonda qty aos filtros do BTCUSDT perpétuo
def round_qty(symbol: str, qty: float) -> float:
    info = client.futures_exchange_info()
    lot = next(s for s in info["symbols"] if s["symbol"] == symbol)
    step = float(next(f["stepSize"] for f in lot["filters"] if f["filterType"] == "LOT_SIZE"))
    # arredonda para múltiplo de step
    return math.floor(qty / step) * step

@app.route('/', methods=['POST'])
def webhook():
    try:
        data = json.loads(request.data or "{}")
        action = (data.get("action") or "").lower()
        qty = float(data.get("qty") or DEFAULT_QTY)
        symbol = "BTCUSDT"
        qty = round_qty(symbol, qty)

        if qty <= 0:
            return jsonify({"error": "Quantidade inválida"}), 400

        # Ações:
        # buy/sell → abre posição (one-way mode)
        # stop_buy/stop_sell → fecha posição no lado oposto (reduceOnly)
        if action == "buy":
            order = client.futures_create_order(
                symbol=symbol, side="BUY", type="MARKET", quantity=qty
            )
            return jsonify({"status": "✅ Futures BUY executada", "quantity": qty, "orderId": order["orderId"]})

        elif action == "sell":
            order = client.futures_create_order(
                symbol=symbol, side="SELL", type="MARKET", quantity=qty
            )
            return jsonify({"status": "✅ Futures SELL executada", "quantity": qty, "orderId": order["orderId"]})

        elif action == "stop_buy":   # fecha short
            order = client.futures_create_order(
                symbol=symbol, side="BUY", type="MARKET", quantity=qty, reduceOnly="true"
            )
            return jsonify({"status": "✅ CLOSE SHORT (reduceOnly BUY)", "quantity": qty, "orderId": order["orderId"]})

        elif action == "stop_sell":  # fecha long
            order = client.futures_create_order(
                symbol=symbol, side="SELL", type="MARKET", quantity=qty, reduceOnly="true"
            )
            return jsonify({"status": "✅ CLOSE LONG (reduceOnly SELL)", "quantity": qty, "orderId": order["orderId"]})

        else:
            return jsonify({"error": "❌ action inválida. Use buy/sell/stop_buy/stop_sell"}), 400

    except Exception as e:
        print("⚠️ Erro no webhook:", e)
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
