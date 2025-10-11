from flask import Flask, request, jsonify
from binance.um_futures import UMFutures
import os, json

app = Flask(__name__)

# 🔑 Chaves da Binance
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

# Cliente de Futuros USDT-M
client = UMFutures(key=API_KEY, secret=API_SECRET)

@app.route('/', methods=['POST'])
def webhook():
    try:
        data = json.loads(request.data)
        print("* Alerta recebido:", data)

        # 💰 Pega saldo disponível (em USDT)
        balance_info = client.balance()
        usdt_balance = float(next(b['balance'] for b in balance_info if b['asset'] == 'USDT'))
        print(f"Saldo disponível em USDT: {usdt_balance}")

        # 🧮 Calcula tamanho da posição (100% do saldo)
        price = float(client.ticker_price(symbol='BTCUSDT')['price'])
        qty = round(usdt_balance / price, 4)
        print(f"Tamanho calculado: {qty} BTC")

        # 🚀 Compra
        if data.get('action') == 'buy':
            order = client.new_order(symbol='BTCUSDT', side='BUY', type='MARKET', quantity=qty)
            print(order)
            return jsonify({'status': '✅ Compra executada', 'quantity': qty})

        # 🔻 Venda
        elif data.get('action') == 'sell':
            order = client.new_order(symbol='BTCUSDT', side='SELL', type='MARKET', quantity=qty)
            print(order)
            return jsonify({'status': '✅ Venda executada', 'quantity': qty})

        else:
            return jsonify({'status': '❌ Ação inválida'}), 400

    except Exception as e:
        print("⚠️ Erro:", e)
        return jsonify({'status': f'⚠️ {str(e)}'}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
