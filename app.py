from flask import Flask, request, jsonify
from binance import Client
import os
import logging
import time
import math
import threading

# === CONFIGURACIÓN GLOBAL ===
closed_trades = []
active_positions = set()  # Para controlar operaciones concurrentes
position_lock = threading.Lock()

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

def log_signal(data, status, error=None):
    """Guarda cada señal recibida y su estado"""
    symbol = data.get("symbol", "unknown").upper()
    action = data.get("action", "unknown").upper()
    msg = f"Señal recibida: {symbol} | Acción: {action} | Estado: {status}"
    if error:
        msg += f" | Error: {error}"
    logging.info(msg)
    print(msg)

def cancel_open_orders(symbol, max_retries=3):
    """Cancela todas las órdenes abiertas para un símbolo con reintento"""
    retries = 0
    while retries < max_retries:
        try:
            client.futures_cancel_all_open_orders(symbol=symbol)
            print(f"✅ Órdenes abiertas canceladas para {symbol}")
            return True
        except Exception as e:
            retries += 1
            print(f"⚠️ Error cancelando órdenes para {symbol}: {str(e)}, reintentando ({retries}/{max_retries})")
            time.sleep(1)
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
POSITION_PERCENT = 0.05  # 5% del balance disponible
MODE_ONEWAY = True

# === FUNCIONES AUXILIARES ===
def get_balance_usdt():
    """Obtiene el balance disponible en USDT"""
    try:
        balances = client.futures_account_balance()
        for b in balances:
            if b['asset'] == 'USDT':
                return float(b['availableBalance'])
        return 0.0
    except Exception as e:
        print("❌ Error al obtener balance:", e)
        return 0.0

def get_open_position(symbol, max_retries=3):
    """Devuelve la cantidad actual abierta con reintento"""
    retries = 0
    while retries < max_retries:
        try:
            position_info = client.futures_position_information(symbol=symbol)
            if not position_info or len(position_info) == 0:
                print(f"ℹ️ No hay posición abierta para {symbol}")
                return 0.0
            qty = float(position_info[0]['positionAmt'])
            print(f"📊 Posición actual en {symbol}: {qty}")
            return abs(qty)
        except Exception as e:
            retries += 1
            print(f"⚠️ Error obteniendo posición para {symbol}: {str(e)}, reintentando ({retries}/{max_retries})")
            time.sleep(1)
    return 0.0

def get_step_size_precision(symbol):
    """Obtiene la cantidad de decimales permitidos por el par"""
    try:
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
        precision = max(0, precision)  # Evitar precisiones negativas
        return precision, step_size
    except Exception as e:
        print(f"⚠️ Error obteniendo precisión para {symbol}: {str(e)}")
        return 0, 0

def get_quantity(symbol):
    investment = get_balance_usdt() * POSITION_PERCENT * LEVERAGE
    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        price = float(ticker['price'])
        precision, _ = get_step_size_precision(symbol)
        qty = investment / price
        final_qty = round(qty, precision)
        if final_qty <= 0:
            raise ValueError(f"Cantidad calculada es cero o negativa: {final_qty}")
        print(f"✅ Cantidad calculada para {symbol}: {final_qty}")
        return final_qty, precision
    except Exception as e:
        print(f"❌ Error obteniendo cantidad para {symbol}: {str(e)}")
        return 0.0, 0

def close_position(symbol, max_retries=3):
    """Cierra cualquier posición abierta con reintento"""
    retries = 0
    while retries < max_retries:
        try:
            position_info = client.futures_position_information(symbol=symbol)
            if not position_info or len(position_info) == 0:
                print(f"ℹ️ No hay posición abierta para {symbol}")
                return 0.0
            qty = float(position_info[0]['positionAmt'])
            if qty != 0:
                order_side = "SELL" if qty > 0 else "BUY"
                client.futures_create_order(
                    symbol=symbol,
                    side=order_side,
                    type="MARKET",
                    quantity=abs(qty),
                    reduceOnly=True
                )
                print(f"✅ Posición cerrada en {symbol}")
                return abs(qty)
            else:
                return 0.0
        except Exception as e:
            retries += 1
            print(f"⚠️ Error cerrando posición en {symbol}: {str(e)}, reintentando ({retries}/{max_retries})")
            time.sleep(1)
    return 0.0

def calculate_pnl(entry_price, exit_price, qty, is_long):
    """Calcula la ganancia/pérdida neta (PnL) en USDT"""
    if qty <= 0:
        return 0.0
    if is_long:
        pnl = (exit_price - entry_price) * qty
    else:
        pnl = (entry_price - exit_price) * qty
    return round(pnl, 2)

def create_stop_loss_order(symbol, sl_price, precision):
    """Crea una orden STOP_MARKET para el Stop Loss"""
    try:
        qty = get_open_position(symbol)
        if qty <= 0:
            print(f"⚠️ Cantidad inválida para crear SL: {qty}")
            return None
        # Validar precio
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
    except Exception as e:
        print(f"⚠️ Error creando Stop Loss para {symbol}: {str(e)}")
        return None

def create_trailing_stop_order(symbol, qty, callback_rate=1.2):  # Cambiado a 1.2%
    """Crea una orden TRAILING_STOP_MARKET para el Stop Loss"""
    try:
        order_side = "SELL" if qty > 0 else "BUY"
        tsl_order = client.futures_create_order(
            symbol=symbol,
            side=order_side,
            type="TRAILING_STOP_MARKET",
            quantity=qty,
            reduceOnly=True,
            callbackRate=callback_rate  # Más estable
        )
        print(f"🔁 Trailing Stop creado para {symbol} con callback rate {callback_rate}%")
        return tsl_order
    except Exception as e:
        print(f"⚠️ Error creando Trailing Stop para {symbol}: {str(e)}")
        return None

def create_take_profit_order(symbol, tp_price, precision, data=None):
    """Crea una orden LIMIT para Take Profit"""
    try:
        qty = get_open_position(symbol)
        if qty <= 0:
            print(f"⚠️ No hay posición abierta para {symbol}")
            return None
        half_qty = round(qty / 2, precision)
        if half_qty <= 0:
            print(f"⚠️ Cantidad inválida para TP: {half_qty}")
            log_signal(data, "Cantidad inválida", error=f"Cerrar parcial con qty={half_qty}")
            return None
        # Validar precio
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
            price=round(tp_price, precision),
            timeInForce="GTC"
        )
        print(f"📈 Take Profit creado en {tp_price} para {symbol}")
        return order
    except Exception as e:
        print(f"⚠️ Error creando Take Profit en {symbol}: {str(e)}")
        return None

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
            # ... [resto del código sin cambios] ...
            
        elif action in ["BUY", "SELL"]:
            # ... [resto del proceso de entrada] ...

            # Ejemplo simplificado de cómo asegurar la apertura
            current_pos = get_open_position(symbol)
            if current_pos <= 0:
                time.sleep(2)
                current_pos = get_open_position(symbol)
                if current_pos <= 0:
                    log_signal(data, "❌ No se confirmó la posición tras la entrada")
                    return

            # ... [continúa resto del monitoreo TP/SL] ...

    finally:
        with position_lock:
            if symbol in active_positions:
                active_positions.remove(symbol)

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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)