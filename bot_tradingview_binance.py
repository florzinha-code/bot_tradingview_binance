from flask import Flask, request, jsonify
from binance.client import Client
import json
import os

app = Flask(__name__)

# 🟢 Suas chaves Binance
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

client = Client(API_KEY, API_SECRET)

@app.route('/', methods=['POST'])
def webhook():
    data = json.loads(request.data)
    print("* Alerta recebido:", data)

    if data.get('action') == 'buy':
        order = client.order_market_buy(symbol='BTCUSDT', quantity=0.001)
        print(order)
        return jsonify({'status': '✅ Buy order executed'})

    elif data.get('action') == 'sell':
        order = client.order_market_sell(symbol='BTCUSDT', quantity=0.001)
        print(order)
        return jsonify({'status': '✅ Sell order executed'})

    else:
        return jsonify({'status': '❌ Invalid action'}), 400

except Exception as e:
    print("⚠️ Erro no webhook:", e)
    return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
