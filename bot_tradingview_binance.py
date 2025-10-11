from flask import Flask, request, jsonify
from binance.um_futures import UMFutures
import os, json

app = Flask(__name__)

# === CHAVES DA BINANCE ===
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

# === Cliente de FUTUROS USDT-M ===
client = UMFutures(key=API_KEY, secret=API_SECRET)

@app.route('/', methods=['POST'])
def webhook():
    try:
        data = json.loads(request.data)
        action = data.get("action")
        print(f"\n🚨 ALERTA RECEBIDO: {action}")

        # --- Verifica saldo na conta de FUTUROS USDT-M
        balances = client.balance()
        usdt_balance = 0
        for b in balances:
            if b.get("asset") == "USDT":
                usdt_balance = float(b.get("balance", 0))
        print(f"💰 Saldo FUTUROS USDT-M detectado: {usdt_balance} USDT")

        # --- Obtém preço do BTCUSDT
        ticker = client.ticker_price("BTCUSDT")
        price = float(ticker["price"])
        print(f"📈 Preço atual BTCUSDT: {price}")

        # --- Calcula tamanho da posição (100% do saldo)
        qty = round(usdt_balance / price, 3)
        if qty <= 0:
            return jsonify({"status": "❌ Saldo insuficiente (USDT=0)", "balance": usdt_balance}), 400

        print(f"📦 Quantidade calculada: {qty} BTC")

        # --- Envia ordem FUTUROS
        if action == "buy":
            order = client.new_order(symbol="BTCUSDT", side="BUY", type="MARKET", quantity=qty)
            print("✅ Ordem de COMPRA executada:", order)
            return jsonify({"status": "✅ Compra executada", "quantity": qty})

        elif action == "sell":
            order = client.new_order(symbol="BTCUSDT", side="SELL", type="MARKET", quantity=qty)
            print("✅ Ordem de VENDA executada:", order)
            return jsonify({"status": "✅ Venda executada", "quantity": qty})

        else:
            return jsonify({"status": "❌ Ação inválida recebida"}), 400

    except Exception as e:
        print(f"⚠️ Erro geral: {e}")
        return jsonify({"status": f"⚠️ Erro: {str(e)}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
