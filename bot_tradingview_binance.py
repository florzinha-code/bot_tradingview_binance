from flask import Flask, request, jsonify
from binance.um_futures import UMFutures
import json, os

app = Flask(__name__)

# 🔑 Chaves da Binance (Render)
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
        print(f"💰 Saldo FUTUROS USDT-M: {usdt_balance:.3f} USDT")

        if usdt_balance <= 5:
            return jsonify({"status": "❌ Saldo insuficiente"}), 400

        symbol = "BTCUSDT"
        leverage = 2
        margin_type = "CROSSED"

        # 🔧 Configurações Binance
        try:
            client.change_margin_type(symbol=symbol, marginType=margin_type)
        except Exception as e:
            if "No need to change margin type" not in str(e):
                print("⚠️ Erro marginType:", e)

        client.change_leverage(symbol=symbol, leverage=leverage)
        print(f"⚙️ Leverage definida: {leverage}x")

        # 📈 Preço atual
        price = float(client.ticker_price(symbol=symbol)['price'])
        print(f"💹 Preço atual: {price}")

        # 📦 Qtd Fixa (entrada)
        qty_entry = 0.002

        # === Consulta posição REAL ===
        positions = client.position_information(symbol=symbol)
        pos_amt = float(positions[0]["positionAmt"])
        pos_side = "LONG" if pos_amt > 0 else "SHORT" if pos_amt < 0 else "FLAT"

        print(f"📊 Posição atual: {pos_amt} BTC ({pos_side})")

        reduce_only = False
        side = None

        # === LÓGICA DE AÇÃO ===
        if action == "buy":
            side = "BUY"
            reduce_only = False
            qty_to_send = qty_entry

        elif action == "sell":
            side = "SELL"
            reduce_only = False
            qty_to_send = qty_entry

        elif action == "stop_buy":   # fechar SHORT
            side = "BUY"
            reduce_only = True
            qty_to_send = abs(pos_amt)

            if pos_amt >= 0:
                print("⚠️ STOP BUY mas não existe SHORT para fechar")
                return jsonify({"status": "❌ Sem posição SHORT"}), 400

        elif action == "stop_sell":  # fechar LONG
            side = "SELL"
            reduce_only = True
            qty_to_send = abs(pos_amt)

            if pos_amt <= 0:
                print("⚠️ STOP SELL mas não existe LONG para fechar")
                return jsonify({"status": "❌ Sem posição LONG"}), 400

        else:
            return jsonify({"status": "❌ Ação inválida"}), 400

        print(f"📦 Quantidade enviada: {qty_to_send} (reduceOnly={reduce_only})")

        # 🚀 ENVIA ORDEM
        order = client.new_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=qty_to_send,
            reduceOnly=reduce_only
        )

        print(f"✅ Ordem executada: {order}")
        return jsonify({"status": "ok"})

    except Exception as e:
        print("❌ Erro geral:", e)
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
