from flask import Flask, request, jsonify
from binance import Client
import os

app = Flask(__name__)

# === CONFIGURACIÓN ===
PARES_PERMITIDOS = ["FETUSDT", "GRTUSDT", "AIUSDT", "SONICUSDT", "DOTUSDT", "BAKEUSDT"]
LEVERAGE = 20
POSITION_PERCENT = 0.05  # 5% del balance disponible
MODE_ONEWAY = True

# Inicializa cliente de Binance
api_key = os.getenv("BINANCE_API_KEY")
api_secret = os.getenv("BINANCE_API_SECRET")
client = Client(api_key, api_secret)

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

# === RUTA PRINCIPAL ===

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    print("Se recibió señal:", data)

    symbol = data.get("symbol", "").upper()
    signal = data.get("signal", "").upper()

    if symbol not in PARES_PERMITIDOS:
        print("Par no permitido:", symbol)
        return jsonify({"status": "error", "message": "Par no permitido"}), 400

    try:
        # Establecer leverage
        client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)

        if MODE_ONEWAY:
            close_position(symbol)

        if signal == "BUY":
            qty = get_quantity(symbol)
            if qty <= 0:
                return jsonify({"status": "error", "message": "Cantidad inválida"}), 400

            order = client.futures_create_order(
                symbol=symbol,
                side="BUY",
                type="MARKET",
                quantity=qty
            )
            print("Orden BUY enviada:", order)

        elif signal == "SELL":
            qty = get_quantity(symbol)
            if qty <= 0:
                return jsonify({"status": "error", "message": "Cantidad inválida"}), 400

            order = client.futures_create_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                quantity=qty
            )
            print("Orden SELL enviada:", order)

        elif signal == "EXIT BUY":
            close_position(symbol)

        elif signal == "EXIT SELL":
            close_position(symbol)

        elif signal == "TAKE PROFIT":
            # Aquí puedes implementar lógica personalizada si TradingView envía el precio objetivo
            print("Señal TAKE PROFIT recibida")

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print("Error ejecutando orden:", e)
        return jsonify({"status": "error", "message": str(e)}), 500

# === INICIO DEL SERVIDOR ===

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)