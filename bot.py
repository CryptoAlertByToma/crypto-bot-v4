# THOMAS BOT PRO - Live 24/7 sur Render Free (avec keep-alive HTTP)

import os
import threading
from flask import Flask
from telegram.ext import Application, CommandHandler
from binance.client import Client
import datetime

# ────────────────────────────────────────────────
#  Tes clés (mets-les en variables d'environnement sur Render !)
BOT_TOKEN = os.getenv("8695042227:AAGvg25FY4dnRGLgZuheEKnXq9_v6QRILXM")          # ← ajoute cette var sur Render
BINANCE_KEY = os.getenv("sFC9TfhWwPNQyPsnEGfNycajpGbiGrEr31XmdJlxE3Fde6JaoZJJFjQQWE2osi4k")
BINANCE_SECRET = os.getenv("1C7Lw2q4c8OekUZ7tI3NTJcstJSNzj7fx8DSp9A2AaQxOY5yPKdiIJ2WbUpEXjCT")
# ────────────────────────────────────────────────

def format_price(price):
    return f"{float(price):.2f}"

client = Client(BINANCE_KEY, BINANCE_SECRET)

# ── Mini serveur Flask pour garder Render heureux ──
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Thomas Bot PRO is alive 🚀"

def run_flask():
    port = int(os.environ.get("PORT", 10000))  # Render force $PORT
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

# ── Ton handler Telegram ──
async def new(update, context):
    symbols = ['BTCUSDT', 'SOLUSDT', 'ETHUSDT']
    data = {}
   
    for sym in symbols:
        ticker24 = client.get_ticker(symbol=sym)
        data[sym.replace('USDT', '')] = {
            'high': format_price(ticker24['highPrice']),
            'low': format_price(ticker24['lowPrice'])
        }
   
    msg = f"""🔥 *RAPPORT CRYPTO* {datetime.date.today()} 🔥
💎 *BTC* ↑ {data['BTC']['high']}$ | ↓ {data['BTC']['low']}$
🚀 *SOL* ↑ {data['SOL']['high']}$ | ↓ {data['SOL']['low']}$
⚡ *ETH* ↑ {data['ETH']['high']}$ | ↓ {data['ETH']['low']}$
📊 *Inflow/Outflow 👇*
BTC: https://coinglass.com/spot-inflow-outflow
SOL: https://coinglass.com/spot-inflow-outflow?symbol=SOL
ETH: https://coinglass.com/spot-inflow-outflow?symbol=ETH
_/new | Thomas Bot_"""
    await update.message.reply_text(msg, parse_mode='Markdown')

# ── Lancement ──
if __name__ == "__main__":
    print("🤖 Thomas Bot PRO starting...")

    # Lance Flask dans un thread séparé
    threading.Thread(target=run_flask, daemon=True).start()

    # Lance le bot Telegram (polling)
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("new", new))
    
    print("🤖 Thomas Bot PRO live 24/7")
    application.run_polling(allowed_updates=["message"])
