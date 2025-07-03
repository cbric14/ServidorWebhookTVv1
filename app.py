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
    signal = data.get("signal", "unknown").upper()

    msg = f"Señal recibida: {symbol} | Acción: {signal} | Estado: {status}"
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
# Lista de pares permitidos (sin .P)
PARES_PERMITIDOS = ["FETUSDT", "GRTUSDT", "AIUSDT", "SONICUSDT", "DOTUSDT", "BAKEUSDT"]
LEVERAGE = 20
POSITION_PERCENT = 0.05  # 5% del balance disponible
MODE_ONEWAY = True
STOP_LOSS_PERCENT = 1.0  # 1% de Stop Loss

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
    """Obtiene la precisión permitida por el par"""
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
        return precision

    except Exception as e:
        print("⚠️ Error obteniendo stepSize:", e)
        return 8  # Fallback a 8 decimales si hay error

def get_quantity(symbol):
    investment = get_balance_usdt() * POSITION_PERCENT
    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        price = float(ticker['price'])
        qty = investment / price

        precision = get_step_size_precision(symbol)
        final_qty = round(qty, precision)

        print(f"✅ Cantidad calculada para {symbol}: {final_qty}")
        return final_qty

    except Exception as e:
        print(f"❌ Error obteniendo cantidad para {symbol}: {e}")
        return 0.0

def create_stop_loss_order(symbol, side, qty, entry_price):
    """Crea una orden STOP_MARKET del 1%"""
    try:
        if side == "BUY":
            stop_price = round(entry_price * (1 - STOP_LOSS_PERCENT / 100), 8)
            order_side = "SELL"
        elif side == "SELL":
            stop_price = round(entry_price * (1 + STOP_LOSS_PERCENT / 100), 8)
            order_side = "BUY"
        else:
            raise ValueError("Side debe ser BUY o SELL")

        sl_order = client.futures_create_order(
            symbol=symbol,
            side=order_side,
            type="STOP_MARKET",
            quantity=abs(qty),
            stopPrice=stop_price,
            reduceOnly=True
        )
        print(f"📉 Stop Loss creado en {stop_price} para {symbol}")
        return sl_order
    except Exception as e:
        print(f"⚠️ Error creando Stop Loss para {symbol}: {str(e)}")
        return None

def close_position(symbol):
    """Cierra cualquier posición abierta"""
    try:
        position_info = client.futures_position_information(symbol=symbol)

        if not position_info or len(position_info) == 0:
            print(f"ℹ️ No hay posición abierta para {symbol}")
            return

        qty = float(position_info[0]['positionAmt'])
        if qty != 0:
            order_side = "SELL" if qty > 0 else "BUY"
            client.futures_create_order(
                symbol=symbol,
                side=order_side,
                type="MARKET",
                quantity=abs(qty)
            )
            print(f"✅ Posición cerrada en {symbol}")
    except Exception as e:
        print(f"⚠️ Error cerrando posición en {symbol}: {str(e)}")

# === SERVIDOR FLASK ===
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({"status": "alive"}), 200

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    print("Se recibió señal:", data)

    symbol = data.get("symbol", "").upper().replace(".P", "")
    signal = data.get("signal", "").upper()

    if symbol not in PARES_PERMITIDOS:
        log_signal(data, "Rechazado (par no permitido)")
        return jsonify({"status": "error", "message": "Par no permitido"}), 400

    if signal not in ["BUY", "SELL", "EXIT BUY", "EXIT SELL"]:
        log_signal(data, "Señal desconocida")
        return jsonify({"status": "error", "message": "Señal desconocida"}), 400

    try:
        # Establecer leverage
        client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)

        if MODE_ONEWAY:
            close_position(symbol)

        if signal == "BUY":
            qty = get_quantity(symbol)
            if qty <= 0:
                log_signal(data, "Cantidad inválida")
                return jsonify({"status": "error", "message": "Cantidad inválida"}), 400

            # Crear orden principal
            order = client.futures_create_order(
                symbol=symbol,
                side="BUY",
                type="MARKET",
                quantity=qty
            )

            # Obtener precio actual para calcular SL
            ticker = client.futures_symbol_ticker(symbol=symbol)
            entry_price = float(ticker['price'])

            # Crear Stop Loss
            sl_order = create_stop_loss_order(symbol, "BUY", qty, entry_price)

            log_signal(data, "Orden BUY enviada")

        elif signal == "SELL":
            qty = get_quantity(symbol)
            if qty <= 0:
                log_signal(data, "Cantidad inválida")
                return jsonify({"status": "error", "message": "Cantidad inválida"}), 400

            # Crear orden principal
            order = client.futures_create_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                quantity=qty
            )

            # Obtener precio actual para calcular SL
            ticker = client.futures_symbol_ticker(symbol=symbol)
            entry_price = float(ticker['price'])

            # Crear Stop Loss
            sl_order = create_stop_loss_order(symbol, "SELL", qty, entry_price)

            log_signal(data, "Orden SELL enviada")

        elif signal == "EXIT BUY":
            close_position(symbol)
            log_signal(data, "Cerrada posición corta")

        elif signal == "EXIT SELL":
            close_position(symbol)
            log_signal(data, "Cerrada posición larga")

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