from flask import Flask, request, jsonify
from binance.um_futures import UMFutures
import json, os

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

        symbol = "BTCUSDT"
        leverage = 2
        qty = 0.002

        # ==========================
        # CONFIG BÁSICA
        # ==========================
        try:
            client.change_margin_type(symbol=symbol, marginType="CROSSED")
        except Exception:
            pass

        client.change_leverage(symbol=symbol, leverage=leverage)

        # ==========================
        # 🛑 STOP: FECHAR QUALQUER POSIÇÃO
        # ==========================
        if action in ("stop_buy", "stop_sell", "stop"):
            print("🔻 Fechando posição com closePosition=True")

            # FECHA VIA MARKET
            order = client.new_order(
                symbol=symbol,
                side="BUY",   # Binance ignora quando closePosition=True
                type="MARKET",
                closePosition=True
            )

            print(f"✅ STOP EXECUTADO → {order}")
            return jsonify({"status": "ok", "stop": True})

        # ==========================
        # 🚀 ENTRADAS NORMAIS
        # ==========================
        if action == "buy":
            side = "BUY"
        elif action == "sell":
            side = "SELL"
        else:
            return jsonify({"status": "❌ ação inválida"}), 400

        order = client.new_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=qty
        )

        print(f"✅ ENTRADA EXECUTADA: {side} → {order}")
        return jsonify({"status": "ok", "side": side})

    except Exception as e:
        print("❌ ERRO GERAL:", e)
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
