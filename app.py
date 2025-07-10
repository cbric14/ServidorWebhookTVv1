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

def get_open_position(symbol):
    """Devuelve la cantidad actual abierta sin modificarla"""
    try:
        position_info = client.futures_position_information(symbol=symbol)
        if not position_info or len(position_info) == 0:
            print(f"ℹ️ No hay posición abierta para {symbol}")
            return 0.0
        qty = float(position_info[0]['positionAmt'])
        return abs(qty)
    except Exception as e:
        print(f"⚠️ Error obteniendo posición para {symbol}: {str(e)}")
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
            print(f"⚠️ No hay posición abierta para {symbol}")
            return None
        half_qty = round(qty / 2, precision)
        if half_qty <= 0:
            print(f"⚠️ Cantidad inválida para TP: {half_qty}")
            log_signal(data, "Cantidad inválida", error=f"Cerrar parcial con qty={half_qty}")
            return None
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
        symbol = data.get("symbol", "").upper().replace("BINANCE:", "").replace(".P", "")
        action = data.get("action", "").upper()

        if action in ["EXIT BUY", "EXIT SELL"]:
            closed_qty = close_position(symbol)
            if closed_qty > 0:
                current_price = float(client.futures_symbol_ticker(symbol=symbol)['price'])
                is_long = data.get("original_action", "").upper() == "BUY"
                pnl = calculate_pnl(entry_price, current_price, closed_qty, is_long)
                closed_trades.append({
                    "symbol": symbol,
                    "action": action,
                    "entry": data.get("original_entry"),
                    "exit": current_price,
                    "qty": closed_qty,
                    "pnl": pnl,
                    "timestamp": time.time(),
                    "reason": "Salida manual"
                })
                log_signal(data, f"Posición cerrada manualmente, PnL: {pnl} USDT")
                return jsonify({"status": "ok", "message": "Posición cerrada"}), 200
            else:
                log_signal(data, "No había posición abierta")
                return jsonify({"status": "ok", "message": "No había posición abierta"}), 200

        elif action in ["BUY", "SELL"]:
            try:
                entry_price = float(data.get("entry"))
                take_profit_price = float(data.get("tp"))
                stop_loss_price = float(data.get("sl"))

                if entry_price <= 0 or take_profit_price <= 0 or stop_loss_price <= 0:
                    log_signal(data, "Precios inválidos", error="Uno o más precios menores o iguales a 0")
                    return jsonify({"status": "error", "message": "Precios deben ser mayores a 0"}), 400

                client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)

                if MODE_ONEWAY:
                    close_position(symbol)

                qty, precision = get_quantity(symbol)
                if qty <= 0:
                    log_signal(data, "Cantidad inválida")
                    return jsonify({"status": "error", "message": "Cantidad inválida"}), 400

                # Verificar si ya hay posición abierta
                current_pos = get_open_position(symbol)
                if current_pos > 0:
                    log_signal(data, f"⚠️ Ya hay posición abierta en {symbol}, cancelando nueva entrada")
                    return jsonify({"status": "error", "message": f"Ya hay posición abierta en {symbol}"}), 409
                
                # Abrir posición inicial
                if action == "BUY":
                    client.futures_create_order(symbol=symbol, side="BUY", type="MARKET", quantity=qty)
                    log_signal(data, "Orden BUY enviada")
                elif action == "SELL":
                    client.futures_create_order(symbol=symbol, side="SELL", type="MARKET", quantity=qty)
                    log_signal(data, "Orden SELL enviada")

                rounded_sl = round(stop_loss_price, precision)
                create_stop_loss_order(symbol, rounded_sl, precision)

                partial_tp_price = entry_price + abs(take_profit_price - entry_price) * 0.7 if action == "BUY" else entry_price - abs(entry_price - take_profit_price) * 0.7
                half_qty = round(qty / 2, precision)

                print(f"🎯 TP objetivo: {take_profit_price}")
                print(f"🟡 TP parcial: {partial_tp_price}")
                print(f"🛡️ SL objetivo: {stop_loss_price}")

                create_take_profit_order(symbol, take_profit_price, precision, data=data)

                partial_tp_executed = False

                while True:
                    current_ticker = client.futures_symbol_ticker(symbol=symbol)
                    current_price = float(current_ticker['price'])

                    if not partial_tp_executed:
                        if ((action == "BUY" and current_price >= partial_tp_price) or
                            (action == "SELL" and current_price <= partial_tp_price)):
                            print(f"🟡 TP parcial alcanzado ({partial_tp_price}), cerrando {half_qty} unidades...")
                            client.futures_create_order(
                                symbol=symbol,
                                side="SELL" if action == "BUY" else "BUY",
                                type="MARKET",
                                quantity=half_qty,
                                reduceOnly=True
                            )
                            remaining_qty = qty - half_qty
                            pnl = calculate_pnl(entry_price, current_price, half_qty, action == "BUY")
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
                            trailing_sl = entry_price
                            client.futures_create_order(
                                symbol=symbol,
                                side="SELL" if action == "BUY" else "BUY",
                                type="STOP_MARKET",
                                quantity=remaining_qty,
                                stopPrice=trailing_sl,
                                reduceOnly=True
                            )
                            partial_tp_executed = True

                    if ((action == "BUY" and current_price >= take_profit_price) or
                        (action == "SELL" and current_price <= take_profit_price)):
                        print("🎯 TP total alcanzado. Cerrando posición restante...")
                        remaining_qty = qty - half_qty
                        client.futures_create_order(
                            symbol=symbol,
                            side="SELL" if action == "BUY" else "BUY",
                            type="MARKET",
                            quantity=remaining_qty,
                            reduceOnly=True
                        )
                        pnl_total = calculate_pnl(entry_price, current_price, remaining_qty, action == "BUY")
                        closed_trades.append({
                            "symbol": symbol,
                            "action": action,
                            "entry": entry_price,
                            "exit": current_price,
                            "qty": remaining_qty,
                            "pnl": pnl_total,
                            "timestamp": time.time(),
                            "reason": "TP total alcanzado"
                        })
                        log_signal(data, f"TP total alcanzado en {current_price}, PnL: {pnl_total} USDT")
                        break

                    try:
                        position_info = client.futures_position_information(symbol=symbol)
                        current_pos = float(position_info[0]['positionAmt']) if position_info else 0.0
                    except Exception:
                        current_pos = 0.0

                    if current_pos == 0:
                        print("ℹ️ Posición completamente cerrada (SL o cierre externo).")
                        remaining_qty = qty - half_qty
                        if remaining_qty > 0:
                            pnl_final = calculate_pnl(entry_price, current_price, remaining_qty, action == "BUY")
                            closed_trades.append({
                                "symbol": symbol,
                                "action": action,
                                "entry": entry_price,
                                "exit": current_price,
                                "qty": remaining_qty,
                                "pnl": pnl_final,
                                "timestamp": time.time(),
                                "reason": "Posición cerrada totalmente (SL o cierre externo)"
                            })
                            log_signal(data, f"Posición cerrada totalmente, PnL: {pnl_final} USDT")
                        break

                    time.sleep(10)

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