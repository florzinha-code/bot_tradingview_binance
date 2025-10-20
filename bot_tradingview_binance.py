from flask import Flask, request, jsonify
from binance.um_futures import UMFutures
import json, os, time

app = Flask(__name__)

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
client = UMFutures(key=API_KEY, secret=API_SECRET)

# Controle de flood — 1 ordem a cada 10s
last_action_time = 0
MIN_INTERVAL = 10  # segundos

@app.route('/', methods=['POST'])
def webhook():
    global last_action_time
    try:
        data = json.loads(request.data)
        action = data.get('action')
        print(f"🚨 ALERTA RECEBIDO: {action}")

        now = time.time()
        if now - last_action_time < MIN_INTERVAL:
            print("⚠️ Ignorado: requisição em intervalo muito curto.")
            return jsonify({"status": "ignored_flood"}), 429
        last_action_time = now

        symbol = "BTCUSDT"

        # Apenas 1 chamada leve (reduz 3 RESTs)
        price = float(client.ticker_price(symbol=symbol)['price'])
        balance = client.balance()
        usdt_balance = next((float(b['balance']) for b in balance if b['asset'] == 'USDT'), 0.0)
        if usdt_balance <= 5:
            return jsonify({"status": "❌ Saldo insuficiente"}), 400

        qty = max(round((usdt_balance * 0.85) / price, 3), 0.001)
        print(f"💰 Saldo: {usdt_balance:.2f} | Preço: {price} | Qty: {qty}")

        # Evita reconfigurar margem e alavancagem em toda ordem
        try:
            client.change_margin_type(symbol=symbol, marginType="CROSSED")
        except Exception:
            pass
        try:
            client.change_leverage(symbol=symbol, leverage=1)
        except Exception:
            pass

        # Execução principal
        sides = {
            "buy": ("BUY", "✅ Buy"),
            "sell": ("SELL", "✅ Sell"),
            "stop_buy": ("SELL", "🛑 Stop BUY"),
            "stop_sell": ("BUY", "🛑 Stop SELL"),
        }

        if action not in sides:
            return jsonify({"status": "❌ Ação inválida"}), 400

        side, msg = sides[action]
        order = client.new_order(symbol=symbol, side=side, type="MARKET", quantity=qty)
        print(f"{msg} executado:", order)
        return jsonify({"status": msg, "qty": qty})

    except Exception as e:
        print("❌ Erro geral:", e)
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
