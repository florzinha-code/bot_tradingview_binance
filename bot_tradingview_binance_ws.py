# ws_bot_renko.py
# Bot WebSocket Binance Futures — Renko 550 pts + EMA9/21 + RSI14 + reversão = stop
# Requisitos:
#   pip install binance-futures-connector==4.0.0
#
# Variáveis de ambiente obrigatórias no Render:
#   API_KEY, API_SECRET
#
# Ajustes rápidos aqui em CONFIG, se quiser.

import os, math, time, threading, traceback
from collections import deque

from binance.um_futures import UMFutures
from binance.websocket.um_futures.websocket_client import UMFuturesWebsocketClient

# ===================== CONFIG =====================
SYMBOL          = "BTCUSDT"
LEVERAGE        = 1
MARGIN_TYPE     = "CROSSED"         # "ISOLATED" se preferir
QTY_PCT         = 0.85              # 85% do saldo USDT
BOX_POINTS      = 550.0             # Renko fixo 550 pts
REV_BOXES       = 2                 # reversão de 2 tijolos (Renko tradicional)
EMA_FAST        = 9
EMA_SLOW        = 21
RSI_LEN         = 14
RSI_WIN_LONG    = (40.0, 65.0)      # compra: 40..65
RSI_WIN_SHORT   = (35.0, 60.0)      # venda:  35..60
MIN_QTY         = 0.001             # mínimo aceito
# ===================== CONFIG =====================

API_KEY    = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

client = UMFutures(key=API_KEY, secret=API_SECRET)

# --- seta margem/alavancagem uma vez (tolerante a erro idempotente) ---
def setup_symbol():
    try:
        client.change_margin_type(symbol=SYMBOL, marginType=MARGIN_TYPE)
        print(f"✅ Modo de margem: {MARGIN_TYPE}")
    except Exception as e:
        if "No need to change margin type" in str(e):
            print("ℹ️ Margem já configurada.")
        else:
            print("⚠️ change_margin_type:", e)

    try:
        client.change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
        print(f"✅ Alavancagem: {LEVERAGE}x")
    except Exception as e:
        print("⚠️ change_leverage:", e)

# --- util: EMA incremental sobre série de fechamentos de tijolos ---
class EMA:
    def __init__(self, length:int):
        self.len = length
        self.mult = 2.0/(length+1.0)
        self.value = None

    def update(self, x:float):
        if self.value is None:
            self.value = x
        else:
            self.value = (x - self.value)*self.mult + self.value
        return self.value

# --- RSI (Wilder RMA) incremental em fechamentos de tijolos ---
class RSI_Wilder:
    def __init__(self, length:int):
        self.len = length
        self.avgU = None
        self.avgD = None
        self.prev = None

    def update(self, x:float):
        if self.prev is None:
            self.prev = x
            return None  # ainda inicializando
        ch = x - self.prev
        up = max(ch, 0.0)
        dn = max(-ch, 0.0)
        if self.avgU is None:
            # primeira “janela” – usa SMA do Wilder
            self.avgU = up
            self.avgD = dn
        else:
            self.avgU = (self.avgU*(self.len - 1) + up) / self.len
            self.avgD = (self.avgD*(self.len - 1) + dn) / self.len
        self.prev = x
        denom = max(self.avgD, 1e-12)
        rs = self.avgU / denom
        rsi = 100.0 - 100.0/(1.0+rs)
        return rsi

# --- motor de Renko fixo (pontos) com reversão de 2 tijolos ---
class RenkoEngine:
    def __init__(self, box_points:float, rev_boxes:int):
        self.box = float(box_points)
        self.rev = int(rev_boxes)
        self.anchor = None     # último fechamento CONFIRMADO de tijolo
        self.dir    = 0        # +1 alta, -1 baixa, 0 neutro
        self.last_brick_close = None
        self.brick_id = 0      # incrementa a cada tijolo novo

    def feed_price(self, px:float):
        """Alimenta com preço (ticks). Retorna lista de (brick_close, dir, brick_id) criados neste tick."""
        created = []
        if self.anchor is None:
            self.anchor = px
            self.dir = 0
            self.last_brick_close = px
            return created

        # continuação
        up_th   = self.anchor + self.box
        down_th = self.anchor - self.box

        # quantos cabem pro lado atual
        # cenário 1: continuação mesma direção → 1 tijolo
        # cenário 2: reversão tradicional → salta +/- rev*box de uma vez
        # No fluxo de preço tick a tick, testamos as duas bordas

        # Subiu o suficiente?
        while px >= up_th:
            # se já estávamos caindo, precisa cruzar reversão (rev boxes pra cima)
            if self.dir == -1:
                # reversão: novo anchor é anchor + box*rev
                self.anchor = self.anchor + self.box*self.rev
            else:
                # continuação
                self.anchor = self.anchor + self.box
            self.dir = 1
            self.brick_id += 1
            self.last_brick_close = self.anchor
            created.append( (self.anchor, self.dir, self.brick_id) )
            # recalcula limiar para caber múltiplos
            up_th = self.anchor + self.box
            down_th = self.anchor - self.box

        # Desceu o suficiente?
        while px <= down_th:
            if self.dir == 1:
                self.anchor = self.anchor - self.box*self.rev
            else:
                self.anchor = self.anchor - self.box
            self.dir = -1
            self.brick_id += 1
            self.last_brick_close = self.anchor
            created.append( (self.anchor, self.dir, self.brick_id) )
            up_th = self.anchor + self.box
            down_th = self.anchor - self.box

        return created

# --- Estado de estratégia (idêntico ao Pine) ---
class StrategyState:
    def __init__(self):
        self.in_long  = False
        self.in_short = False
        self.last_signal_brick = -1  # debouncer por brick_id

        # indicadores sobre fechamentos de tijolo
        self.ema_fast = EMA(EMA_FAST)
        self.ema_slow = EMA(EMA_SLOW)
        self.rsi      = RSI_Wilder(RSI_LEN)

    def update_indics(self, brick_close:float):
        e1 = self.ema_fast.update(brick_close)
        e2 = self.ema_slow.update(brick_close)
        rsi = self.rsi.update(brick_close)
        return e1, e2, rsi

# --- execução de ordens (REST) ---
def get_qty(price:float):
    bal = client.balance()
    usdt = next((float(b["balance"]) for b in bal if b["asset"]=="USDT"), 0.0)
    qty  = round(max(MIN_QTY, (usdt * QTY_PCT) / price), 3)
    return qty, usdt

def market_order(side:str, qty:float, reduce_only:bool=False):
    # Para stops que fecham posição, usamos reduceOnly=True pra não virar a mão sem querer
    params = dict(symbol=SYMBOL, side=side, type="MARKET", quantity=qty)
    if reduce_only:
        params["reduceOnly"] = "true"
    try:
        order = client.new_order(**params)
        print(f"✅ Ordem {side} qty={qty} reduceOnly={reduce_only}: {order}")
        return True
    except Exception as e:
        print("❌ Erro ao enviar ordem:", e)
        return False

# --- Lógica de sinal idêntica ao Pine (sobre fechamentos de tijolo) ---
def apply_logic_on_brick(state:StrategyState, brick_close:float, dir:int, brick_id:int):
    # evita reprocessar o mesmo brick
    # (mas permite várias execuções no MESMO segundo se vários bricks surgirem)
    # vamos permitir 1 ação por brick_id para cada direção de trade
    e1, e2, rsi = state.update_indics(brick_close)

    if e1 is None or e2 is None or rsi is None:
        print(f"… warmup EMA/RSI — brick {brick_id}")
        return

    price = float(client.ticker_price(symbol=SYMBOL)['price'])
    qty, usdt = get_qty(price)

    # Flags de “box verde/vermelho” comparando fechamento atual vs anterior do Renko
    # dir == +1 (tijolo de alta), dir == -1 (tijolo de baixa)
    renkoVerde    = (dir == +1)
    renkoVermelho = (dir == -1)

    # *** 1) STOP primeiro (prioridade) ***
    # stop da compra: 1º tijolo contrário é vermelho
    if state.in_long and renkoVermelho:
        print(f"🛑 STOP COMPRA no brick {brick_id} @{brick_close:.2f}")
        if market_order("SELL", qty, reduce_only=True):
            state.in_long = False

    # stop da venda: 1º tijolo contrário é verde
    if state.in_short and renkoVerde:
        print(f"🛑 STOP VENDA no brick {brick_id} @{brick_close:.2f}")
        if market_order("BUY", qty, reduce_only=True):
            state.in_short = False

    # *** 2) ENTRADAS (após aplicar stop) ***
    # Condições iguais ao Pine:
    # compra: reversão bullish (tijolo verde) E EMA9>EMA21 E RSI∈[40,65]
    can_long = renkoVerde and (e1 > e2) and (RSI_WIN_LONG[0] <= rsi <= RSI_WIN_LONG[1])
    # venda:  reversão bearish (tijolo vermelho) E EMA9<EMA21 E RSI∈[35,60]
    can_short = renkoVermelho and (e1 < e2) and (RSI_WIN_SHORT[0] <= rsi <= RSI_WIN_SHORT[1])

    # Não entrar se acabou de sair e ainda está processando o mesmo brick;
    # mas se saiu e ainda atende, vamos deixar entrar (igual seu Pine atual)
    if can_long and not state.in_long:
        print(f"🚀 COMPRA no brick {brick_id} @{brick_close:.2f} | EMA9={e1:.2f} EMA21={e2:.2f} RSI={rsi:.2f}")
        if market_order("BUY", qty):
            state.in_long = True
            state.in_short = False

    if can_short and not state.in_short:
        print(f"🔻 VENDA no brick {brick_id} @{brick_close:.2f} | EMA9={e1:.2f} EMA21={e2:.2f} RSI={rsi:.2f}")
        if market_order("SELL", qty):
            state.in_short = True
            state.in_long = False

# --- Loop WebSocket: usamos aggTrade (ticks agregados) para latência baixa ---
def run_ws():
    setup_symbol()
    state = StrategyState()
    renko = RenkoEngine(BOX_POINTS, REV_BOXES)

    ws = UMFuturesWebsocketClient()

    def on_msg(_, message):
        try:
            # aggTrade payload: {'e':'aggTrade','s':'BTCUSDT','p':'12345.67', ...}
            px = float(message.get("p") or message.get("c") or 0.0)
            if px <= 0:
                return
            created = renko.feed_price(px)
            # se um ou mais tijolos se formaram neste tick, processa cada um
            for (brick_close, d, brick_id) in created:
                apply_logic_on_brick(state, brick_close, d, brick_id)
        except Exception as e:
            print("⚠️ on_msg error:", e)
            traceback.print_exc()

    ws.start()
    # stream de trades agregados (latência bem baixa)
    ws.agg_trade(symbol=SYMBOL.lower(), id=1, callback=on_msg)
    print("▶️ WebSocket iniciado — ouvindo aggTrade", SYMBOL)

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        ws.stop()
        print("⏹️ WebSocket parado")

if __name__ == "__main__":
    run_ws()
