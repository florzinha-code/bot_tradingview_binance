from flask import Flask, request, jsonify
from binance.client import Client
import json
import os

app = Flask(__name__)

# 🟢 Suas chaves Binance (configure no Render → Environment)
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

client = Client(API_KEY, API_SECRET)

@app.route('/', methods=['POST'])
def webhook():
    try:
        data = json.loads(request.data)
        print("* Alerta recebido:", data)

        action = data.get('action')

        if action == 'buy':
            # Compra com 99,9% do saldo USDT
            balance = client.get_asset_balance(asset='USDT')
            usdt_amount = float(balance['free'])
            ticker = client.get_symbol_ticker(symbol='BTCUSDT')
            btc_price = float(ticker['price'])
            quantity = round((usdt_amount / btc_price) * 0.999, 6)

            if quantity < 0.0001:
                return jsonify({'status': '⚠️ Saldo insuficiente para compra', 'quantity': quantity}), 400

            order = client.order_market_buy(symbol='BTCUSDT', quantity=quantity)
            print(order)
            return jsonify({'status': '✅ Buy order executed', 'quantity': quantity})

        elif action == 'sell':
            # Venda com 99,9% do saldo BTC
            balance = client.get_asset_balance(asset='BTC')
            btc_amount = float(balance['free'])
            quantity = round(btc_amount * 0.999, 6)

            if quantity < 0.0001:
                return jsonify({'status': '⚠️ Saldo insuficiente para venda', 'quantity': quantity}), 400

            order = client.order_market_sell(symbol='BTCUSDT', quantity=quantity)
            print(order)
            return jsonify({'status': '✅ Sell order executed', 'quantity': quantity})

        else:
            return jsonify({'status': '❌ Invalid action'}), 400

    except Exception as e:
        print("⚠️ Erro no webhook:", e)
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
