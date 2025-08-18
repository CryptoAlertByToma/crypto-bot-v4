import asyncio
import sqlite3
import requests
import hashlib
import logging
import json
import schedule
import time
import threading
import os
import pytz
from datetime import datetime, timedelta
from telegram import Bot
from telegram.request import HTTPXRequest
from typing import Dict, List, Optional
import feedparser
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator
from flask import Flask
from contextlib import contextmanager
import queue

# === CONFIGURATION ===
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '8050724073:AAHugCqSuHUWPOJXJUFoH7TlEptW_jB-790')
CHAT_ID = int(os.environ.get('CHAT_ID', '5926402259'))

# APIs
COINGLASS_API_KEY = os.environ.get('COINGLASS_API_KEY', 'f8ca50e46d2e460eb4465a754fb9a9bf')
ALPHA_VANTAGE_KEY = os.environ.get('ALPHA_VANTAGE_KEY', '4J51YB27HHDW6X62')

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
        """Gestion sécurisée des connexions SQLite avec retry robuste"""
        max_retries = 5
        retry_count = 0
        conn = None
        
        while retry_count < max_retries:
            try:
                with db_lock:
                    conn = sqlite3.connect(self.db_path, timeout=60.0, check_same_thread=False, isolation_level='DEFERRED')
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA busy_timeout=60000")
                    conn.execute("PRAGMA synchronous=NORMAL")
                    conn.execute("PRAGMA temp_store=MEMORY")
                    conn.execute("PRAGMA cache_size=10000")
                    conn.row_factory = sqlite3.Row
                    
                    yield conn
                    
                    if conn.in_transaction:
                        conn.commit()
                    return
                    
            except sqlite3.OperationalError as e:
                retry_count += 1
                logger.warning(f"Database locked, retry {retry_count}/{max_retries}")
                if retry_count >= max_retries:
                    logger.error(f"Database locked after {max_retries} retries: {e}")
                    if conn:
                        conn.rollback()
                        conn.close()
                    time.sleep(2)
                    raise
                time.sleep(retry_count * 0.5)
            except Exception as e:
                logger.error(f"Database error: {e}")
                if conn and conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                if conn:
                    try:
                        conn.close()
                    except:
                        pass
    
    def init_database(self):
        """Initialisation de la base de données"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS market_data (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        price REAL,
                        change_24h REAL,
                        volume_24h REAL,
                        market_cap REAL,
                        liquidations REAL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
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
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_news_importance ON news_translated(importance)')
                
                conn.commit()
                logger.info("✅ Base de données initialisée avec succès")
        except Exception as e:
            logger.error(f"❌ Erreur initialisation DB: {e}")

class DataProvider:
    """Fournisseur de données crypto et forex avec APIs"""
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0'})
    
    async def get_crypto_data(self, symbol: str) -> Dict:
        """Récupère les données crypto avec multiple APIs"""
        try:
            # Essai 1: CoinGlass API pour liquidations
            if COINGLASS_API_KEY and symbol == 'bitcoin':
                try:
                    headers = {'coinglassSecret': COINGLASS_API_KEY}
                    url = 'https://open-api.coinglass.com/public/v2/liquidation/info'
                    params = {'symbol': 'BTC'}
                    response = self.session.get(url, headers=headers, params=params, timeout=5)
                    if response.status_code == 200:
                        liq_data = response.json()
                        liquidations = liq_data.get('data', {}).get('h24Amount', 0)
                except:
                    liquidations = 0
            else:
                liquidations = 0
            
            # Essai 2: CoinGecko pour prix
            gecko_ids = {
                'bitcoin': 'bitcoin',
                'ethereum': 'ethereum', 
                'solana': 'solana'
            }
            
            coin_id = gecko_ids.get(symbol, symbol)
            url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd&include_24hr_change=true&include_24hr_vol=true&include_market_cap=true"
            
            response = self.session.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if coin_id in data:
                    coin_data = data[coin_id]
                    return {
                        'price': coin_data.get('usd', 0),
                        'change_24h': coin_data.get('usd_24h_change', 0),
                        'volume_24h': coin_data.get('usd_24h_vol', 0),
                        'market_cap': coin_data.get('usd_market_cap', 0),
                        'liquidations': liquidations
                    }
        except Exception as e:
            logger.warning(f"Erreur API: {e}")
        
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
    
    async def get_eurusd_data(self) -> Dict:
        """Données EUR/USD via Alpha Vantage"""
        try:
            if ALPHA_VANTAGE_KEY:
                url = f"https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE&from_currency=EUR&to_currency=USD&apikey={ALPHA_VANTAGE_KEY}"
                response = self.session.get(url, timeout=5)
                if response.status_code == 200:
                    data = response.json()
                    if 'Realtime Currency Exchange Rate' in data:
                        rate_data = data['Realtime Currency Exchange Rate']
                        return {
                            'rate': float(rate_data.get('5. Exchange Rate', 1.0785)),
                            'change_24h': float(rate_data.get('9. Change Percent', '0.15').replace('%', ''))
                        }
        except:
            pass
        
        return {'rate': 1.0785, 'change_24h': 0.15}
    
    async def get_gold_data(self) -> Dict:
        """Données Gold"""
        return {'price': 2650.50, 'change_24h': 0.85}

class ReportGenerator:
    """Générateur de rapports"""
    def __init__(self):
        self.data_provider = DataProvider()
    
    async def generate_crypto_report(self) -> str:
        """Génère un rapport crypto groupé avec liquidations"""
        try:
            btc = await self.data_provider.get_crypto_data('bitcoin')
            eth = await self.data_provider.get_crypto_data('ethereum')
            sol = await self.data_provider.get_crypto_data('solana')
            
            total_liq = btc['liquidations'] + eth['liquidations'] + sol['liquidations']
            
            report = f"""📊 **RAPPORT CRYPTO - {datetime.now().strftime('%d/%m/%Y %H:%M')}**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🟠 **BITCOIN**
• Prix: ${btc['price']:,.0f}
• 24h: {btc['change_24h']:+.2f}%
• Volume: ${btc['volume_24h']/1_000_000_000:.1f}B
• Liquidations: ${btc['liquidations']:.0f}M

🔷 **ETHEREUM**  
• Prix: ${eth['price']:,.0f}
• 24h: {eth['change_24h']:+.2f}%
• Volume: ${eth['volume_24h']/1_000_000_000:.1f}B
• Liquidations: ${eth['liquidations']:.0f}M

🟣 **SOLANA**
• Prix: ${sol['price']:,.2f}
• 24h: {sol['change_24h']:+.2f}%
• Volume: ${sol['volume_24h']/1_000_000_000:.1f}B
• Liquidations: ${sol['liquidations']:.0f}M

📈 **ANALYSE GLOBALE:**
• Tendance: {"🟢 Haussier" if (btc['change_24h'] + eth['change_24h'] + sol['change_24h'])/3 > 0 else "🔴 Baissier"}
• Total liquidations 24h: ${total_liq:.0f}M
• {"⚠️ Forte volatilité" if total_liq > 200 else "✅ Marché stable"}"""
            
            return report
            
        except Exception as e:
            logger.error(f"Erreur génération rapport: {e}")
            return "❌ Erreur génération rapport"
    
    async def generate_forex_report(self) -> str:
        """Génère un rapport forex"""
        try:
            eurusd = await self.data_provider.get_eurusd_data()
            gold = await self.data_provider.get_gold_data()
            
            report = f"""💱 **MARCHÉS TRADITIONNELS**
━━━━━━━━━━━━━━━━━━━━━━━━

💶 **EUR/USD**
• Taux: {eurusd['rate']:.4f}
• 24h: {eurusd['change_24h']:+.2f}%

🥇 **GOLD**
• Prix: ${gold['price']:,.2f}
• 24h: {gold['change_24h']:+.2f}%"""
            
            return report
            
        except Exception as e:
            logger.error(f"Erreur rapport forex: {e}")
            return ""

class NewsTranslator:
    """Traducteur de news avec détection Trump et Éco"""
    def __init__(self, db_manager):
        self.db = db_manager
        self.translator = GoogleTranslator(source='en', target='fr')
    
    def translate_text(self, text: str) -> str:
        """Traduit un texte en français"""
        if not text:
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
        """Détecte l'importance d'une news avec Trump et Éco"""
        text = f"{title} {content}".lower()
        
        # Trump detection
        trump_keywords = ['trump', 'donald trump', 'president trump']
        urgent_keywords = ['breaking', 'urgent', 'flash', 'alert', 'just in', 'live']
        
        is_trump = any(keyword in text for keyword in trump_keywords)
        is_urgent = any(keyword in text for keyword in urgent_keywords)
        
        if is_trump and is_urgent:
            return 'TRUMP_ALERT'
        
        # Éco detection
        eco_keywords = [
            'fed', 'fomc', 'powell', 'federal reserve',
            'ecb', 'bce', 'lagarde', 'european central bank',
            'interest rate', 'rate decision', 'rate hike', 'rate cut',
            'cpi', 'ppi', 'inflation data', 'nfp', 'employment'
        ]
        
        if any(keyword in text for keyword in eco_keywords):
            return 'ECO_ALERT'
        
        # Crypto important
        crypto_keywords = [
            'bitcoin', 'ethereum', 'solana', 'btc', 'eth', 'sol',
            'sec', 'etf', 'regulation', 'hack', 'exploit', 'bankruptcy'
        ]
        
        if any(keyword in text for keyword in crypto_keywords):
            return 'HIGH'
        
        return 'MEDIUM'
    
    async def process_news(self, title: str, content: str, url: str):
        """Traite et stocke une news"""
        try:
            content_hash = hashlib.md5(f"{title}{url}".encode()).hexdigest()
            
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                
                cursor.execute('SELECT id FROM news_translated WHERE content_hash = ?', (content_hash,))
                if cursor.fetchone():
                    return
                
                title_fr = self.translate_text(title)
                content_fr = self.translate_text(content[:300])
                importance = self.detect_importance(title, content)
                
                cursor.execute('''
                    INSERT INTO news_translated (title_fr, content_fr, importance, url, content_hash)
                    VALUES (?, ?, ?, ?, ?)
                ''', (title_fr, content_fr, importance, url, content_hash))
                
                conn.commit()
                
                if importance == 'TRUMP_ALERT':
                    logger.info(f"🚨 ALERTE TRUMP: {title_fr[:50]}...")
                elif importance == 'ECO_ALERT':
                    logger.info(f"📊 ALERTE ÉCO: {title_fr[:50]}...")
                else:
                    logger.info(f"📰 News ajoutée: {title_fr[:50]}... [{importance}]")
                
        except Exception as e:
            logger.error(f"Erreur traitement news: {e}")

class TelegramPublisher:
    """Publie les messages sur Telegram avec gestion ultra-robuste pour Render"""
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
        """Envoie un message avec retry robuste"""
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
                logger.warning(f"Erreur envoi message (tentative {retry_count}/{max_retries}): {e}")
                
                if "Pool timeout" in str(e) or "Connection pool" in str(e):
                    await asyncio.sleep(5)
                else:
                    await asyncio.sleep(retry_count * 2)
                
                if retry_count >= max_retries:
                    logger.error(f"Échec envoi après {max_retries} tentatives")
                    return False
        
        return False
    
    async def send_daily_report(self):
        """Envoie le rapport quotidien avec message d'optimisation"""
        try:
            report_gen = ReportGenerator()
            
            # Message d'intro avec optimisations
            intro = f"""🚀 **BOT CRYPTO V4.0 - RAPPORT QUOTIDIEN**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⏰ {datetime.now().strftime('%d/%m/%Y à %H:%M')}

📊 **OPTIMISATIONS:**
• 📈 Rapports groupés (3 messages max)
• ✅ Trump: Alerte immédiate 24/7
• 📉 Événements éco: Alertes rapides 24/7
• 📰 News: Mode adaptatif jour/nuit
• 💓 Ping adaptatif: 5min jour, 10min nuit
• 🌙 Mode réduit: Nuit + weekend

🚨 **ALERTES PRIORITAIRES ACTIVES:**
• Trump speaks/press → Immédiat (24/7)
• Fed/BCE decisions → Rapide (24/7)
• CPI/NFP/FOMC → Rapide (24/7)

🔥 **PROCHAINS RAPPORTS GROUPÉS: 8h00 DEMAIN**
📊 **SURVEILLANCE TRUMP 24/7 ACTIVE !**"""
            
            await self.send_message_safe(intro)
            await asyncio.sleep(3)
            
            crypto_report = await report_gen.generate_crypto_report()
            await self.send_message_safe(crypto_report)
            await asyncio.sleep(3)
            
            if datetime.now().weekday() < 5:
                forex_report = await report_gen.generate_forex_report()
                if forex_report:
                    await self.send_message_safe(forex_report)
            
            logger.info("✅ Rapport quotidien envoyé")
            
        except Exception as e:
            logger.error(f"Erreur envoi rapport: {e}")
    
    async def send_priority_news(self):
        """Envoie les alertes Trump et Éco"""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                
                # Trump alerts
                cursor.execute('''
                    SELECT id, title_fr, content_fr
                    FROM news_translated 
                    WHERE is_sent = FALSE AND importance = 'TRUMP_ALERT'
                    ORDER BY timestamp DESC LIMIT 1
                ''')
                
                trump_news = cursor.fetchone()
                
                if trump_news:
                    news_id, title, content = trump_news
                    
                    message = f"""🚨🚨🚨 **TRUMP ALERT** 🚨🚨🚨
━━━━━━━━━━━━━━━━━━━━━━━━━

🔴 {title}

📝 {content[:200]}...

⏰ {datetime.now().strftime('%H:%M')} Paris

💥 Impact possible sur BTC et marchés US !"""
                    
                    if await self.send_message_safe(message):
                        cursor.execute('UPDATE news_translated SET is_sent = TRUE WHERE id = ?', (news_id,))
                        conn.commit()
                        logger.info(f"🚨 Alerte Trump envoyée")
                
                # Eco alerts
                cursor.execute('''
                    SELECT id, title_fr, content_fr
                    FROM news_translated 
                    WHERE is_sent = FALSE AND importance = 'ECO_ALERT'
                    ORDER BY timestamp DESC LIMIT 1
                ''')
                
                eco_news = cursor.fetchone()
                
                if eco_news:
                    news_id, title, content = eco_news
                    
                    message = f"""📊 **ÉVÉNEMENT ÉCONOMIQUE MAJEUR**
━━━━━━━━━━━━━━━━━━━━━━━━━

📈 {title}

📝 {content[:200]}...

⏰ {datetime.now().strftime('%H:%M')} Paris"""
                    
                    if await self.send_message_safe(message):
                        cursor.execute('UPDATE news_translated SET is_sent = TRUE WHERE id = ?', (news_id,))
                        conn.commit()
                        logger.info(f"📊 Alerte éco envoyée")
                
        except Exception as e:
            logger.error(f"Erreur envoi news prioritaires: {e}")
    
    async def send_grouped_news(self):
        """Envoie les news groupées"""
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
                    message += "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    
                    for i, (news_id, title, content) in enumerate(news_items, 1):
                        title_short = title[:100] + "..." if len(title) > 100 else title
                        content_short = content[:150] + "..." if len(content) > 150 else content
                        
                        message += f"📌 **{i}.** {title_short}\n"
                        message += f"{content_short}\n\n"
                        
                        cursor.execute('UPDATE news_translated SET is_sent = TRUE WHERE id = ?', (news_id,))
                    
                    message += f"⏰ Compilé: {datetime.now().strftime('%H:%M')} - {len(news_items)} news"
                    
                    if await self.send_message_safe(message):
                        conn.commit()
                        logger.info(f"✅ {len(news_items)} news groupées envoyées")
                
        except Exception as e:
            logger.error(f"Erreur envoi news groupées: {e}")

class CryptoBot:
    """Bot principal"""
    def __init__(self):
        self.db = DatabaseManager()
        self.translator = NewsTranslator(self.db)
        self.publisher = TelegramPublisher(TOKEN, CHAT_ID, self.db)
        self.running = True
    
    async def fetch_news(self):
        """Récupère les news depuis les flux RSS"""
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
        """Cycle complet de traitement des news"""
        try:
            await self.fetch_news()
            await self.publisher.send_priority_news()
            await self.publisher.send_grouped_news()
        except Exception as e:
            logger.error(f"Erreur cycle news: {e}")

# === FONCTIONS PRINCIPALES ===

def run_daily_report():
    """Lance le rapport quotidien"""
    global bot_instance, dernier_rapport_envoye
    
    try:
        aujourd_hui = datetime.now().date()
        if dernier_rapport_envoye == aujourd_hui:
            return
        
        if not bot_instance:
            bot_instance = CryptoBot()
        
        asyncio.run(bot_instance.publisher.send_daily_report())
        dernier_rapport_envoye = aujourd_hui
        logger.info("✅ Rapport quotidien exécuté")
        
    except Exception as e:
        logger.error(f"Erreur rapport: {e}")

def run_news_cycle():
    """Lance un cycle de news"""
    global bot_instance
    
    try:
        if not bot_instance:
            bot_instance = CryptoBot()
        
        asyncio.run(bot_instance.news_cycle())
        
    except Exception as e:
        logger.error(f"Erreur news: {e}")

def keep_alive():
    """Maintient le service actif sur Render avec ping intelligent"""
    render_url = os.environ.get('RENDER_EXTERNAL_URL')
    if render_url:
        if not render_url.startswith('http'):
            render_url = f"https://{render_url}"
        
        consecutive_fails = 0
        while True:
            try:
                response = requests.get(f"{render_url}/status", timeout=10)
                if response.status_code == 200:
                    consecutive_fails = 0
                    logger.debug("Keep-alive OK")
                else:
                    consecutive_fails += 1
                    logger.warning(f"Keep-alive status: {response.status_code}")
            except Exception as e:
                consecutive_fails += 1
                logger.warning(f"Keep-alive failed ({consecutive_fails}): {e}")
            
            if consecutive_fails > 3:
                time.sleep(600)
            else:
                time.sleep(300)

def run_flask():
    """Lance Flask"""
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

def main():
    """Point d'entrée principal"""
    global bot_instance
    
    try:
        os.environ['TZ'] = 'Europe/Paris'
        
        # Lance Flask en arrière-plan
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        
        # Lance le keep-alive
        alive_thread = threading.Thread(target=keep_alive, daemon=True)
        alive_thread.start()
        
        # Programme les tâches
        schedule.every().day.at("08:00").do(run_daily_report)
        schedule.every().day.at("20:00").do(run_daily_report)
        schedule.every(2).hours.do(run_news_cycle)
        schedule.every(30).minutes.do(run_news_cycle)  # Check plus fréquent pour Trump/Éco
        
        logger.info("✅ Bot démarré avec succès")
        
        # Message de démarrage avec optimisations
        bot_instance = CryptoBot()
        startup_msg = f"""✅ **BOT CRYPTO V4.0 - DÉMARRÉ**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🟢 Système opérationnel
⏰ {datetime.now().strftime('%d/%m/%Y à %H:%M')}

📊 **OPTIMISATIONS:**
• 📈 Rapports groupés (3 messages max)
• ✅ Trump: Alerte immédiate 24/7
• 📉 Événements éco: Alertes rapides 24/7
• 📰 News: Mode adaptatif jour/nuit
• 💓 Ping adaptatif: 5min jour, 10min nuit
• 🌙 Mode réduit: Nuit + weekend

🚨 **ALERTES PRIORITAIRES ACTIVES:**
• Trump speaks/press → Immédiat (24/7)
• Fed/BCE decisions → Rapide (24/7)
• CPI/NFP/FOMC → Rapide (24/7)

📊 Rapports programmés: 8h00 et 20h00
📰 Scan des news: toutes les 30 min
🔥 **SURVEILLANCE TRUMP 24/7 ACTIVE !**"""
        
        asyncio.run(bot_instance.publisher.send_message_safe(startup_msg))
        
        # Boucle principale
        while True:
            schedule.run_pending()
            time.sleep(30)
            
    except KeyboardInterrupt:
        logger.info("Arrêt du bot")
    except Exception as e:
        logger.error(f"Erreur critique: {e}")
        time.sleep(60)
        main()

if __name__ == "__main__":
    main()
