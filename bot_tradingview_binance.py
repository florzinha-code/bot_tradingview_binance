from flask import Flask, request, jsonify
from binance.um_futures import UMFutures
import os, json

app = Flask(__name__)

# 🔑 Suas chaves Binance (devem estar configuradas no Render)
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

# Conecta ao cliente de Futuros USDT-M
client = UMFutures(key=API_KEY, secret=API_SECRET)

@app.route('/', methods=['POST'])
def webhook():
    try:
        data = json.loads(request.data)
        print("\n🚨 Alerta recebido:", data)

        # --- DEBUG: printa todas as informações de saldo
        balance_info = client.balance()
        print("\n📊 Resultado bruto do client.balance():")
        print(json.dumps(balance_info, indent=4))

        # Procura saldo em USDT
        usdt_balance = float(next((b['balance'] for b in balance_info if b['asset'] == 'USDT'), 0))
        print(f"\n💰 Saldo disponível (USDT): {usdt_balance}")

        # Pega preço atual do BTCUSDT
        ticker = client.ticker_price(symbol='BTCUSDT')
        price = float(ticker['price'])
        print(f"📈 Preço atual BTCUSDT: {price}")

        # Calcula tamanho da ordem com 100% do saldo
        qty = round(usdt_balance / price, 4)
        print(f"📏 Quantidade calculada: {qty} BTC")

        # --- Ações
        action = data.get('action')

        if action == 'buy':
            print("🟢 Enviando ordem de COMPRA (LONG)")
            order = client.new_order(symbol='BTCUSDT', side='BUY', type='MARKET', quantity=qty)
            print("📦 Retorno da Binance:", order)
            return jsonify({'status': '✅ Compra executada', 'quantity': qty})

        elif action == 'sell':
            print("🔴 Enviando ordem de VENDA (SHORT)")
            order = client.new_order(symbol='BTCUSDT', side='SELL', type='MARKET', quantity=qty)
            print("📦 Retorno da Binance:", order)
            return jsonify({'status': '✅ Venda executada', 'quantity': qty})

        else:
            print("⚠️ Ação inválida recebida:", action)
            return jsonify({'status': '❌ Ação inválida'}), 400

    except Exception as e:
        print("\n❗ ERRO DETECTADO:", e)
        return jsonify({'status': f'⚠️ Erro: {str(e)}'}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
