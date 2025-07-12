from flask import Flask, request, jsonify
from binance.websocket.futures.websocket_client import FuturesWebsocketClient
from binance import Client
import os
import logging
import time
import math
import threading

# === CONFIGURACIÓN GLOBAL ===
closed_trades = []
active_positions = set()
position_lock = threading.Lock()

# Para mantener estado local de órdenes y posiciones
positions = {}  # {symbol: qty}
open_orders = {}  # {symbol: [order_ids]}

# === INICIALIZAR FLASK Y BINANCE CLIENT ===
app = Flask(__name__)
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
if not api_key or not api_secret:
    raise ValueError("Faltan claves API de Binance")
client = Client(api_key, api_secret)

# Configuración del logging
logging.basicConfig(
    filename='webhook_server.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# === FUNCIONES AUXILIARES ===
def log_signal(data, status, error=None):
    symbol = data.get("symbol", "unknown").upper()
    action = data.get("action", "unknown").upper()
    msg = f"Señal recibida: {symbol} | Acción: {action} | Estado: {status}"
    if error:
        msg += f" | Error: {error}"
    logging.info(msg)
    print(msg)

def binance_api_call(func, max_retries=5, base_delay=1):
    retries = 0
    while retries < max_retries:
        try:
            return func()
        except Exception as e:
            if any(err in str(e).lower() for err in ["rate limit", "503", "timeout"]):
                wait = base_delay * (2 ** retries)
                print(f"⏳ Reintentando en {wait}s por error temporal: {str(e)}")
                retries += 1
            else:
                handle_binance_error(e, func.__name__)
                return None
    return None

def handle_binance_error(e, context=""):
    error_code = getattr(e, 'code', None)
    error_msg = getattr(e, 'message', str(e))
    print(f"⚠️ Error en Binance [{context}]: Código {error_code}, Mensaje: {error_msg}")
    log_signal({}, f"Error en Binance: {error_msg}", error=str(e))

def cancel_open_orders(symbol):
    def _cancel():
        return client.futures_cancel_all_open_orders(symbol=symbol)
    response = binance_api_call(_cancel)
    if response:
        print(f"✅ Órdenes abiertas canceladas para {symbol}")
        return True
    return False

# === AJUSTE DE TIEMPO CON BINANCE ===
try:
    res = client.get_server_time()
    server_time = res['serverTime']
    local_time = int(time.time() * 1000)
    client.time_offset = server_time - local_time
    print(f"✅ Offset de tiempo ajustado: {client.time_offset} ms")
except Exception as e:
    print(f"⚠️ No se pudo ajustar el tiempo: {e}")

# === CONFIGURACIÓN DEL BOT ===
PARES_PERMITIDOS = ["FETUSDT", "GRTUSDT", "AIUSDT", "SONICUSDT", "DOTUSDT", "BAKEUSDT"]
LEVERAGE = 20
POSITION_PERCENT = 0.05
MODE_ONEWAY = True

# === FUNCIONES DE TRADING ===
def get_balance_usdt():
    def _get_balance():
        balances = client.futures_account_balance()
        for b in balances:
            if b['asset'] == 'USDT':
                return float(b['availableBalance'])
        return 0.0
    result = binance_api_call(_get_balance)
    return result if result is not None else 0.0

def get_step_size_precision(symbol):
    def _get_info():
        info = client.futures_exchange_info()
        symbol_data = next((item for item in info['symbols'] if item['symbol'] == symbol), None)
        if not symbol_data:
            raise ValueError(f"{symbol} no encontrado en Binance Futures")
        step_size_str = None
        for f in symbol_data['filters']:
            if 'stepSize' in f:
                step_size_str = f['stepSize']
                break
        if not step_size_str:
            raise ValueError(f"No se encontró stepSize para {symbol}")
        step_size = float(step_size_str)
        precision = int(round(-math.log(step_size, 10), 0))
        precision = max(0, precision)
        return precision, step_size
    result = binance_api_call(_get_info)
    return result if result else (0, 0)

def get_quantity(symbol):
    investment = get_balance_usdt() * POSITION_PERCENT * LEVERAGE
    if investment <= 0:
        print("❌ Inversión calculada es cero o negativa")
        return 0.0, 0

    def _get_quantity():
        ticker = client.futures_symbol_ticker(symbol=symbol)
        price = float(ticker['price'])
        precision, step_size = get_step_size_precision(symbol)
        qty = investment / price
        final_qty = round(qty / step_size) * step_size
        if final_qty < step_size:
            raise ValueError(f"Cantidad menor al mínimo permitido {step_size}: {final_qty}")
        print(f"✅ Cantidad calculada para {symbol}: {final_qty} (Precisión: {precision}, Step Size: {step_size})")
        return final_qty, precision
    result = binance_api_call(_get_quantity)
    return result if result else (0.0, 0)

# === CONEXIÓN WEBSOCKET ===
futures_manager = FuturesWebsocketManager(api_key=api_key, api_secret=api_secret)

def handle_message(msg):
    print("Mensaje WebSocket:", msg)
    
    # Actualizar estado local según evento
    if msg['e'] == 'ORDER_TRADE_UPDATE':
        order_data = msg['o']
        symbol = order_data['s']
        order_id = order_data['i']
        qty = float(order_data['q'])
        price = float(order_data['p'])
        side = order_data['S']
        status = order_data['X']
        
        if status == 'FILLED':
            if order_data['o'] == 'LIMIT':
                print(f"🎯 TP alcanzado en {symbol} a {price}")
            elif order_data['o'] == 'STOP_MARKET':
                print(f"🛑 SL alcanzado en {symbol} a {price}")
            elif order_data['o'] == 'TRAILING_STOP_MARKET':
                print(f"🔁 Trailing Stop activado en {symbol} a {price}")
            
            # Actualizar posición local
            if side == 'SELL':
                positions[symbol] = max(0, positions.get(symbol, 0) - qty)
            else:
                positions[symbol] = max(0, positions.get(symbol, 0) + qty)
            
            # Eliminar orden del estado local
            if symbol in open_orders:
                open_orders[symbol] = [oid for oid in open_orders[symbol] if oid != order_id]

    elif msg['e'] == 'ACCOUNT_UPDATE':
        update_data = msg['a']
        for asset in update_data['B']:
            if asset['a'] == 'USDT':
                print(f"💰 Balance actualizado: {asset['wb']} USDT")
        for position in update_data['P']:
            symbol = position['s']
            qty = float(position['pa'])
            positions[symbol] = qty
            print(f"📊 Posición actualizada en {symbol}: {qty}")

listen_key = client.futures_stream_get_listen_key()
futures_manager.start()
futures_manager.future_user_data(listen_key=listen_key, callback=handle_message)

# === LÓGICA DE TRADING ===
def create_stop_loss_order(symbol, sl_price, precision):
    def _create_sl():
        qty = positions.get(symbol, 0)
        if qty <= 0:
            print(f"⚠️ Cantidad inválida para crear SL: {qty}")
            return None
        if sl_price <= 0:
            raise ValueError(f"Precio de Stop Loss inválido: {sl_price}")
        order_side = "SELL" if qty > 0 else "BUY"
        sl_order = client.futures_create_order(
            symbol=symbol,
            side=order_side,
            type="STOP_MARKET",
            quantity=round(qty, precision),
            stopPrice=round(sl_price, precision),
            reduceOnly=True
        )
        print(f"🛑 Stop Loss creado en {sl_price} para {symbol}")
        return sl_order
    result = binance_api_call(_create_sl)
    return result

def create_trailing_stop_order(symbol, qty, callback_rate=1.2):
    def _create_tsl():
        if qty <= 0:
            print(f"⚠️ Cantidad inválida para trailing stop: {qty}")
            return None
        order_side = "SELL" if qty > 0 else "BUY"
        tsl_order = client.futures_create_order(
            symbol=symbol,
            side=order_side,
            type="TRAILING_STOP_MARKET",
            quantity=qty,
            reduceOnly=True,
            callbackRate=callback_rate
        )
        print(f"🔁 Trailing Stop creado para {symbol} con callback rate {callback_rate}%")
        return tsl_order
    result = binance_api_call(_create_tsl)
    return result

def create_take_profit_order(symbol, tp_price, precision, data=None):
    def _create_tp():
        qty = positions.get(symbol, 0)
        if qty <= 0:
            print(f"⚠️ No hay posición abierta en {symbol}")
            return None
        half_qty = round(qty / 2, precision)
        if half_qty <= 0:
            print(f"⚠️ Cantidad inválida para TP: {half_qty}")
            log_signal(data, "Cantidad inválida", error=f"Cerrar parcial con qty={half_qty}")
            return None
        if tp_price <= 0:
            raise ValueError(f"Precio de Take Profit inválido: {tp_price}")
        rounded_tp_price = round(tp_price, precision)
        if rounded_tp_price <= 0:
            raise ValueError(f"Precio de TP redondeado inválido: {rounded_tp_price}")
        order = client.futures_create_order(
            symbol=symbol,
            side="SELL" if half_qty > 0 else "BUY",
            type="LIMIT",
            quantity=half_qty,
            price=rounded_tp_price,
            timeInForce="GTC"
        )
        print(f"📈 Take Profit creado en {tp_price} para {symbol}")
        return order
    result = binance_api_call(_create_tp)
    return result

def confirm_position_opened(symbol, expected_qty):
    time.sleep(3)  # Esperar actualización por WebSocket
    current_pos = positions.get(symbol, 0)
    if current_pos >= expected_qty * 0.95:
        print(f"✅ Posición confirmada: {current_pos} en {symbol}")
        return True
    print("❌ No se pudo confirmar la posición tras la entrada")
    return False

def close_position(symbol):
    current_pos = positions.get(symbol, 0)
    if current_pos == 0:
        print(f"ℹ️ No hay posición abierta para {symbol}")
        return 0.0
    order_side = "SELL" if current_pos > 0 else "BUY"
    def _close():
        return client.futures_create_order(
            symbol=symbol,
            side=order_side,
            type="MARKET",
            quantity=abs(current_pos),
            reduceOnly=True
        )
    result = binance_api_call(_close)
    if result:
        print(f"✅ Posición cerrada en {symbol}")
        positions[symbol] = 0
        return abs(current_pos)
    return 0.0

# === PROCESO DE SEÑALES ===
def process_signal(data):
    symbol = data.get("symbol", "").upper().replace("BINANCE:", "").replace(".P", "")
    action = data.get("action", "").upper()

    with position_lock:
        if symbol in active_positions:
            log_signal(data, "⚠️ Señal ignorada por operación en curso")
            return
        active_positions.add(symbol)

    try:
        if action in ["EXIT BUY", "EXIT SELL"]:
            cancel_open_orders(symbol)
            closed_qty = close_position(symbol)
            if closed_qty > 0:
                log_signal(data, f"✅ Posición cerrada: {closed_qty} unidades")
            else:
                log_signal(data, "ℹ️ No había posición abierta")

        elif action in ["BUY", "SELL"]:
            entry_price = float(data.get("entry"))
            take_profit_price = float(data.get("tp"))
            stop_loss_price = float(data.get("sl"))

            def _change_leverage():
                return client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
            binance_api_call(_change_leverage)

            if MODE_ONEWAY:
                close_position(symbol)
                time.sleep(1)
                if positions.get(symbol, 0) != 0:
                    log_signal(data, f"⚠️ No se pudo cerrar posición previa: {positions.get(symbol, 0)}")
                    return

            qty, precision = get_quantity(symbol)
            if qty <= 0:
                log_signal(data, "Cantidad inválida", error=f"Valor calculado: {qty}")
                return

            if positions.get(symbol, 0) > 0:
                log_signal(data, f"⚠️ Ya hay posición abierta en {symbol}, cancelando nueva entrada")
                return

            if action == "BUY":
                def _buy():
                    return client.futures_create_order(symbol=symbol, side="BUY", type="MARKET", quantity=qty)
                response = binance_api_call(_buy)
                if not response:
                    log_signal(data, "Error al enviar orden BUY")
                    return
            elif action == "SELL":
                def _sell():
                    return client.futures_create_order(symbol=symbol, side="SELL", type="MARKET", quantity=qty)
                response = binance_api_call(_sell)
                if not response:
                    log_signal(data, "Error al enviar orden SELL")
                    return

            if not confirm_position_opened(symbol, qty):
                log_signal(data, "❌ No se confirmó la posición tras la entrada")
                return

            create_trailing_stop_order(symbol, qty)
            create_stop_loss_order(symbol, stop_loss_price, precision)

            partial_tp_price = entry_price + abs(take_profit_price - entry_price) * 0.618 if action == "BUY" else entry_price - abs(entry_price - take_profit_price) * 0.618
            half_qty = round(qty / 2, precision)
            print(f"🎯 TP objetivo: {take_profit_price}")
            print(f"🟡 TP parcial: {partial_tp_price}")
            print(f"🛡️ SL objetivo: {stop_loss_price}")

            timeout = 30 * 60
            start_time = time.time()
            partial_tp_executed = False

            while time.time() - start_time < timeout:
                current_pos = positions.get(symbol, 0)
                if current_pos == 0:
                    print("ℹ️ Posición completamente cerrada (SL o cierre externo).")
                    remaining_qty = qty - half_qty
                    if remaining_qty > 0:
                        pnl_final = calculate_pnl(entry_price, (take_profit_price + entry_price) / 2, remaining_qty, action == "BUY")
                        closed_trades.append({
                            "symbol": symbol,
                            "action": action,
                            "entry": entry_price,
                            "exit": (take_profit_price + entry_price) / 2,
                            "qty": remaining_qty,
                            "pnl": pnl_final,
                            "timestamp": time.time(),
                            "reason": "Posición cerrada totalmente (SL o cierre externo)"
                        })
                        log_signal(data, f"Posición cerrada totalmente, PnL: {pnl_final} USDT")
                    break
                time.sleep(5)

            else:
                print("⏰ Tiempo de espera agotado. Verifica manualmente la posición.")
                log_signal(data, "Tiempo de espera agotado para TP/SL")

    except Exception as e:
        log_signal(data, "Error crítico", error=str(e))
    finally:
        with position_lock:
            if symbol in active_positions:
                active_positions.remove(symbol)

# === RUTAS FLASK ===
@app.route('/')
def home():
    return jsonify({"status": "ok", "message": "Servidor activo"}), 200

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    print("Se recibió señal:", data)
    threading.Thread(target=process_signal, args=(data,)).start()
    return jsonify({"status": "received"}), 200

@app.route('/stats', methods=['GET'])
def stats():
    try:
        with open('webhook_server.log', 'r') as f:
            logs = f.readlines()
        total_pnl = sum(t["pnl"] for t in closed_trades)
        wins = sum(1 for t in closed_trades if t["pnl"] > 0)
        losses = sum(1 for t in closed_trades if t["pnl"] < 0)
        win_rate = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
        return jsonify({
            "total_signals": len(logs),
            "total_positions_closed": len(closed_trades),
            "total_pnl": round(total_pnl, 2),
            "win_rate": round(win_rate, 2),
            "last_10_logs": [log.strip() for log in logs[-10:]],
            "last_5_trades": closed_trades[-5:]
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# === INICIAR SERVIDOR ===
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)