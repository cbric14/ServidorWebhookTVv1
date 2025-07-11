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

def create_trailing_stop_order(symbol, qty, callback_rate=1.2):
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
            print(f"⚠️ No hay posición abierta en {symbol}")
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
            price=rounded_tp_price,
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
            if closed_qty > 0:
                log_signal(data, f"✅ Posición cerrada: {closed_qty} unidades")
            else:
                log_signal(data, "ℹ️ No había posición abierta")

        elif action in ["BUY", "SELL"]:
            try:
                entry_price = float(data.get("entry"))
                take_profit_price = float(data.get("tp"))
                stop_loss_price = float(data.get("sl"))
                
                if entry_price <= 0 or take_profit_price <= 0 or stop_loss_price <= 0:
                    log_signal(data, "Precios inválidos", error="Uno o más precios menores o iguales a 0")
                    return 

                print(f"🔧 Cambiando apalancamiento para {symbol} a x{LEVERAGE}")
                try:
                    client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
                except Exception as le:
                    log_signal(data, "Error ajustando apalancamiento", error=str(le))
                    return

                if MODE_ONEWAY:
                    close_position(symbol)
                    time.sleep(1)  # Breve pausa
                    current_pos = get_open_position(symbol)
                    if current_pos > 0:
                        log_signal(data, f"⚠️ No se pudo cerrar la posición previa: {current_pos}")
                        return          

                qty, precision = get_quantity(symbol)
                print(f"DEBUG: Cantidad calculada para {symbol}: {qty}, Precisión: {precision}")
                if qty <= 0:
                    log_signal(data, "Cantidad inválida", error=f"Valor calculado: {qty}")
                    return 

                current_pos = get_open_position(symbol)
                if current_pos > 0:
                    log_signal(data, f"⚠️ Ya hay posición abierta en {symbol}, cancelando nueva entrada")
                    return 

                if action == "BUY":
                    print(f"🛒 Enviando orden BUY para {symbol} de {qty} unidades...")
                    try:
                        client.futures_create_order(symbol=symbol, side="BUY", type="MARKET", quantity=qty)
                        log_signal(data, "Orden BUY enviada")
                    except Exception as e:
                        print(f"❌ Error al enviar orden BUY: {str(e)}")
                        log_signal(data, "Error al enviar orden", error=str(e))
                        return
                        
                elif action == "SELL":
                    print(f"🔻 Enviando orden SELL para {symbol} de {qty} unidades...")
                    try:
                        client.futures_create_order(symbol=symbol, side="SELL", type="MARKET", quantity=qty)
                        log_signal(data, "Orden SELL enviada")
                    except Exception as e:
                        print(f"❌ Error al enviar orden SELL: {str(e)}")
                        log_signal(data, "Error al enviar orden", error=str(e))
                        return

                create_trailing_stop_order(symbol, round(qty, precision), callback_rate=1.2)

                partial_tp_price = entry_price + abs(take_profit_price - entry_price) * 0.7 if action == "BUY" else entry_price - abs(entry_price - take_profit_price) * 0.7
                half_qty = round(qty / 2, precision)
                print(f"🎯 TP objetivo: {take_profit_price}")
                print(f"🟡 TP parcial: {partial_tp_price}")
                print(f"🛡️ SL objetivo: {stop_loss_price}")

                create_take_profit_order(symbol, take_profit_price, precision, data=data)

                partial_tp_executed = False
                # Límite de tiempo: 30 minutos
                timeout = 30 * 60  # segundos
                start_time = time.time()
                while time.time() - start_time < timeout:
                    try:
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
                                cancel_open_orders(symbol)
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
                            cancel_open_orders(symbol)
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
                    except Exception as e:
                        print(f"⚠️ Error en monitoreo de TP/SL: {e}")
                        break
                    time.sleep(10)
                else:
                    print("⏰ Tiempo de espera agotado. Verifica manualmente la posición.")
                    log_signal(data, "Tiempo de espera agotado para TP/SL")
            except Exception as e:
                log_signal(data, "Error al ejecutar orden", error=str(e))
                return 
        else:
            log_signal(data, "Acción desconocida")
            return
    except Exception as e:
        log_signal(data, "Error crítico", error=str(e))
        return
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