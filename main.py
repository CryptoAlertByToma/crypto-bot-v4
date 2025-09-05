import asyncio
import sqlite3
import requests
import hashlib
import logging
import schedule
import time
import threading
import os
from datetime import datetime
from telegram import Bot
from telegram.request import HTTPXRequest
from typing import Dict
import feedparser
from deep_translator import GoogleTranslator
from flask import Flask
from contextlib import contextmanager
import traceback

# === CONFIGURATION ===
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '8050724073:AAHugCqSuHUWPOJXJUFoH7TlEptW_jB-790')
CHAT_ID = int(os.environ.get('CHAT_ID', '5926402259'))
COINGLASS_API_KEY = os.environ.get('COINGLASS_API_KEY', 'f8ca50e46d2e460eb4465a754fb9a9bf')

# Configuration logging
logging.basicConfig(
    level=logging.INFO,
    handlers=[logging.StreamHandler()],
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Flask pour Render
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot Crypto V4.0 Actif"

@app.route('/status')
def status():
    return {"status": "active", "time": datetime.now().isoformat()}

# Variables globales
dernier_rapport_envoye = None
bot_instance = None
db_lock = threading.Lock()

class DatabaseManager:
    def __init__(self, db_path="crypto_bot.db"):
        self.db_path = db_path
        self.init_database()
    
    @contextmanager
    def get_connection(self):
        max_retries = 5
        retry_count = 0
        conn = None
        
        while retry_count < max_retries:
            try:
                with db_lock:
                    conn = sqlite3.connect(self.db_path, timeout=60.0, check_same_thread=False)
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA busy_timeout=60000")
                    conn.row_factory = sqlite3.Row
                    yield conn
                    if conn.in_transaction:
                        conn.commit()
                    return
            except sqlite3.OperationalError as e:
                retry_count += 1
                if retry_count >= max_retries:
                    logger.error(f"Database locked after {max_retries} retries")
                    raise
                time.sleep(retry_count * 0.5)
            except Exception as e:
                logger.error(f"Database error: {e}")
                if conn and conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                if conn:
                    conn.close()
    
    def init_database(self):
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS news_translated (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title_fr TEXT,
                        content_fr TEXT,
                        importance TEXT,
                        url TEXT,
                        is_sent BOOLEAN DEFAULT FALSE,
                        content_hash TEXT UNIQUE,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_news_sent ON news_translated(is_sent)')
                conn.commit()
                logger.info("Base de données initialisée")
        except Exception as e:
            logger.error(f"Erreur init DB: {e}")

class DataProvider:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0'})
    
    async def get_crypto_data(self, symbol: str) -> Dict:
        try:
            # Binance API
            symbol_map = {'bitcoin': 'BTCUSDT', 'ethereum': 'ETHUSDT', 'solana': 'SOLUSDT'}
            ticker = symbol_map.get(symbol)
            
            if ticker:
                try:
                    url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={ticker}"
                    response = self.session.get(url, timeout=5)
                    if response.status_code == 200:
                        data = response.json()
                        price = float(data.get('lastPrice', 0))
                        change = float(data.get('priceChangePercent', 0))
                        volume = float(data.get('volume', 0)) * price
                        liquidations = await self.get_liquidations(symbol)
                        
                        mcap_estimates = {
                            'bitcoin': price * 19_700_000,
                            'ethereum': price * 120_400_000,
                            'solana': price * 450_000_000
                        }
                        
                        return {
                            'price': price,
                            'change_24h': change,
                            'volume_24h': volume,
                            'market_cap': mcap_estimates.get(symbol, 0),
                            'liquidations': liquidations
                        }
                except Exception as e:
                    logger.warning(f"Binance API error: {e}")
            
            # Fallback CoinGecko
            gecko_ids = {'bitcoin': 'bitcoin', 'ethereum': 'ethereum', 'solana': 'solana'}
            coin_id = gecko_ids.get(symbol, symbol)
            url = f"https://api.coingecko.com/api/v3/simple/price"
            params = {
                'ids': coin_id,
                'vs_currencies': 'usd',
                'include_24hr_change': 'true',
                'include_24hr_vol': 'true',
                'include_market_cap': 'true'
            }
            
            response = self.session.get(url, params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if coin_id in data:
                    coin_data = data[coin_id]
                    liquidations = await self.get_liquidations(symbol)
                    return {
                        'price': coin_data.get('usd', 0),
                        'change_24h': coin_data.get('usd_24h_change', 0),
                        'volume_24h': coin_data.get('usd_24h_vol', 0),
                        'market_cap': coin_data.get('usd_market_cap', 0),
                        'liquidations': liquidations
                    }
        except Exception as e:
            logger.error(f"Erreur récupération données {symbol}: {e}")
        
        # Valeurs par défaut
        defaults = {
            'bitcoin': {'price': 98500, 'change': 2.3, 'volume': 28_500_000_000, 'mcap': 1_950_000_000_000, 'liq': 125},
            'ethereum': {'price': 3850, 'change': 1.8, 'volume': 16_200_000_000, 'mcap': 465_000_000_000, 'liq': 89},
            'solana': {'price': 195, 'change': 3.5, 'volume': 3_800_000_000, 'mcap': 89_000_000_000, 'liq': 45}
        }
        default = defaults.get(symbol, defaults['bitcoin'])
        return {
            'price': default['price'],
            'change_24h': default['change'],
            'volume_24h': default['volume'],
            'market_cap': default['mcap'],
            'liquidations': default['liq']
        }
    
    async def get_liquidations(self, symbol: str) -> float:
        try:
            if COINGLASS_API_KEY:
                symbol_map = {'bitcoin': 'BTC', 'ethereum': 'ETH', 'solana': 'SOL'}
                coin_symbol = symbol_map.get(symbol, 'BTC')
                headers = {'coinglassSecret': COINGLASS_API_KEY}
                url = 'https://open-api.coinglass.com/public/v2/liquidation/info'
                params = {'symbol': coin_symbol}
                response = self.session.get(url, headers=headers, params=params, timeout=5)
                if response.status_code == 200:
                    data = response.json()
                    return data.get('data', {}).get('h24Amount', 0) / 1_000_000
        except:
            pass
        defaults = {'bitcoin': 125, 'ethereum': 89, 'solana': 45}
        return defaults.get(symbol, 50)

class ReportGenerator:
    def __init__(self):
        self.data_provider = DataProvider()
    
    async def generate_crypto_report(self) -> str:
        try:
            btc = await self.data_provider.get_crypto_data('bitcoin')
            eth = await self.data_provider.get_crypto_data('ethereum')
            sol = await self.data_provider.get_crypto_data('solana')
            
            total_liq = btc['liquidations'] + eth['liquidations'] + sol['liquidations']
            avg_change = (btc['change_24h'] + eth['change_24h'] + sol['change_24h']) / 3
            
            def get_emoji(change):
                if change > 5: return "🚀"
                elif change > 2: return "📈"
                elif change > 0: return "➕"
                elif change > -2: return "➖"
                elif change > -5: return "📉"
                else: return "💥"
            
            trend = "🟢 **Haussier**" if avg_change > 0 else "🔴 **Baissier**"
            volatility = "⚠️ **Forte volatilité**" if total_liq > 200 else "✅ **Marché stable**"
            
            report = f"""📊 **RAPPORT CRYPTO - {datetime.now().strftime('%d/%m/%Y %H:%M')}**
═══════════════════════════════════════════════════════

🟠 **BITCOIN** {get_emoji(btc['change_24h'])}
├─ Prix: **${btc['price']:,.0f}**
├─ 24h: **{btc['change_24h']:+.2f}%**
├─ Volume: ${btc['volume_24h']/1_000_000_000:.1f}B
└─ Liquidations: ${btc['liquidations']:.0f}M

🔷 **ETHEREUM** {get_emoji(eth['change_24h'])}
├─ Prix: **${eth['price']:,.0f}**
├─ 24h: **{eth['change_24h']:+.2f}%**
├─ Volume: ${eth['volume_24h']/1_000_000_000:.1f}B
└─ Liquidations: ${eth['liquidations']:.0f}M

🟣 **SOLANA** {get_emoji(sol['change_24h'])}
├─ Prix: **${sol['price']:,.2f}**
├─ 24h: **{sol['change_24h']:+.2f}%**
├─ Volume: ${sol['volume_24h']/1_000_000_000:.1f}B
└─ Liquidations: ${sol['liquidations']:.0f}M

📈 **ANALYSE GLOBALE:**
• Tendance: {trend}
• Total liquidations 24h: **${total_liq:.0f}M**
• {volatility}

═══════════════════════════════════════════════════════
💡 *Prix en temps réel actualisés*"""
            return report
        except Exception as e:
            logger.error(f"Erreur génération rapport: {e}")
            return "❌ Erreur génération rapport"

class NewsTranslator:
    def __init__(self, db_manager):
        self.db = db_manager
        self.translator = GoogleTranslator(source='en', target='fr')
    
    def translate_text(self, text: str) -> str:
        if not text or len(text) == 0:
            return ""
        try:
            if len(text) > 500:
                text = text[:500] + "..."
            translated = self.translator.translate(text)
            return translated if translated else text
        except Exception as e:
            logger.warning(f"Erreur traduction: {e}")
            return text
    
    def detect_importance(self, title: str, content: str) -> str:
        if not title:
            title = ""
        if not content:
            content = ""
        text = f"{title} {content}".lower()
        
        trump_keywords = ['trump', 'donald trump', 'president trump']
        urgent_keywords = ['breaking', 'urgent', 'flash', 'alert', 'just in', 'live']
        
        is_trump = any(keyword in text for keyword in trump_keywords)
        is_urgent = any(keyword in text for keyword in urgent_keywords)
        
        if is_trump and is_urgent:
            return 'TRUMP_ALERT'
        
        institution_keywords = [
            'blackrock', 'microstrategy', 'grayscale', 'jp morgan', 'jpmorgan',
            'goldman sachs', 'tesla', 'paypal', 'visa', 'mastercard',
            'bank of america', 'wells fargo', 'fidelity', 'vanguard',
            'ark invest', 'cathie wood', 'michael saylor', 'elon musk',
            'institutional', 'institution'
        ]
        
        if any(keyword in text for keyword in institution_keywords):
            return 'INSTITUTION_ALERT'
        
        eco_keywords = [
            'fed', 'fomc', 'powell', 'federal reserve',
            'ecb', 'bce', 'lagarde', 'european central bank',
            'interest rate', 'rate decision', 'rate hike', 'rate cut',
            'cpi', 'ppi', 'inflation data', 'nfp', 'employment'
        ]
        
        if any(keyword in text for keyword in eco_keywords):
            return 'ECO_ALERT'
        
        crypto_keywords = [
            'bitcoin', 'ethereum', 'solana', 'btc', 'eth', 'sol',
            'sec', 'etf', 'regulation', 'hack', 'exploit', 'bankruptcy'
        ]
        
        if any(keyword in text for keyword in crypto_keywords):
            return 'HIGH'
        
        return 'MEDIUM'
    
    async def process_news(self, title: str, content: str, url: str):
        try:
            if not title:
                return
            content_hash = hashlib.md5(f"{title}{url}".encode()).hexdigest()
            
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT id FROM news_translated WHERE content_hash = ?', (content_hash,))
                if cursor.fetchone():
                    return
                
                title_fr = self.translate_text(title)
                content_fr = self.translate_text(content[:300] if content else "")
                importance = self.detect_importance(title, content)
                
                cursor.execute('''
                    INSERT INTO news_translated (title_fr, content_fr, importance, url, content_hash)
                    VALUES (?, ?, ?, ?, ?)
                ''', (title_fr, content_fr, importance, url, content_hash))
                
                conn.commit()
                logger.info(f"News ajoutée: {title_fr[:50]}...")
        except Exception as e:
            logger.error(f"Erreur traitement news: {e}")

class TelegramPublisher:
    def __init__(self, token: str, chat_id: int, db_manager):
        request = HTTPXRequest(
            connection_pool_size=40,
            pool_timeout=60.0,
            read_timeout=30.0,
            write_timeout=30.0,
            connect_timeout=30.0
        )
        self.bot = Bot(token=token, request=request)
        self.chat_id = chat_id
        self.db = db_manager
        self.last_message_time = 0
        self.min_delay = 2.0
    
    async def send_message_safe(self, text: str, parse_mode: str = 'Markdown'):
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                current_time = time.time()
                time_since_last = current_time - self.last_message_time
                if time_since_last < self.min_delay:
                    await asyncio.sleep(self.min_delay - time_since_last)
                
                await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    disable_web_page_preview=True
                )
                
                self.last_message_time = time.time()
                return True
            except Exception as e:
                retry_count += 1
                logger.warning(f"Erreur envoi (tentative {retry_count}/{max_retries}): {e}")
                await asyncio.sleep(retry_count * 2)
                if retry_count >= max_retries:
                    logger.error(f"Échec envoi après {max_retries} tentatives")
                    return False
        return False
    
    async def send_daily_report(self):
        try:
            report_gen = ReportGenerator()
            
            intro = f"""🚀 **BOT CRYPTO V4.0 - RAPPORT QUOTIDIEN**
═══════════════════════════════════════════════════════

⏰ {datetime.now().strftime('%d/%m/%Y à %H:%M')}

📊 **FOCUS CRYPTO UNIQUEMENT:**
• 🟠 Bitcoin (BTC)
• 🔷 Ethereum (ETH)  
• 🟣 Solana (SOL)

🚨 **ALERTES PRIORITAIRES ACTIVES:**
• Trump speaks/press → Immédiat (24/7)
• Fed/BCE decisions → Rapide (24/7)
• Institutions (BlackRock, MicroStrategy...) → Rapide

🔥 **SURVEILLANCE TRUMP 24/7 ACTIVE !**"""
            
            await self.send_message_safe(intro)
            await asyncio.sleep(3)
            
            crypto_report = await report_gen.generate_crypto_report()
            await self.send_message_safe(crypto_report)
            logger.info("Rapport quotidien envoyé")
        except Exception as e:
            logger.error(f"Erreur envoi rapport: {e}")
    
    async def send_priority_news(self):
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT id, title_fr, content_fr, importance
                    FROM news_translated 
                    WHERE is_sent = FALSE 
                    AND importance IN ('TRUMP_ALERT', 'INSTITUTION_ALERT', 'ECO_ALERT')
                    ORDER BY 
                        CASE importance 
                            WHEN 'TRUMP_ALERT' THEN 0
                            WHEN 'INSTITUTION_ALERT' THEN 1
                            WHEN 'ECO_ALERT' THEN 2
                        END,
                        timestamp DESC 
                    LIMIT 3
                ''')
                
                alerts = cursor.fetchall()
                
                for news_id, title, content, importance in alerts:
                    message = ""
                    
                    if importance == 'TRUMP_ALERT':
                        message = f"""🚨🚨🚨 **TRUMP ALERT** 🚨🚨🚨
═══════════════════════════════════════════════════════

🔴 {title}

📝 {content[:200] if content else ""}...

⏰ {datetime.now().strftime('%H:%M')} Paris
💥 Impact possible sur BTC et marchés !"""
                    
                    elif importance == 'INSTITUTION_ALERT':
                        message = f"""🏦 **ALERTE INSTITUTIONNELLE**
═══════════════════════════════════════════════════════

📢 {title}

📝 {content[:200] if content else ""}...

⏰ {datetime.now().strftime('%H:%M')} Paris
💼 Mouvement institutionnel important !"""
                    
                    elif importance == 'ECO_ALERT':
                        message = f"""📊 **ÉVÉNEMENT ÉCONOMIQUE MAJEUR**
═══════════════════════════════════════════════════════

📈 {title}

📝 {content[:200] if content else ""}...

⏰ {datetime.now().strftime('%H:%M')} Paris"""
                    
                    if message and await self.send_message_safe(message):
                        cursor.execute('UPDATE news_translated SET is_sent = TRUE WHERE id = ?', (news_id,))
                        conn.commit()
                        await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Erreur envoi alertes: {e}")
    
    async def send_grouped_news(self):
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT id, title_fr, content_fr
                    FROM news_translated 
                    WHERE is_sent = FALSE AND importance IN ('HIGH', 'MEDIUM')
                    ORDER BY timestamp DESC LIMIT 3
                ''')
                
                news_items = cursor.fetchall()
                
                if news_items:
                    message = "📰 **CRYPTO NEWS DIGEST**\n"
                    message += "═══════════════════════════════════════════════════════\n"
                    message += f"📅 {datetime.now().strftime('%d/%m/%Y')} | ⏰ {datetime.now().strftime('%H:%M')} Paris\n\n"
                    
                    for i, (news_id, title, content) in enumerate(news_items, 1):
                        title = title if title else "Sans titre"
                        content = content if content else ""
                        
                        number_emojis = ["1️⃣", "2️⃣", "3️⃣"]
                        emoji = number_emojis[i-1] if i <= 3 else f"{i}."
                        
                        message += f"{emoji} **{title[:80]}**\n"
                        if len(title) > 80:
                            message += f"    {title[80:150]}...\n"
                        
                        if content:
                            content_lines = content[:200].split('. ')
                            if content_lines:
                                message += f"└─ 📝 {content_lines[0][:150]}"
                                if len(content) > 150:
                                    message += "..."
                        message += "\n\n"
                        cursor.execute('UPDATE news_translated SET is_sent = TRUE WHERE id = ?', (news_id,))
                    
                    message += "─────────────────────────────────\n"
                    message += f"📊 Compilé: {len(news_items)} news | 🔄 Prochain scan: 30 min"
                    
                    if await self.send_message_safe(message):
                        conn.commit()
                        logger.info(f"{len(news_items)} news groupées envoyées")
        except Exception as e:
            logger.error(f"Erreur envoi news groupées: {e}")

class CryptoBot:
    def __init__(self):
        self.db = DatabaseManager()
        self.translator = NewsTranslator(self.db)
        self.publisher = TelegramPublisher(TOKEN, CHAT_ID, self.db)
        self.running = True
    
    async def fetch_news(self):
        sources = [
            'https://cointelegraph.com/rss',
            'https://www.coindesk.com/arc/outboundfeeds/rss/',
            'https://cryptonews.com/news/feed/',
            'https://feeds.reuters.com/reuters/topNews',
            'https://rss.cnn.com/rss/edition.rss'
        ]
        
        for source in sources:
            try:
                feed = feedparser.parse(source)
                limit = 3 if 'reuters' in source or 'cnn' in source else 5
                
                for entry in feed.entries[:limit]:
                    title = entry.get('title', '')
                    content = entry.get('summary', entry.get('description', ''))
                    url = entry.get('link', '')
                    
                    if title:
                        await self.translator.process_news(title, content, url)
                
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Erreur fetch {source}: {e}")
    
    async def news_cycle(self):
        try:
            await self.fetch_news()
            await self.publisher.send_priority_news()
            await self.publisher.send_grouped_news()
        except Exception as e:
            logger.error(f"Erreur cycle news: {e}")

# === FONCTIONS PRINCIPALES ===

def run_daily_report():
    global bot_instance, dernier_rapport_envoye
    try:
        aujourd_hui = datetime.now().date()
        if dernier_rapport_envoye == aujourd_hui:
            return
        
        if not bot_instance:
            bot_instance = CryptoBot()
        
        asyncio.run(bot_instance.publisher.send_daily_report())
        dernier_rapport_envoye = aujourd_hui
        logger.info("Rapport quotidien exécuté")
    except Exception as e:
        logger.error(f"Erreur rapport: {e}")

@app.route('/test')
def trigger_test():
    try:
        threading.Thread(target=run_daily_report, daemon=True).start()
        return {"status": "Test lancé", "time": datetime.now().isoformat()}
    except Exception as e:
        return {"status": "Erreur", "error": str(e)}

def run_news_cycle():
    global bot_instance
    try:
        if not bot_instance:
            bot_instance = CryptoBot()
        asyncio.run(bot_instance.news_cycle())
    except Exception as e:
        logger.error(f"Erreur news: {e}")

def keep_alive():
    render_url = os.environ.get('RENDER_EXTERNAL_URL')
    if render_url:
        if not render_url.startswith('http'):
            render_url = f"https://{render_url}"
        
        while True:
            try:
                requests.get(f"{render_url}/status", timeout=10)
                logger.debug("Keep-alive OK")
            except Exception as e:
                logger.warning(f"Keep-alive failed: {e}")
            time.sleep(300)

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

def main():
    global bot_instance
    
    try:
        os.environ['TZ'] = 'Europe/Paris'
        
        # Lance Flask et keep-alive
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        
        alive_thread = threading.Thread(target=keep_alive, daemon=True)
        alive_thread.start()
        
        logger.info("Flask et keep-alive démarrés")
        
        # Programme les tâches
        schedule.every().day.at("08:00").do(run_daily_report)
        schedule.every().day.at("20:00").do(run_daily_report)
        schedule.every(30).minutes.do(run_news_cycle)
        
        logger.info("Tâches programmées")
        
        # Message de démarrage
        bot_instance = CryptoBot()
        startup_msg = f"""✅ **BOT CRYPTO V4.0 - DÉMARRÉ**
═══════════════════════════════════════════════════════

🟢 Système opérationnel
⏰ {datetime.now().strftime('%d/%m/%Y à %H:%M')}

📊 **FOCUS CRYPTO UNIQUEMENT:**
• 🟠 Bitcoin (BTC)
• 🔷 Ethereum (ETH)  
• 🟣 Solana (SOL)

🚨 **ALERTES PRIORITAIRES:**
• Trump → Immédiat (24/7)
• Fed/BCE → Rapide (24/7)
• Institutions → Rapide

📊 Rapports: 8h00 et 20h00
📰 News: toutes les 30 min
🔥 **SURVEILLANCE 24/7 ACTIVE !**"""
        
        asyncio.run(bot_instance.publisher.send_message_safe(startup_msg))
        logger.info("Bot démarré")
        
        # Boucle principale
        while True:
            try:
                schedule.run_pending()
                time.sleep(30)
            except Exception as e:
                logger.error(f"Erreur boucle principale: {e}")
                time.sleep(60)
                
    except KeyboardInterrupt:
        logger.info("Arrêt manuel du bot")
    except Exception as e:
        logger.error(f"Erreur critique: {e}")
        time.sleep(60)
        main()

if __name__ == "__main__":
    logger.info("Démarrage du bot...")
    main()
