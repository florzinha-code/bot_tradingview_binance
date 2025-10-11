from flask import Flask, request, jsonify
from binance.um_futures import UMFutures
import json, os

app = Flask(__name__)

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

client = UMFutures(key=API_KEY, secret=API_SECRET)

@app.route('/', methods=['POST'])
def webhook():
    try:
        data = json.loads(request.data)
        action = data.get('action')
        print(f"📩 ALERTA RECEBIDO: {action}")

        # --- saldo disponível ---
        balance_data = client.balance()
        usdt_balance = float(next(b['balance'] for b in balance_data if b['asset'] == 'USDT'))
        print(f"💰 Saldo FUTUROS USDT-M detectado: {usdt_balance:.2f} USDT")

        # --- preço atual do BTC ---
        ticker = client.ticker_price(symbol="BTCUSDT")
        price = float(ticker["price"])
        print(f"📈 Preço atual BTCUSDT: {price}")

        # --- define modo isolado e alavancagem 1x ---
        client.change_margin_type(symbol="BTCUSDT", marginType="ISOLATED")
        client.change_leverage(symbol="BTCUSDT", leverage=1)

        # --- calcula quantidade: 99% do saldo disponível ---
        quantity = round((usdt_balance * 0.99) / price, 4)
        print(f"📊 Quantidade calculada: {quantity} BTC (≈99% do saldo)")

        if action == "buy":
            order = client.new_order(symbol="BTCUSDT", side="BUY", type="MARKET", quantity=quantity)
            print(order)
            return jsonify({"status": "✅ COMPRA executada", "quantidade": quantity})

        elif action == "sell":
            order = client.new_order(symbol="BTCUSDT", side="SELL", type="MARKET", quantity=quantity)
            print(order)
            return jsonify({"status": "✅ VENDA executada", "quantidade": quantity})

        else:
            return jsonify({"status": "❌ Ação inválida"}), 400

    except Exception as e:
        print("⚠️ Erro geral:", e)
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
