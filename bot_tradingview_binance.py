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
        leverage = 1
        margin_type = "CROSSED"  # modo Cross

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

        # 🚀 Define lado da ordem com suporte aos 4 tipos de ação
        if action in ('buy', 'stop_sell'):
            side = "BUY"
        elif action in ('sell', 'stop_buy'):
            side = "SELL"
        else:
            print("❌ Ação inválida:", action)
            return jsonify({"status": "❌ Ação inválida"}), 400

        # 📦 Tenta executar ordem com ajuste dinâmico de margem
        attempts = [0.85, 0.80, 0.75]
        order = None

        for p in attempts:
            qty = round((usdt_balance * p) / price, 4)
            if qty < 0.001:
                qty = 0.001
            try:
                order = client.new_order(symbol=symbol, side=side, type="MARKET", quantity=qty)
                print(f"✅ Ordem executada: {side} com {p*100:.0f}% do saldo ({qty} BTC)")
                break
            except Exception as e:
                if "Margin is insufficient" in str(e):
                    print(f"⚠️ Margem insuficiente com {p*100:.0f}%, tentando {int(p*100-5)}%...")
                    continue
                else:
                    print(f"❌ Erro inesperado: {e}")
                    raise e

        if not order:
            print("❌ Falha após 3 tentativas — saldo insuficiente.")
            return jsonify({"status": "❌ Margem insuficiente mesmo após ajustes"}), 400

        return jsonify({"status": f"✅ {side} executado", "qty": qty})

    except Exception as e:
        print("❌ Erro geral:", e)
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
