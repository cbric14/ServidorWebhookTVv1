from flask import Flask, request, jsonify
from binance import Client
import os
import logging
import time
import math

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

# Inicializar cliente de Binance
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")

if not api_key or not api_secret:
    raise ValueError("Faltan claves API de Binance")

client = Client(api_key, api_secret)

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
        print("Error al obtener balance:", e)
        return 0.0

def get_step_size_precision(symbol):
    """Obtiene la cantidad de decimales permitidos por el par"""
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
    return precision, step_size

def get_quantity(symbol):
    investment = get_balance_usdt() * POSITION_PERCENT
    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        price = float(ticker['price'])
        qty = investment / price

        precision, _ = get_step_size_precision(symbol)
        final_qty = round(qty, precision)

        print(f"✅ Cantidad calculada para {symbol}: {final_qty}")
        return final_qty, precision

    except Exception as e:
        print(f"❌ Error obteniendo cantidad para {symbol}: {str(e)}")
        return 0.0, 0

def close_position(symbol):
    """Cierra cualquier posición abierta"""
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
        print(f"⚠️ Error cerrando posición en {symbol}: {str(e)}")
        return 0.0

def create_stop_loss_order(symbol, sl_price, precision):
    """Crea una orden STOP_MARKET para el Stop Loss"""
    try:
        sl_order = client.futures_create_order(
            symbol=symbol,
            side="SELL" if "BUY" else "BUY",
            type="STOP_MARKET",
            quantity=round(close_position(symbol), precision),
            stopPrice=round(sl_price, precision),
            reduceOnly=True
        )
        print(f"🛑 Stop Loss creado en {sl_price} para {symbol}")
        return sl_order
    except Exception as e:
        print(f"⚠️ Error creando Stop Loss para {symbol}: {str(e)}")
        return None

def create_take_profit_order(symbol, tp_price, precision):
    """Crea una orden LIMIT para Take Profit"""
    try:
        half_qty = round(close_position(symbol) / 2, precision)

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

# === SERVIDOR FLASK ===
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({"status": "alive"}), 200

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    print("Se recibió señal:", data)

    try:
        # Limpiar y procesar símbolo
        symbol = data.get("symbol", "").upper().replace("BINANCE:", "").replace(".P", "")
        action = data.get("action", "").upper()

        # Convertir valores numéricos
        entry_price = float(data.get("entry", 0))
        take_profit_price = float(data.get("tp", 0))
        stop_loss_price = float(data.get("sl", 0))

    except ValueError as ve:
        log_signal(data, "Precios inválidos", error="TP, SL o ENTRY no son números")
        return jsonify({"status": "error", "message": "Valores inválidos en la señal"}), 400

    if symbol not in PARES_PERMITIDOS:
        log_signal(data, "Rechazado (par no permitido)")
        return jsonify({"status": "error", "message": "Par no permitido"}), 400

    if action not in ["BUY", "SELL", "EXIT BUY", "EXIT SELL"]:
        log_signal(data, "Acción desconocida")
        return jsonify({"status": "error", "message": "Acción desconocida"}), 400

    if take_profit_price <= 0 or stop_loss_price <= 0:
        log_signal(data, "Precios inválidos", error="TP o SL vacíos")
        return jsonify({"status": "error", "message": "Take Profit o Stop Loss inválido"}), 400


    try:
        # Establecer leverage
        client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)

        if MODE_ONEWAY:
            close_position(symbol)

        qty, precision = get_quantity(symbol)
        if qty <= 0:
            log_signal(data, "Cantidad inválida")
            return jsonify({"status": "error", "message": "Cantidad inválida"}), 400

        # Abrir posición inicial
        if action == "BUY":
            client.futures_create_order(
                symbol=symbol,
                side="BUY",
                type="MARKET",
                quantity=qty
            )
            log_signal(data, "Orden BUY enviada")

        elif action == "SELL":
            client.futures_create_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                quantity=qty
            )
            log_signal(data, "Orden SELL enviada")

        # Calcular el 70% del TP objetivo
        if action == "BUY":
            partial_tp_price = entry_price + (take_profit_price - entry_price) * 0.7
        elif action == "SELL":
            partial_tp_price = entry_price - (entry_price - take_profit_price) * 0.7
        else:
            partial_tp_price = 0.0

        half_qty = round(qty / 2, precision)

        print(f"🎯 TP objetivo: {take_profit_price}")
        print(f"🟡 TP parcial: {partial_tp_price}")
        print(f"🛡️ SL objetivo: {stop_loss_price}")

        # Iniciar polling del precio
        while True:
            current_ticker = client.futures_symbol_ticker(symbol=symbol)
            current_price = float(current_ticker['price'])

            # Si llega al 70% del TP, cierra el 50%
            if (action == "BUY" and current_price >= partial_tp_price) or \
               (action == "SELL" and current_price <= partial_tp_price):

                print(f"🟡 TP parcial alcanzado ({partial_tp_price}), cerrando {half_qty} unidades...")
                client.futures_create_order(
                    symbol=symbol,
                    side="SELL" if action == "BUY" else "BUY",
                    type="MARKET",
                    quantity=half_qty,
                    reduceOnly=True
                )
                remaining_qty = qty - half_qty
                print(f"✅ Se cerró el 50% de la posición. Restan {remaining_qty} unidades.")

                # Mover Stop Loss al precio de entrada (break-even)
                trailing_sl = entry_price
                print(f"🛡️ Moviendo Stop Loss al precio de entrada: {trailing_sl}")
                client.futures_create_order(
                    symbol=symbol,
                    side="SELL" if action == "BUY" else "BUY",
                    type="STOP_MARKET",
                    quantity=remaining_qty,
                    stopPrice=trailing_sl,
                    reduceOnly=True
                )

                break  # Salir del bucle después de ejecutar el TP parcial

            # Si se cierra toda la posición antes de llegar al TP → salir
            current_pos = close_position(symbol)
            if current_pos == 0:
                print("ℹ️ Posición completamente cerrada.")
                break

            time.sleep(10)  # Polling cada 10 segundos

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        log_signal(data, "Error al ejecutar orden", error=str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/stats', methods=['GET'])
def stats():
    """Muestra estadísticas básicas de uso"""
    try:
        with open('webhook_server.log', 'r') as f:
            logs = f.readlines()
        return jsonify({
            "total_signals": len(logs),
            "last_10_logs": [log.strip() for log in logs[-10:]]
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)