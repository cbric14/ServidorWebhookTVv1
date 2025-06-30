from flask import Flask, request
from binance import Client, ThreadedWebsocketManager
import os

app = Flask(webHookServer)

# Carga tus claves desde variables de entorno
api_key = os.getenv('BINANCE_API_KEY')
api_secret = os.getenv('BINANCE_API_SECRET')

client = Client(api_key, api_secret)

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    print("Se recibió una señal:", data)

    symbol = "BTCUSDT"
    quantity = 0.001  # Ajusta según tu estrategia

    if data.get('action') == 'buy':
        order = client.futures_create_order(
            symbol=symbol,
            side='BUY',
            type='MARKET',
            quantity=quantity
        )
        print("Orden de compra enviada:", order)

    elif data.get('action') == 'sell':
        order = client.futures_create_order(
            symbol=symbol,
            side='SELL',
            type='MARKET',
            quantity=quantity
        )
        print("Orden de venta enviada:", order)

    return '', 200

if webHookServer == '__main__':
    app.run()