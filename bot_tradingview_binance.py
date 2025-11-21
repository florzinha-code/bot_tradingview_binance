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
        usar_pct = 0.85  # 85% do saldo

        # ==========================================
        # 🔧 CONFIG BASE
        # ==========================================
        try:
            client.change_margin_type(symbol=symbol, marginType="CROSSED")
        except Exception:
            pass

        client.change_leverage(symbol=symbol, leverage=leverage)

        # 💰 Saldo
        balance = client.balance()
        usdt_balance = next((float(b['balance']) for b in balance if b['asset'] == 'USDT'), 0.0)

        price = float(client.ticker_price(symbol=symbol)['price'])

        # ==========================================
        # 📦 QUANTIDADE DINÂMICA → 85% COM 2X
        # ==========================================
        position_value = usdt_balance * usar_pct * leverage  # valor em USDT da posição
        qty = position_value / price

        # Arredonda para 4 casas
        qty = math.floor(qty * 10000) / 10000

        # 🔒 mínimo para ordem (~$100)
        min_qty = 100 / price
        if qty < min_qty:
            qty = min_qty

        print(f"💰 Saldo USDT: {usdt_balance}")
        print(f"🔗 Exposição: {position_value} USDT (2x sobre 85%)")
        print(f"📦 Quantidade final enviada: {qty} BTC")

        # ==========================================
        # 🛑 STOP (FECHA QUALQUER POSIÇÃO)
        # ==========================================
        if action in ("stop", "stop_buy", "stop_sell"):
            print("🛑 Fechando posição...")

            positions = client.get_position_risk()
            pos = next((p for p in positions if p["symbol"] == symbol and float(p["positionAmt"]) != 0), None)

            if not pos:
                print("ℹ️ Sem posição aberta.")
                return jsonify({"status": "ok", "info": "no_position"})

            position_amt = float(pos["positionAmt"])
            side_close = "SELL" if position_amt > 0 else "BUY"
            qty_close = abs(position_amt)

            order = client.new_order(
                symbol=symbol,
                side=side_close,
                type="MARKET",
                quantity=qty_close
            )

            print(f"✅ STOP EXECUTADO → {order}")
            return jsonify({"status": "ok", "closed": qty_close})

        # ==========================================
        # 🚀 ENTRADAS
        # ==========================================
        if action == "buy":
            side = "BUY"
        elif action == "sell":
            side = "SELL"
        else:
            return jsonify({"status": "erro", "msg": "ação inválida"}), 400

        print(f"📌 ENTRADA → {side} {qty} BTC")

        order = client.new_order(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=qty
        )

        print(f"✅ ENTRADA EXECUTADA → {order}")
        return jsonify({"status": "ok", "side": side, "qty": qty})

    except Exception as e:
        print("❌ ERRO GERAL:", e)
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
