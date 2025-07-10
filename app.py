from flask import Flask, request, jsonify
from binance import Client
import os
import logging
import time
import math

# === CONFIGURACIÓN GLOBAL ===
closed_trades = []

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

def get_open_position(symbol):
    """Devuelve la cantidad actual abierta sin modificarla"""
    try:
        position_info = client.futures_position_information(symbol=symbol)
        if not position_info or float(position_info[0]['positionAmt']) == 0:
            print(f"ℹ️ No hay posición abierta para {symbol}")
            return 0.0
        qty = float(position_info[0]['positionAmt'])
        return abs(qty)
    except Exception as e:
        print(f"⚠️ Error obteniendo posición para {symbol}: {str(e)}")
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

def create_take_profit_order(symbol, tp_price, precision, data=None):
    """Crea una orden LIMIT para Take Profit"""
    try:
        qty = get_open_position(symbol)
        if qty <= 0:
            print(f"⚠️ Cantidad inválida para crear TP: {qty}")
            log_signal(data, "Cantidad inválida", error=f"Cerrar parcial con qty={qty}")
            return None
        half_qty = round(qty / 2, precision)
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
    return jsonify({"status": "alive"}), 200

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    print("Se recibió señal:", data)

    try:
        # Limpiar y procesar símbolo
        symbol = data.get("symbol", "").upper().replace("BINANCE:", "").replace(".P", "")
        action = data.get("action", "").upper()

        # Valores numéricos solo necesarios para entradas
        entry_price = None
        take_profit_price = None
        stop_loss_price = None

        # Validaciones operaciones
        if action in ["EXIT BUY", "EXIT SELL"]:
            print(f"🛑 Cerrando posición manualmente: {symbol}")
            closed_qty = close_position(symbol)

            if closed_qty > 0:
                log_signal(data, f"Posición cerrada manualmente ({closed_qty} unidades)")
                return jsonify({"status": "ok", "message": "Posición cerrada"}), 200
            else:
                log_signal(data, "No había posición abierta")
                return jsonify({"status": "ok", "message": "No había posición abierta"}), 200

        elif action in ["BUY", "SELL"]:
            try:
                entry_price = float(data.get("entry", 0.0))
                take_profit_price = float(data.get("tp", 0.0))
                stop_loss_price = float(data.get("sl", 0.0))

                if take_profit_price <= 0 or stop_loss_price <= 0:
                    log_signal(data, "Precios inválidos", error="TP o SL vacíos")
                    return jsonify({"status": "error", "message": "Take Profit o Stop Loss inválido"}), 400

                # Establecer leverage
                client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)

                if MODE_ONEWAY:
                    close_position(symbol)

                # Obtener cantidad y precisión
                qty, precision = get_quantity(symbol)
                if qty <= 0:
                    log_signal(data, "Cantidad inválida")
                    return jsonify({"status": "error", "message": "Cantidad inválida"}), 400

                # Abrir posición inicial
                if action == "BUY":
                    client.futures_create_order(symbol=symbol, side="BUY", type="MARKET", quantity=qty)
                    log_signal(data, "Orden BUY enviada")
                elif action == "SELL":
                    client.futures_create_order(symbol=symbol, side="SELL", type="MARKET", quantity=qty)
                    log_signal(data, "Orden SELL enviada")

                # Crear Stop Loss si está definido
                if stop_loss_price > 0:
                    create_stop_loss_order(symbol, stop_loss_price, precision)
                else:
                    print("🛡️ Stop Loss no definido o inválido")

                # Calcular TP parcial
                partial_tp_price = 0.0
                if action == "BUY":
                    partial_tp_price = entry_price + (take_profit_price - entry_price) * 0.7
                elif action == "SELL":
                    partial_tp_price = entry_price - (entry_price - take_profit_price) * 0.7

                half_qty = round(qty / 2, precision)
                print(f"🎯 TP objetivo: {take_profit_price}")
                print(f"🟡 TP parcial: {partial_tp_price}")
                print(f"🛡️ SL objetivo: {stop_loss_price}")

                # Crear Take Profit objetivo
                create_take_profit_order(symbol, take_profit_price, precision, data=data)

                # Iniciar polling del precio
                while True:
                    current_ticker = client.futures_symbol_ticker(symbol=symbol)
                    current_price = float(current_ticker['price'])

                    if (action == "BUY" and current_price >= partial_tp_price) or \
                       (action == "SELL" and current_price <= partial_tp_price):
                        print(f"🟡 TP parcial alcanzado ({partial_tp_price}), cerrando {half_qty} unidades...")

                        if half_qty <= 0:
                            print(f"⚠️ Cantidad inválida para cerrar parcialmente: {half_qty}")
                            log_signal(data, "Cantidad inválida", error=f"Cerrar parcial con qty={half_qty}")
                            time.sleep(10)
                            continue

                        client.futures_create_order(
                            symbol=symbol,
                            side="SELL" if action == "BUY" else "BUY",
                            type="MARKET",
                            quantity=half_qty,
                            reduceOnly=True
                        )
                        remaining_qty = qty - half_qty
                        print(f"✅ Se cerró el 50% de la posición. Restan {remaining_qty} unidades.")

                        # Calcular PnL
                        is_long = action == "BUY"
                        pnl = calculate_pnl(entry_price, current_price, half_qty, is_long)
                        print(f"💰 Ganancia/Pérdida: {pnl} USDT")
                        closed_trades.append({
                            "symbol": symbol,
                            "action": action,
                            "entry": entry_price,
                            "exit": current_price,
                            "qty": half_qty,
                            "pnl": pnl,
                            "timestamp": time.time()
                        })
                        log_signal(data, f"TP parcial alcanzado en {current_price}, PnL: {pnl} USDT")

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

                    # Verificar si la posición fue cerrada externamente
                    try:
                        position_info = client.futures_position_information(symbol=symbol)
                        if not position_info or len(position_info) == 0:
                            print(f"⚠️ No hay información disponible para {symbol}")
                            current_pos = 0.0
                        else:
                            current_pos = float(position_info[0]['positionAmt'])
                    except Exception as e:
                        print(f"❌ Error obteniendo posición para {symbol}: {str(e)}")
                        current_pos = 0.0

                    if current_pos == 0:
                        print("ℹ️ Posición completamente cerrada.")
                        break

                    time.sleep(10)  # Polling cada 10 segundos

            except Exception as e:
                log_signal(data, "Error al ejecutar orden", error=str(e))
                return jsonify({"status": "error", "message": str(e)}), 500

        else:
            log_signal(data, "Acción desconocida")
            return jsonify({"status": "error", "message": "Acción desconocida"}), 400

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        log_signal(data, "Error crítico", error=str(e))
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/stats', methods=['GET'])
def stats():
    """Muestra estadísticas básicas de uso"""
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