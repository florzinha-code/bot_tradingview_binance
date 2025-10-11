from flask import Flask, request, jsonify
from binance.um_futures import UMFutures
import json, os

app = Flask(__name__)

# 🔑 Suas chaves da Binance (definidas no Render Environment)
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

client = UMFutures(key=API_KEY, secret=API_SECRET)

@app.route('/', methods=['POST'])
def webhook():
    try:
        data = json.loads(request.data)
        action = data.get('action')
        print(f"🚨 ALERTA RECEBIDO: {action}")

        # Saldo disponível
        balance = client.balance()
        usdt_balance = next(
            (float(b['balance']) for b in balance if b['asset'] == 'USDT'), 0.0)
        print(f"💰 Saldo FUTUROS USDT-M detectado: {usdt_balance:.3f} USDT")

        if usdt_balance <= 5:
            return jsonify({"status": "❌ Saldo insuficiente"}), 400

        # Configurações básicas
        symbol = "BTCUSDT"
        leverage = 1
        margin_type = "ISOLATED"

        # Verifica e define margem isolada somente se necessário
        try:
            info = client.get_position_mode()
            client.change_margin_type(symbol=symbol, marginType=margin_type)
        except Exception as e:
            if "No need to change margin type" not in str(e):
                print("⚠️ Erro ao alterar tipo de margem:", e)

        client.change_leverage(symbol=symbol, leverage=leverage)

        # Preço atual do BTC
        ticker = client.ticker_price(symbol=symbol)
        price = float(ticker['price'])
        print(f"💹 Preço atual BTCUSDT: {price}")

        # Calcula quantidade (99% do saldo disponível)
        qty = round((usdt_balance * 0.99) / price, 4)
        print(f"📦 Quantidade calculada: {qty} BTC")

        # Execução da ordem
        if action == 'buy':
            order = client.new_order(symbol=symbol, side="BUY",
                                     type="MARKET", quantity=qty)
            print("✅ Ordem de COMPRA enviada:", order)
            return jsonify({"status": "✅ Buy executado", "qty": qty})

        elif action == 'sell':
            order = client.new_order(symbol=symbol, side="SELL",
                                     type="MARKET", quantity=qty)
            print("✅ Ordem de VENDA enviada:", order)
            return jsonify({"status": "✅ Sell executado", "qty": qty})

        else:
            return jsonify({"status": "❌ Ação inválida"}), 400

    except Exception as e:
        print("❌ Erro geral:", e)
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
