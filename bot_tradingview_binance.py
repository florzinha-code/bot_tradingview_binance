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

        symbol = "BTCUSDT"
        leverage = 2

        # ==========================================
        # 🔧 DEFINIR MODO E ALAVANCAGEM
        # ==========================================
        try:
            client.change_margin_type(symbol=symbol, marginType="CROSSED")
        except Exception:
            pass

        client.change_leverage(symbol=symbol, leverage=leverage)

        # ==========================================
        # 💰 PEGA SALDO E CALCULA QTD DINÂMICA
        # ==========================================
        balance = client.balance()
        usdt_balance = next((float(b['balance']) for b in balance if b['asset'] == 'USDT'), 0.0)

        price = float(client.ticker_price(symbol=symbol)['price'])
        qty = math.floor((usdt_balance * 0.85 / price) * 1000) / 1000

        if qty < 0.001:
            qty = 0.001

        print(f"💹 Preço BTCUSDT: {price}")
        print(f"💰 Saldo: {usdt_balance} USDT")
        print(f"📦 Quantidade calculada: {qty} BTC")

        # ==========================================
        # 🛑 STOP → FECHA QUALQUER POSIÇÃO ABERTA
        # ==========================================
        if action in ("stop_buy", "stop_sell", "stop"):
            print("🔍 Consultando posição aberta...")
            positions = client.get_position_risk()
            pos = next((p for p in positions if p["symbol"] == symbol and float(p["positionAmt"]) != 0), None)

            if not pos:
                print("ℹ️ Nenhuma posição aberta para fechar.")
                return jsonify({"status": "ok", "info": "sem_posicao"})

            position_amt = float(pos["positionAmt"])
            qty_close = abs(position_amt)
            side_close = "SELL" if position_amt > 0 else "BUY"

            print(f"🔒 Fechando {qty_close} BTC → lado {side_close}")

            order = client.new_order(
                symbol=symbol,
                side=side_close,
                type="MARKET",
                quantity=qty_close
            )

            print(f"✅ POSIÇÃO FECHADA → {order}")
            return jsonify({"status": "ok", "closed": qty_close})

        # ==========================================
        # 🚀 ENTRADAS
        # ==========================================
        if action == "buy":
            side = "BUY"
        elif action == "sell":
            side = "SELL"
        else:
            return jsonify({"status": "❌ ação inválida"}), 400

        print(f"📌 ENTRADA → {side} {qty} BTC")

        order = client.new_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=qty
        )

        print(f"✅ ENTRADA EXECUTADA → {order}")
        return jsonify({"status": "ok", "side": side})

    except Exception as e:
        print("❌ ERRO GERAL:", e)
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
