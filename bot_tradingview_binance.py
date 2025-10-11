from flask import Flask, request, jsonify
from binance.um_futures import UMFutures
import json, os

app = Flask(__name__)

# 🔑 Chaves da Binance (configuradas no Render Environment)
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

client = UMFutures(key=API_KEY, secret=API_SECRET)

@app.route('/', methods=['POST'])
def webhook():
    try:
        data = json.loads(request.data)
        action = data.get('action')
        print(f"🚨 ALERTA RECEBIDO: {action}")

        # 📊 Consulta saldo
        balance = client.balance()
        usdt_balance = next(
            (float(b['balance']) for b in balance if b['asset'] == 'USDT'),
            0.0
        )
        print(f"💰 Saldo FUTUROS USDT-M detectado: {usdt_balance:.3f} USDT")

        if usdt_balance <= 5:
            return jsonify({"status": "❌ Saldo insuficiente"}), 400

        # ⚙️ Configurações do trade
        symbol = "BTCUSDT"
        leverage = 1
        margin_type = "ISOLATED"

        # 🧭 Define margem isolada (só altera se necessário)
        try:
            client.change_margin_type(symbol=symbol, marginType=margin_type)
            print("✅ Modo de margem definido como ISOLADO")
        except Exception as e:
            if "No need to change margin type" in str(e):
                print("ℹ️ Margem já está configurada como ISOLADA.")
            else:
                print("⚠️ Erro ao definir margem:", e)

        # 📈 Define alavancagem
        client.change_leverage(symbol=symbol, leverage=leverage)
        print(f"⚙️ Alavancagem ajustada para {leverage}x")

        # 💹 Preço atual BTC
        ticker = client.ticker_price(symbol=symbol)
        price = float(ticker['price'])
        print(f"💹 Preço atual BTCUSDT: {price}")

        # 📦 Quantidade calculada (99% do saldo, com limites automáticos)
        qty = round((usdt_balance * 0.99) / price, 4)

        # Respeita os limites mínimos e máximos da Binance
        if qty < 0.001:
            qty = 0.001  # mínimo aceito
        if qty > 0.0015:
            qty = 0.0015  # evita erro "position over maximum"
        print(f"📦 Quantidade ajustada: {qty} BTC")

        # 🚀 Executa a ordem
        if action == 'buy':
            order = client.new_order(symbol=symbol, side="BUY", type="MARKET", quantity=qty)
            print("✅ Ordem de COMPRA enviada:", order)
            return jsonify({"status": "✅ Buy executado", "qty": qty})

        elif action == 'sell':
            order = client.new_order(symbol=symbol, side="SELL", type="MARKET", quantity=qty)
            print("✅ Ordem de VENDA enviada:", order)
            return jsonify({"status": "✅ Sell executado", "qty": qty})

        else:
            print("❌ Ação inválida recebida:", action)
            return jsonify({"status": "❌ Ação inválida"}), 400

    except Exception as e:
        print("❌ Erro geral:", e)
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
