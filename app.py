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

def binance_api_call(func, max_retries=5, base_delay=1):
    """Ejecuta llamadas a la API con reintento inteligente"""
    retries = 0
    while retries < max_retries:
        try:
            return func()
        except Exception as e:
            if any(err in str(e).lower() for err in ["rate limit", "503", "timeout"]):
                wait = base_delay * (2 ** retries)
                print(f"⏳ Reintentando en {wait}s por error temporal: {str(e)}")
                time.sleep(wait)
                retries += 1
            else:
                handle_binance_error(e, func.__name__)
                return None
    return None

def handle_binance_error(e, context=""):
    """Maneja errores de Binance con detalles"""
    error_code = getattr(e, 'code', None)
    error_msg = getattr(e, 'message', str(e))
    print(f"⚠️ Error en Binance [{context}]: Código {error_code}, Mensaje: {error_msg}")
    log_signal({}, f"Error en Binance: {error_msg}", error=str(e))

def cancel_open_orders(symbol, max_retries=3):
    """Cancela todas las órdenes abiertas para un símbolo con reintento"""
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
POSITION_PERCENT = 0.05  # 5% del balance disponible
MODE_ONEWAY = True

# === FUNCIONES AUXILIARES ===
def get_balance_usdt():
    """Obtiene el balance disponible en USDT"""
    def _get_balance():
        balances = client.futures_account_balance()
        for b in balances:
            if b['asset'] == 'USDT':
                return float(b['availableBalance'])
        return 0.0
    
    result = binance_api_call(_get_balance)
    return result if result is not None else 0.0

def get_step_size_precision(symbol):
    """Obtiene la cantidad de decimales permitidos por el par"""
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
        precision = max(0, precision)  # Evitar precisiones negativas
        return precision, step_size
        
    result = binance_api_call(_get_info)
    return result if result else (0, 0)

def get_quantity(symbol):
    """Calcula la cantidad a operar con precisión exacta"""
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

def get_open_position(symbol, max_retries=3):
    """Devuelve la cantidad actual abierta con reintento"""
    def _get_position():
        position_info = client.futures_position_information(symbol=symbol)
        if not position_info or len(position_info) == 0:
            print(f"ℹ️ No hay posición abierta para {symbol}")
            return 0.0
            
        qty = float(position_info[0]['positionAmt'])
        print(f"📊 Posición actual en {symbol}: {qty}")
        return abs(qty)
        
    result = binance_api_call(_get_position)
    return result if result is not None else 0.0

def close_position(symbol, max_retries=3):
    """Cierra cualquier posición abierta con reintento"""
    def _close_position():
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
        return 0.0
        
    result = binance_api_call(_close_position)
    return result if result is not None else 0.0

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
    def _create_sl():
        qty = get_open_position(symbol)
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
    """Crea una orden TRAILING_STOP_MARKET para el Stop Loss"""
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
            callbackRate=callback_rate  # Ajustado a 1.2%
        )
        print(f"🔁 Trailing Stop creado para {symbol} con callback rate {callback_rate}%")
        return tsl_order
        
    result = binance_api_call(_create_tsl)
    return result

def create_take_profit_order(symbol, tp_price, precision, data=None):
    """Crea una orden LIMIT para Take Profit"""
    def _create_tp():
        qty = get_open_position(symbol)
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

def confirm_position_opened(symbol, expected_qty, max_retries=5, delay=3):
    """Confirma que la posición se abrió correctamente"""
    for i in range(max_retries):
        time.sleep(delay)
        current_pos = get_open_position(symbol)
        if current_pos >= expected_qty * 0.95:
            print(f"✅ Posición confirmada: {current_pos} en {symbol}")
            return True
        print(f"🔄 Re-verificando posición {i+1}/{max_retries}")
    print("❌ No se pudo confirmar la posición tras la entrada")
    return False

def keepalive_binance_connection():
    """Mantiene la conexión con Binance viva"""
    while True:
        try:
            client.ping()
        except Exception as e:
            print("⚠️ Problema con conexión a Binance:", str(e))
        time.sleep(60)  # Cada minuto

def was_position_closed(symbol, timeout=30):
    """Verifica si la posición fue cerrada manualmente"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            trades = client.futures_account_trades(symbol=symbol, limit=1)
            if trades:
                trade = trades[0]
                if trade['realizedPnl'] != "0":
                    print("ℹ️ Posición cerrada manualmente")
                    return True
        except Exception as e:
            print("⚠️ Error consultando historial de órdenes:", str(e))
        time.sleep(5)
    return False

def process_signal(data):
    """Procesa señales de entrada/salida"""
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

                # Ajustar apalancamiento
                def _change_leverage():
                    return client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
                binance_api_call(_change_leverage)
                
                # Modo One-Way
                if MODE_ONEWAY:
                    close_position(symbol)
                    time.sleep(1)
                    current_pos = get_open_position(symbol)
                    if current_pos > 0:
                        log_signal(data, f"⚠️ No se pudo cerrar posición previa: {current_pos}")
                        return

                # Calcular tamaño de posición
                qty, precision = get_quantity(symbol)
                if qty <= 0:
                    log_signal(data, "Cantidad inválida", error=f"Valor calculado: {qty}")
                    return 

                # Verificar si ya hay posición abierta
                current_pos = get_open_position(symbol)
                if current_pos > 0:
                    log_signal(data, f"⚠️ Ya hay posición abierta en {symbol}, cancelando nueva entrada")
                    return 

                # Abrir posición
                if action == "BUY":
                    def _buy():
                        return client.futures_create_order(
                            symbol=symbol, side="BUY", type="MARKET", quantity=qty
                        )
                    response = binance_api_call(_buy)
                    if not response:
                        log_signal(data, "Error al enviar orden BUY")
                        return
                        
                elif action == "SELL":
                    def _sell():
                        return client.futures_create_order(
                            symbol=symbol, side="SELL", type="MARKET", quantity=qty
                        )
                    response = binance_api_call(_sell)
                    if not response:
                        log_signal(data, "Error al enviar orden SELL")
                        return

                # Confirmar posición abierta
                if not confirm_position_opened(symbol, qty):
                    log_signal(data, "❌ No se confirmó la posición tras la entrada")
                    return
                    
                # Crear órdenes de salida
                create_trailing_stop_order(symbol, qty)
                create_stop_loss_order(symbol, stop_loss_price, precision)
                
                # Calcular precios de TP
                partial_tp_price = entry_price + abs(take_profit_price - entry_price) * 0.618 if action == "BUY" else entry_price - abs(entry_price - take_profit_price) * 0.618
                half_qty = round(qty / 2, precision)
                print(f"🎯 TP objetivo: {take_profit_price}")
                print(f"🟡 TP parcial: {partial_tp_price}")
                print(f"🛡️ SL objetivo: {stop_loss_price}")

                # Monitoreo activo durante 30 minutos
                timeout = 30 * 60  # segundos
                start_time = time.time()
                partial_tp_executed = False
                
                while time.time() - start_time < timeout:
                    try:
                        current_ticker = client.futures_symbol_ticker(symbol=symbol)
                        current_price = float(current_ticker['price'])
                        
                        # TP parcial
                        if not partial_tp_executed:
                            if ((action == "BUY" and current_price >= partial_tp_price) or
                                (action == "SELL" and current_price <= partial_tp_price)):
                                print(f"🟡 TP parcial alcanzado ({partial_tp_price}), cerrando {half_qty}...")
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

                        # TP total
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

                        # Verificar cierre anticipado
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
    # Iniciar ping periódico a Binance
    threading.Thread(target=keepalive_binance_connection, daemon=True).start()
    
    # Correr servidor Flask
    app.run(host='0.0.0.0', port=5000)