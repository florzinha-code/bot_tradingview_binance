# ws_bot_renko.py / bot_tradingview_binance_ws.py
# Binance Futures WebSocket — Renko 550 pts + EMA9/21 + RSI14 + reversão = stop
# Versão 24/7 com:
# ✅ PnL detalhado (USDT + %)
# ✅ Reconexão instantânea (0.1s)
# ✅ Heartbeat a cada 30s
# ✅ Debounce de ordens no mesmo brick
# ✅ Auto-relogin se a sessão cair
# ✅ Logs coloridos Render-friendly
# ✅ DEBUG AVANÇADO (explica por que entrou ou não entrou)

import os, time, math, json, traceback, functools, threading
from binance.um_futures import UMFutures
from binance.websocket.um_futures.websocket_client import UMFuturesWebsocketClient

# ========== CORES ==========
RESET = "\033[0m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
CYAN = "\033[96m"
GRAY = "\033[90m"

# ========== LOGS FLUSH IMEDIATOS ==========
print = functools.partial(print, flush=True)

# ========== CONFIG ==========
SYMBOL = "BTCUSDT"
LEVERAGE = 1
MARGIN_TYPE = "CROSSED"
QTY_PCT = 0.85
BOX_POINTS = 550.0
REV_BOXES = 2
EMA_FAST = 9
EMA_SLOW = 21
RSI_LEN = 14
RSI_WIN_LONG = (40.0, 65.0)
RSI_WIN_SHORT = (35.0, 60.0)
MIN_QTY = 0.001
NO_TICK_RESTART_S = 120
HEARTBEAT_S = 30
# ==========================================

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

def get_client():
  return UMFutures(key=API_KEY, secret=API_SECRET)

client = get_client()

# --- Setup de símbolo ---
def setup_symbol():
  try:
      client.change_margin_type(symbol=SYMBOL, marginType=MARGIN_TYPE)
      print(f"{GREEN}✅ Modo de margem: {MARGIN_TYPE}{RESET}")
  except Exception as e:
      if "No need to change margin type" in str(e):
          print(f"{CYAN}ℹ️ Margem já configurada.{RESET}")
      else:
          print(f"{YELLOW}⚠️ change_margin_type:{RESET}", e)
  try:
      client.change_leverage(symbol=SYMBOL, leverage=LEVERAGE)
      print(f"{GREEN}✅ Alavancagem: {LEVERAGE}x{RESET}")
  except Exception as e:
      print(f"{YELLOW}⚠️ change_leverage:{RESET}", e)

# --- EMA ---
class EMA:
  def __init__(self, length):
      self.len = length
      self.mult = 2.0 / (length + 1.0)
      self.value = None
  def update(self, x):
      self.value = x if self.value is None else (x - self.value) * self.mult + self.value
      return self.value

# --- RSI (Wilder) ---
class RSI_Wilder:
  def __init__(self, length):
      self.len = length
      self.avgU = self.avgD = self.prev = None
  def update(self, x):
      if self.prev is None:
          self.prev = x
          return None
      ch = x - self.prev
      up, dn = max(ch, 0), max(-ch, 0)
      if self.avgU is None:
          self.avgU, self.avgD = up, dn
      else:
          self.avgU = (self.avgU*(self.len - 1) + up) / self.len
          self.avgD = (self.avgD*(self.len - 1) + dn) / self.len
      self.prev = x
      rs = self.avgU / max(self.avgD, 1e-12)
      return 100 - 100 / (1 + rs)

# --- Renko Engine ---
class RenkoEngine:
  def __init__(self, box_points, rev_boxes):
      self.box = float(box_points)
      self.rev = int(rev_boxes)
      self.anchor = None
      self.dir = 0
      self.brick_id = 0
  def feed_price(self, px):
      created = []
      if self.anchor is None:
          self.anchor = px
          return created
      up_th = self.anchor + self.box
      down_th = self.anchor - self.box
      while px >= up_th:
          self.anchor += self.box if self.dir != -1 else self.box * self.rev
          self.dir = 1
          self.brick_id += 1
          created.append((self.anchor, self.dir, self.brick_id))
          up_th = self.anchor + self.box
          down_th = self.anchor - self.box
      while px <= down_th:
          self.anchor -= self.box if self.dir != 1 else self.box * self.rev
          self.dir = -1
          self.brick_id += 1
          created.append((self.anchor, self.dir, self.brick_id))
          up_th = self.anchor + self.box
          down_th = self.anchor - self.box
      return created

# --- Estado da estratégia ---
class StrategyState:
  def __init__(self):
      self.in_long = False
      self.in_short = False
      self.ema_fast = EMA(EMA_FAST)
      self.ema_slow = EMA(EMA_SLOW)
      self.rsi = RSI_Wilder(RSI_LEN)
      self.last_order_key = None
  def update_indics(self, brick_close):
      return (
          self.ema_fast.update(brick_close),
          self.ema_slow.update(brick_close),
          self.rsi.update(brick_close)
      )

# --- Quantidade dinâmica ---
def get_qty(price):
  bal = client.balance()
  usdt = next((float(b["balance"]) for b in bal if b["asset"] == "USDT"), 0.0)
  qty = round(max(MIN_QTY, (usdt * QTY_PCT) / price), 3)
  return qty, usdt

# --- Exibir PnL ---
def show_pnl():
  try:
      pos_data = client.get_position_risk(symbol=SYMBOL)
      if pos_data:
          pos = pos_data[0]
          pnl = float(pos.get("unRealizedProfit", 0))
          entry = float(pos.get("entryPrice", 0))
          amt = float(pos.get("positionAmt", 0))
          side = "LONG" if amt > 0 else "SHORT" if amt < 0 else "FLAT"
          if amt != 0 and entry > 0:
              mark = float(pos.get("markPrice", 0))
              pct = ((mark - entry) / entry * 100) * (1 if amt > 0 else -1)
          else:
              pct = 0.0
          color = GREEN if pnl >= 0 else RED
          print(f"{color}💰 PnL: {pnl:.2f} USDT | Δ={pct:.3f}% | Entrada: {entry:.2f} | Qty: {amt:.4f} | {side}{RESET}")
  except Exception as e:
      print(f"{YELLOW}⚠️ Erro ao obter PnL:{RESET}", e)

# --- Execução de ordens ---
def market_order(side, qty, reduce_only=False):
  params = dict(symbol=SYMBOL, side=side, type="MARKET", quantity=qty)
  if reduce_only:
      params["reduceOnly"] = "true"
  try:
      client.new_order(**params)
      print(f"{GREEN}✅ Ordem {side} qty={qty} reduceOnly={reduce_only}{RESET}")
      time.sleep(0.2)
      show_pnl()
      return True
  except Exception as e:
      print(f"{RED}❌ Erro ao enviar ordem:{RESET}", e)
      return False

# --- Extração de preço ---
def extract_price(message):
  if isinstance(message, str):
      try: message = json.loads(message)
      except Exception: return 0.0
  val = message.get("p") or message.get("c") or message.get("price")
  try: return float(val)
  except Exception: return 0.0

# --- Lógica principal (com DEBUG AVANÇADO) ---
def apply_logic_on_brick(state, brick_close, dir, brick_id):
  e1, e2, rsi = state.update_indics(brick_close)
  if e1 is None or e2 is None or rsi is None:
      print(f"{GRAY}[DEBUG] Indicadores insuficientes ainda (EMA/RSI não completos){RESET}")
      return

  price = float(client.ticker_price(symbol=SYMBOL)['price'])
  qty, _ = get_qty(price)

  renkoVerde = dir == 1
  renkoVermelho = dir == -1
  def order_key(side): return f"{side}_{brick_id}_{round(brick_close)}"

  # DEBUG: status geral
  print(f"{GRAY}📊 [DEBUG] Brick#{brick_id} Dir={'▲' if renkoVerde else '▼'} | EMA9={e1:.2f} EMA21={e2:.2f} RSI={rsi:.2f} | in_long={state.in_long} in_short={state.in_short}{RESET}")

  # Stops
  if state.in_long and renkoVermelho:
      print(f"{YELLOW}[DEBUG] Possível STOP de COMPRA detectado{RESET}")
      key = order_key("STOP_LONG")
      if key != state.last_order_key:
          if market_order("SELL", qty, True):
              state.in_long = False
              state.last_order_key = key

  if state.in_short and renkoVerde:
      print(f"{YELLOW}[DEBUG] Possível STOP de VENDA detectado{RESET}")
      key = order_key("STOP_SHORT")
      if key != state.last_order_key:
          if market_order("BUY", qty, True):
              state.in_short = False
              state.last_order_key = key

  # Entradas
  long_ok = renkoVerde and (e1 > e2) and (RSI_WIN_LONG[0] <= rsi <= RSI_WIN_LONG[1]) and not state.in_long
  short_ok = renkoVermelho and (e1 < e2) and (RSI_WIN_SHORT[0] <= rsi <= RSI_WIN_SHORT[1]) and not state.in_short
  print(f"{GRAY}[DEBUG] Condições: long_ok={long_ok} short_ok={short_ok}{RESET}")

  # Motivos de bloqueio
  if renkoVerde and not (RSI_WIN_LONG[0] <= rsi <= RSI_WIN_LONG[1]):
      print(f"{YELLOW}⛔ Long bloqueado: RSI fora do range ({rsi:.2f}){RESET}")
  if renkoVermelho and not (RSI_WIN_SHORT[0] <= rsi <= RSI_WIN_SHORT[1]):
      print(f"{YELLOW}⛔ Short bloqueado: RSI fora do range ({rsi:.2f}){RESET}")
  if (renkoVerde and e1 <= e2):
      print(f"{YELLOW}⛔ Long bloqueado: EMA9 <= EMA21{RESET}")
  if (renkoVermelho and e1 >= e2):
      print(f"{YELLOW}⛔ Short bloqueado: EMA9 >= EMA21{RESET}")
  if state.last_order_key and (state.last_order_key.startswith("BUY") or state.last_order_key.startswith("SELL")):
      print(f"{GRAY}⏸️ Debounce ativo — última ordem {state.last_order_key}{RESET}")

  if long_ok:
      key = order_key("BUY")
      if key != state.last_order_key:
          print(f"{GREEN}🚀 COMPRA #{brick_id} @ {brick_close:.2f}{RESET}")
          if market_order("BUY", qty):
              state.in_long, state.in_short = True, False
              state.last_order_key = key

  if short_ok:
      key = order_key("SELL")
      if key != state.last_order_key:
          print(f"{CYAN}🔻 VENDA #{brick_id} @ {brick_close:.2f}{RESET}")
          if market_order("SELL", qty):
              state.in_short, state.in_long = True, False
              state.last_order_key = key

# --- WebSocket principal ---
def run_ws():
  setup_symbol()
  while True:
      state = StrategyState()
      renko = RenkoEngine(BOX_POINTS, REV_BOXES)
      ws = UMFuturesWebsocketClient()
      last_tick_ts = time.time()

      def on_msg(_, message):
          nonlocal last_tick_ts
          try:
              px = extract_price(message)
              if px > 0:
                  last_tick_ts = time.time()
                  for brick_close, d, brick_id in renko.feed_price(px):
                      print(f"{MAGENTA}🧱 Brick {brick_id} {'▲' if d == 1 else '▼'} close={brick_close:.2f}{RESET}")
                      apply_logic_on_brick(state, brick_close, d, brick_id)
          except Exception as e:
              print(f"{YELLOW}⚠️ on_msg error:{RESET}", e)
              traceback.print_exc()

      def heartbeat():
          while True:
              time.sleep(HEARTBEAT_S)
              try:
                  ws.ping()
                  uptime = round(time.time() - start_ts, 1)
                  print(f"{MAGENTA}💓 Heartbeat enviado (ping {HEARTBEAT_S}s) | Uptime: {uptime}s{RESET}")
              except Exception:
                  break

      start_ts = time.time()
      try:
          ws.agg_trade(symbol=SYMBOL.lower(), id=1, callback=on_msg)
          threading.Thread(target=heartbeat, daemon=True).start()
          print(f"{BLUE}▶️ WS assinado em aggTrade {SYMBOL}. Aguardando ticks…{RESET}")
      except Exception as e:
          print(f"{RED}❌ Erro ao abrir WS:{RESET}", e)

      try:
          while True:
              time.sleep(5)
              if time.time() - last_tick_ts > NO_TICK_RESTART_S:
                  print(f"{YELLOW}⏱️ {NO_TICK_RESTART_S}s sem tick. Reiniciando WS…{RESET}")
                  try: ws.stop()
                  except Exception: pass
                  break
      except Exception as e:
          print(f"{YELLOW}⚠️ Loop principal caiu:{RESET}", e)
          traceback.print_exc()
          try: ws.stop()
          except Exception: pass

      print(f"{CYAN}⚡ Reabrindo WS agora… (instantâneo){RESET}")
      time.sleep(0.1)

if __name__ == "__main__":
  run_ws()
