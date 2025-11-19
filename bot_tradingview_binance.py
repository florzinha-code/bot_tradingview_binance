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

        # 📦 Quantidade fixa para ENTRADA
        qty = 0.002
        print(f"📦 Quantidade de ENTRADA: {qty} BTC")

        # =======================
        # 🚨 LÓGICA DOS STOPS
        # =======================
        if action in ("stop_buy", "stop_sell"):
            print("🛑 STOP recebido, tentando fechar posição aberta...")

            # Pegamos a posição atual na Binance
            positions = client.get_position_risk(symbol=symbol)
            print(f"📊 Posições retornadas: {positions}")

            pos = None
            for p in positions:
                amt = float(p.get("positionAmt", "0"))
                if amt != 0.0:
                    pos = p
                    break

            if not pos:
                print("ℹ️ Nenhuma posição aberta para fechar.")
                return jsonify({"status": "ok", "info": "sem_posicao"}), 200

            position_amt = float(pos["positionAmt"])
            side_close = "SELL" if position_amt > 0 else "BUY"
            qty_close = abs(position_amt)

            print(f"🔒 Fechando {qty_close} BTC na direção {side_close} (reduceOnly=True)")

            order = client.new_order(
                symbol=symbol,
                side=side_close,
                type="MARKET",
                quantity=qty_close,
                reduceOnly=True
            )

            print(f"✅ Ordem de STOP executada → {order}")
            return jsonify({"status": "ok", "closed_qty": qty_close})

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

        print(f"✅ Ordem de ENTRADA executada: {side} → {order}")
        return jsonify({"status": "ok"})

    except Exception as e:
        print("❌ Erro geral:", e)
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
