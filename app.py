from flask import Flask, request, jsonify
from binance import Client
import os
import logging
from datetime import datetime

# Configuración del logging
logging.basicConfig(
    filename='webhook_server.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def log_signal(data, status, error=None):
    """Guarda en logs cada señal recibida y su estado"""
    symbol = data.get("symbol", "unknown").upper()
    signal = data.get("signal", "unknown").upper()

    msg = f"Señal recibida: {symbol} | Acción: {signal} | Estado: {status}"
    if error:
        msg += f" | Error: {error}"

    logging.info(msg)
    print(msg)  # Mostrar también en consola


# Inicializar cliente de Binance
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")

client = Client(api_key, api_secret)

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

def get_quantity(symbol):
    """Calcula cantidad en base al 5% del balance"""
    balance = get_balance_usdt()
    investment = balance * POSITION_PERCENT
    try:
        ticker = client.futures_symbol_ticker(symbol=symbol)
        price = float(ticker['price'])
        qty = round(investment / price, 8)  # Ajuste a 8 decimales
        return qty
    except Exception as e:
        print("Error obteniendo precio:", e)
        return 0.0

def close_position(symbol):
    """Cierra cualquier posición abierta"""
    try:
        position = client.futures_position_information(symbol=symbol)
        qty = float(position[0]['positionAmt'])
        if qty != 0:
            side = "SELL" if qty > 0 else "BUY"
            client.futures_create_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=abs(qty)
            )
            print(f"Posición cerrada en {symbol}")
    except Exception as e:
        print(f"Error cerrando posición en {symbol}:", e)

# === SERVIDOR FLASK ===
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    print("Se recibió señal:", data)

    symbol = data.get("symbol", "").upper()
    signal = data.get("signal", "").upper()

    # Limpiar el símbolo (ej: SONICUSDT.P → SONICUSDT)
    symbol = symbol.replace(".P", "")

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

            order = client.futures_create_order(
                symbol=symbol,
                side="BUY",
                type="MARKET",
                quantity=qty
            )
            log_signal(data, "Orden BUY enviada")

        elif signal == "SELL":
            qty = get_quantity(symbol)
            if qty <= 0:
                log_signal(data, "Cantidad inválida")
                return jsonify({"status": "error", "message": "Cantidad inválida"}), 400

            order = client.futures_create_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                quantity=qty
            )
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
            "last_10_logs": logs[-10:]
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)