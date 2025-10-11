# === bot_tradingview_binance ===
# Flask + Binance Futures (USDT-M) webhook handler

from flask import Flask, request, jsonify
from binance.um_futures import UMFutures
import os
import json

app = Flask(__name__)

# === 🔐 Chaves da Binance ===
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

client = UMFutures(key=API_KEY, secret=API_SECRET)

# === 🧮 Função para calcular quantidade usando 100% da margem disponível ===
def get_max_qty(symbol):
    # saldo disponível em USDT na carteira de Futuros
    balance = float(next(b['balance'] for b in client.balance() if b['asset'] == 'USDT'))
    # preço de mercado atual do BTC/USDT
    price = float(client.ticker_price(symbol=symbol)['price'])
    # converte o saldo em quantidade de BTC (99% para evitar erro de arredondamento)
    qty = (balance / price) * 0.99
    return round(qty, 4)  # arredonda pra 4 casas decimais

# === 🧠 Webhook principal ===
@app.route('/', methods=['POST'])
def webhook():
    try:
        data = json.loads(request.data)
        print("🚨 Alerta recebido:", data)

        action = data.get('action')
        symbol = 'BTCUSDT'
        qty = get_max_qty(symbol)

        if qty <= 0:
            return jsonify({'status': '❌ Saldo insuficiente', 'quantity': qty}), 400

        if action == 'buy':
            order = client.new_order(symbol=symbol, side='BUY', type='MARKET', quantity=qty)
            print("✅ Ordem de COMPRA executada:", order)
            return jsonify({'status': '✅ Ordem de COMPRA executada', 'quantity': qty})

        elif action == 'sell':
            order = client.new_order(symbol=symbol, side='SELL', type='MARKET', quantity=qty)
            print("✅ Ordem de VENDA executada:", order)
            return jsonify({'status': '✅ Ordem de VENDA executada', 'quantity': qty})

        else:
            return jsonify({'status': '❌ Ação inválida', 'data': data}), 400

    except Exception as e:
        print("⚠️ Erro no webhook:", e)
        return jsonify({'error': str(e)}), 500


# === 🚀 Executa o servidor ===
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
