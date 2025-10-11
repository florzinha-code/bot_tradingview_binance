from flask import Flask, request, jsonify
from binance.client import Client
import json
import os

app = Flask(__name__)

# 🟢 Suas chaves Binance
API_KEY = "Jh33OLr6yEDfPCujosXuCnMsmCPg7Mh0R1HJ7j8PvQFfBkrQtSwFGAjIH8w9vanU"
API_SECRET = "WMi3P0kOUQbjg5NAXKlNHRdaGFGW7GVXoOG0G0517wWTrTZLT2MaGMcqr2gzwFxT"

client = Client(API_KEY, API_SECRET)

@app.route('/', methods=['POST'])
def webhook():
    data = json.loads(request.data)
    print("📩 Alerta recebido:", data)

    if data['action'] == 'buy':
        order = client.order_market_buy(symbol='BTCUSDT', quantity=0.001)
        print(order)
        return jsonify({'status': 'Buy order executed'})

    elif data['action'] == 'sell':
        order = client.order_market_sell(symbol='BTCUSDT', quantity=0.001)
        print(order)
        return jsonify({'status': 'Sell order executed'})

    else:
        return jsonify({'status': 'Invalid action'})

if __name__ == '__main__':
    app.run()
