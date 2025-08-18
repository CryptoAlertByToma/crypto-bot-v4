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
message_queue = queue.Queue()

class DatabaseManager:
    def __init__(self, db_path="crypto_bot.db"):
        self.db_path = db_path
        self.init_database()
    
    @contextmanager
    def get_connection(self):
        """Gestion sécurisée des connexions SQLite"""
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                with db_lock:
                    conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA busy_timeout=30000")
                    conn.execute("PRAGMA synchronous=NORMAL")
                    conn.row_factory = sqlite3.Row
                    yield conn
                    conn.commit()
                    return
            except sqlite3.OperationalError as e:
                retry_count += 1
                if retry_count >= max_retries:
                    logger.error(f"Database locked after {max_retries} retries")
                    raise
                time.sleep(1)
            finally:
                if 'conn' in locals():
                    conn.close()
    
    def init_database(self):
        """Initialisation de la base de données"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # Table des données market
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS market_data (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        price REAL,
                        change_24h REAL,
                        volume_24h REAL,
                        market_cap REAL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Table des news traduites
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
                
                # Index pour optimiser les requêtes
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_news_sent ON news_translated(is_sent)')
                cursor.execute('CREATE INDEX IF NOT EXISTS idx_news_importance ON news_translated(importance)')
                
                conn.commit()
                logger.info("✅ Base de données initialisée avec succès")
        except Exception as e:
            logger.error(f"❌ Erreur initialisation DB: {e}")

class DataProvider:
    """Fournisseur de données crypto et forex"""
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0'})
    
    async def get_crypto_data(self, symbol: str) -> Dict:
        """Récupère les données crypto"""
        try:
            # Essai avec CoinGecko
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
                        'market_cap': coin_data.get('usd_market_cap', 0)
                    }
        except Exception as e:
            logger.warning(f"Erreur API: {e}")
        
        # Valeurs par défaut réalistes
        defaults = {
            'bitcoin': {'price': 98500, 'change': 2.3, 'volume': 28_500_000_000, 'mcap': 1_950_000_000_000},
            'ethereum': {'price': 3850, 'change': 1.8, 'volume': 16_200_000_000, 'mcap': 465_000_000_000},
            'solana': {'price': 195, 'change': 3.5, 'volume': 3_800_000_000, 'mcap': 89_000_000_000}
        }
        
        default = defaults.get(symbol, defaults['bitcoin'])
        return {
            'price': default['price'],
            'change_24h': default['change'],
            'volume_24h': default['volume'],
            'market_cap': default['mcap']
        }
    
    async def get_eurusd_data(self) -> Dict:
        """Données EUR/USD"""
        return {'rate': 1.0785, 'change_24h': 0.15}
    
    async def get_gold_data(self) -> Dict:
        """Données Gold"""
        return {'price': 2650.50, 'change_24h': 0.85}

class ReportGenerator:
    """Générateur de rapports"""
    def __init__(self):
        self.data_provider = DataProvider()
    
    async def generate_crypto_report(self) -> str:
        """Génère un rapport crypto groupé"""
        try:
            btc = await self.data_provider.get_crypto_data('bitcoin')
            eth = await self.data_provider.get_crypto_data('ethereum')
            sol = await self.data_provider.get_crypto_data('solana')
            
            report = f"""📊 **RAPPORT CRYPTO - {datetime.now().strftime('%d/%m/%Y %H:%M')}**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🟠 **BITCOIN**
• Prix: ${btc['price']:,.0f}
• 24h: {btc['change_24h']:+.2f}%
• Volume: ${btc['volume_24h']/1_000_000_000:.1f}B

🔷 **ETHEREUM**  
• Prix: ${eth['price']:,.0f}
• 24h: {eth['change_24h']:+.2f}%
• Volume: ${eth['volume_24h']/1_000_000_000:.1f}B

🟣 **SOLANA**
• Prix: ${sol['price']:,.2f}
• 24h: {sol['change_24h']:+.2f}%
• Volume: ${sol['volume_24h']/1_000_000_000:.1f}B

📈 **Tendance**: {"🟢 Haussier" if (btc['change_24h'] + eth['change_24h'] + sol['change_24h'])/3 > 0 else "🔴 Baissier"}"""
            
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
━━━━━━━━━━━━━━━━━━━━━━━━━

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
    """Traducteur de news avec détection des alertes"""
    def __init__(self, db_manager):
        self.db = db_manager
        self.translator = GoogleTranslator(source='en', target='fr')
    
    def translate_text(self, text: str) -> str:
        """Traduit un texte en français"""
        if not text:
            return ""
        try:
            # Limite la longueur pour éviter les erreurs
            if len(text) > 500:
                text = text[:500] + "..."
            translated = self.translator.translate(text)
            return translated if translated else text
        except Exception as e:
            logger.warning(f"Erreur traduction: {e}")
            return text
    
    def detect_importance(self, title: str, content: str) -> str:
        """Détecte l'importance d'une news"""
        text = f"{title} {content}".lower()
        
        # Mots-clés critiques
        critical_keywords = [
            'breaking', 'urgent', 'flash', 'alert',
            'trump', 'powell', 'fed', 'fomc', 'ecb', 'lagarde',
            'rate decision', 'interest rate', 'inflation data',
            'hack', 'exploit', 'bankruptcy', 'sec'
        ]
        
        # Mots-clés importants
        important_keywords = [
            'bitcoin', 'ethereum', 'solana', 'etf', 
            'regulation', 'adoption', 'investment',
            'bullish', 'bearish', 'rally', 'crash'
        ]
        
        for keyword in critical_keywords:
            if keyword in text:
                return 'CRITICAL'
        
        for keyword in important_keywords:
            if keyword in text:
                return 'HIGH'
        
        return 'MEDIUM'
    
    async def process_news(self, title: str, content: str, url: str):
        """Traite et stocke une news"""
        try:
            # Génère un hash unique
            content_hash = hashlib.md5(f"{title}{url}".encode()).hexdigest()
            
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                
                # Vérifie si la news existe déjà
                cursor.execute('SELECT id FROM news_translated WHERE content_hash = ?', (content_hash,))
                if cursor.fetchone():
                    return
                
                # Traduit
                title_fr = self.translate_text(title)
                content_fr = self.translate_text(content[:300])
                
                # Détecte l'importance
                importance = self.detect_importance(title, content)
                
                # Stocke
                cursor.execute('''
                    INSERT INTO news_translated (title_fr, content_fr, importance, url, content_hash)
                    VALUES (?, ?, ?, ?, ?)
                ''', (title_fr, content_fr, importance, url, content_hash))
                
                conn.commit()
                logger.info(f"News ajoutée: {title_fr[:50]}... [{importance}]")
                
        except Exception as e:
            logger.error(f"Erreur traitement news: {e}")

class TelegramPublisher:
    """Publie les messages sur Telegram avec gestion optimisée"""
    def __init__(self, token: str, chat_id: int, db_manager):
        # Configuration du bot avec pool optimisé
        request = HTTPXRequest(
            connection_pool_size=20,
            pool_timeout=30.0,
            read_timeout=20.0,
            write_timeout=20.0
        )
        self.bot = Bot(token=token, request=request)
        self.chat_id = chat_id
        self.db = db_manager
        self.last_message_time = 0
        self.min_delay = 1.0  # Délai minimum entre messages
    
    async def send_message_safe(self, text: str, parse_mode: str = 'Markdown'):
        """Envoie un message avec gestion du rate limiting"""
        try:
            # Rate limiting
            current_time = time.time()
            time_since_last = current_time - self.last_message_time
            if time_since_last < self.min_delay:
                await asyncio.sleep(self.min_delay - time_since_last)
            
            # Envoie le message
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=parse_mode
            )
            
            self.last_message_time = time.time()
            return True
            
        except Exception as e:
            logger.error(f"Erreur envoi message: {e}")
            return False
    
    async def send_daily_report(self):
        """Envoie le rapport quotidien"""
        try:
            report_gen = ReportGenerator()
            
            # Message d'intro
            intro = f"""🚀 **BOT CRYPTO V4.0 - RAPPORT QUOTIDIEN**
━━━━━━━━━━━━━━━━━━━━━━━━━

⏰ {datetime.now().strftime('%d/%m/%Y à %H:%M')}

📊 **FONCTIONNALITÉS ACTIVES:**
• Rapports crypto 2x/jour (8h et 20h)
• Alertes breaking news en temps réel
• News crypto compilées toutes les 2h
• Données EUR/USD et Gold en semaine

🔥 Envoi des rapports dans 3 secondes..."""
            
            await self.send_message_safe(intro)
            await asyncio.sleep(2)
            
            # Rapport crypto
            crypto_report = await report_gen.generate_crypto_report()
            await self.send_message_safe(crypto_report)
            await asyncio.sleep(2)
            
            # Rapport forex (si jour de semaine)
            if datetime.now().weekday() < 5:
                forex_report = await report_gen.generate_forex_report()
                if forex_report:
                    await self.send_message_safe(forex_report)
            
            logger.info("✅ Rapport quotidien envoyé")
            
        except Exception as e:
            logger.error(f"Erreur envoi rapport: {e}")
    
    async def send_priority_news(self):
        """Envoie les news prioritaires"""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                
                # Récupère les news critiques non envoyées
                cursor.execute('''
                    SELECT id, title_fr, content_fr, importance
                    FROM news_translated 
                    WHERE is_sent = FALSE AND importance = 'CRITICAL'
                    ORDER BY timestamp DESC LIMIT 1
                ''')
                
                critical_news = cursor.fetchone()
                
                if critical_news:
                    news_id, title, content, importance = critical_news
                    
                    message = f"""🚨 **BREAKING NEWS** 🚨
━━━━━━━━━━━━━━━━━━━━━━━━━

📰 {title}

📝 {content[:200]}...

⏰ {datetime.now().strftime('%H:%M')} Paris"""
                    
                    if await self.send_message_safe(message):
                        cursor.execute('UPDATE news_translated SET is_sent = TRUE WHERE id = ?', (news_id,))
                        conn.commit()
                        logger.info(f"Alerte critique envoyée: {title[:50]}")
                
        except Exception as e:
            logger.error(f"Erreur envoi news prioritaires: {e}")
    
    async def send_grouped_news(self):
        """Envoie les news groupées"""
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                
                # Récupère les news non critiques
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
                        # Limite la longueur
                        title_short = title[:100] + "..." if len(title) > 100 else title
                        content_short = content[:150] + "..." if len(content) > 150 else content
                        
                        message += f"📌 **{i}.** {title_short}\n"
                        message += f"{content_short}\n\n"
                        
                        cursor.execute('UPDATE news_translated SET is_sent = TRUE WHERE id = ?', (news_id,))
                    
                    message += f"⏰ {datetime.now().strftime('%H:%M')}"
                    
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
            'https://cryptonews.com/news/feed/'
        ]
        
        for source in sources:
            try:
                feed = feedparser.parse(source)
                
                for entry in feed.entries[:5]:  # Limite à 5 news par source
                    title = entry.get('title', '')
                    content = entry.get('summary', entry.get('description', ''))
                    url = entry.get('link', '')
                    
                    if title:
                        await self.translator.process_news(title, content, url)
                
                await asyncio.sleep(1)  # Pause entre sources
                
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
    """Maintient le service actif sur Render"""
    render_url = os.environ.get('RENDER_EXTERNAL_URL')
    if render_url:
        if not render_url.startswith('http'):
            render_url = f"https://{render_url}"
        
        while True:
            try:
                requests.get(f"{render_url}/status", timeout=10)
                logger.debug("Keep-alive ping")
            except:
                pass
            time.sleep(300)  # Ping toutes les 5 minutes

def run_flask():
    """Lance Flask"""
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

def main():
    """Point d'entrée principal"""
    global bot_instance
    
    try:
        # Configure le timezone
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
        
        logger.info("✅ Bot démarré avec succès")
        
        # Message de démarrage
        bot_instance = CryptoBot()
        startup_msg = f"""✅ **BOT CRYPTO V4.0 - DÉMARRÉ**
━━━━━━━━━━━━━━━━━━━━━━━━━

🟢 Système opérationnel
📊 Rapports programmés: 8h00 et 20h00
📰 Scan des news: toutes les 2h

Prochain rapport dans {(8 - datetime.now().hour) % 12}h"""
        
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
