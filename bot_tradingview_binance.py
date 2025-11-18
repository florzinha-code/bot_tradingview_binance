from flask import Flask, request, jsonify
from binance.um_futures import UMFutures
import json, os, math

app = Flask(__name__)

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

client = UMFutures(key=API_KEY, secret=API_SECRET)

@app.route('/', methods=['POST'])
def webhook():
    try:
        data = json.loads(request.data)
        action = data.get('action')
        print(f"🚨 ALERTA RECEBIDO: {action}")

        # 💰 Saldo
        balance = client.balance()
        usdt_balance = next(
            (float(b['balance']) for b in balance if b['asset'] == 'USDT'),
            0.0
        )
        print(f"💰 Saldo FUTUROS USDT-M detectado: {usdt_balance:.3f} USDT")

        if usdt_balance <= 5:
            return jsonify({"status": "❌ Saldo insuficiente"}), 400

        symbol = "BTCUSDT"
        leverage = 2
        margin_type = "CROSSED"

        # 🔧 Define modo de margem e alavancagem
        try:
            client.change_margin_type(symbol=symbol, marginType=margin_type)
            print("✅ Modo de margem definido como CROSS")
        except Exception as e:
            if "No need to change margin type" in str(e):
                print("ℹ️ Margem já está CROSS.")
            else:
                print("⚠️ Erro ao mudar margem:", e)

        client.change_leverage(symbol=symbol, leverage=leverage)
        print(f"⚙️ Alavancagem definida: {leverage}x")

        # 📈 Preço atual
        price = float(client.ticker_price(symbol=symbol)['price'])
        print(f"💹 Preço atual BTCUSDT: {price}")

        # 📦 Quantidade
        qty = 0.002

        print(f"📦 Quantidade final enviada: {qty} BTC")

        # =======================
        # 🚨 LÓGICA NOVA DOS STOPS
        # =======================

        # STOP DE COMPRA → fecha posição BUY
        if action == "stop_buy":
            print("🔻 Fechando posição de COMPRA com closePosition=True")
            order = client.new_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                closePosition=True
            )
            print(f"✅ Ordem STOP BUY executada → {order}")
            return jsonify({"status": "ok"})

        # STOP DE VENDA → fecha posição SELL
        if action == "stop_sell":
            print("🔻 Fechando posição de VENDA com closePosition=True")
            order = client.new_order(
                symbol=symbol,
                side="BUY",
                type="MARKET",
                closePosition=True
            )
            print(f"✅ Ordem STOP SELL executada → {order}")
            return jsonify({"status": "ok"})

        # =======================
        # 🚀 ENTRADAS NORMAIS
        # =======================
        if action == "buy":
            side = "BUY"
        elif action == "sell":
            side = "SELL"
        else:
            return jsonify({"status": "❌ Ação inválida"}), 400

        order = client.new_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=qty
        )

        print(f"✅ Ordem executada: {side} → {order}")
        return jsonify({"status": "ok"})

    except Exception as e:
        print("❌ Erro geral:", e)
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
