from flask import Flask, request, jsonify
from binance.um_futures import UMFutures
import json, os

app = Flask(__name__)

# 🔑 Chaves da Binance (Render Environment)
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

client = UMFutures(key=API_KEY, secret=API_SECRET)

@app.route('/', methods=['POST'])
def webhook():
    try:
        data = json.loads(request.data)
        action = data.get('action')
        print(f"🚨 ALERTA RECEBIDO: {action}")

        # 💰 Consulta saldo
        balance = client.balance()
        usdt_balance = next(
            (float(b['balance']) for b in balance if b['asset'] == 'USDT'),
            0.0
        )
        print(f"💰 Saldo FUTUROS USDT-M detectado: {usdt_balance:.3f} USDT")

        if usdt_balance <= 5:
            return jsonify({"status": "❌ Saldo insuficiente"}), 400

        symbol = "BTCUSDT"
        leverage = 2          # <<<<<<<<<< ALAVANCAGEM DEFINIDA AQUI
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

        # 📦 Quantidade FIXA = 0,002 BTC
        qty = 0.002
        print(f"📦 Quantidade final enviada: {qty} BTC")

        # === LÓGICA DE ORDENS COM REDUCE-ONLY ===
        reduce_only = False
        side = None

        if action == "buy":
            side = "BUY"
            reduce_only = False
        elif action == "sell":
            side = "SELL"
            reduce_only = False
        elif action == "stop_buy":
            side = "BUY"
            reduce_only = True
        elif action == "stop_sell":
            side = "SELL"
            reduce_only = True
        else:
            return jsonify({"status": "❌ Ação inválida"}), 400

        # 🚀 Executa ordem
        order = client.new_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=qty,
            reduceOnly=reduce_only
        )

        print(f"✅ Ordem executada: {side} (reduceOnly={reduce_only}) → {order}")
        return jsonify({"status": "ok"})

    except Exception as e:
        print("❌ Erro geral:", e)
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
