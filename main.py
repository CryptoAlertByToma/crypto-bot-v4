# bot_crypto_v4_fixed.py
import asyncio
import sqlite3
import requests
import hashlib
import logging
import os
import time
import threading
from datetime import datetime
from typing import Dict
from contextlib import contextmanager

import feedparser
from deep_translator import GoogleTranslator
from flask import Flask
from telegram import Bot
from telegram.request import HTTPXRequest

# ========= CONFIG =========
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '8050724073:AAHugCqSuHUWPOJXJUFoH7TlEptW_jB-790')
CHAT_ID = int(os.environ.get('CHAT_ID', '5926402259'))
COINGLASS_API_KEY = os.environ.get('COINGLASS_API_KEY', 'f8ca50e46d2e460eb4465a754fb9a9bf')
ALPHA_VANTAGE_KEY = os.environ.get('ALPHA_VANTAGE_KEY', '4J51YB27HHDW6X62')

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("bot")

# Flask (Render healthcheck)
app = Flask(__name__)
@app.get("/")
def home(): return "Bot Crypto V4.1 actif"
@app.get("/status")
def status(): return {"status":"active","time":datetime.now().isoformat()}

# Globals
db_lock = threading.Lock()
job_lock = threading.Lock()          # évite que deux jobs tournent en même temps
send_lock = asyncio.Lock()           # sérialise tous les envois Telegram
bot_instance = None
last_report_date = None

# ========= DB =========
class DatabaseManager:
    def __init__(self, path="crypto_bot.db"):
        self.path = path
        self.init()

    @contextmanager
    def conn(self):
        max_retries, tries, c = 5, 0, None
        while True:
            try:
                with db_lock:
                    c = sqlite3.connect(self.path, timeout=60.0, check_same_thread=False, isolation_level="DEFERRED")
                    c.execute("PRAGMA journal_mode=WAL")
                    c.execute("PRAGMA busy_timeout=60000")
                    c.row_factory = sqlite3.Row
                    yield c
                    if c.in_transaction:
                        c.commit()
                return
            except sqlite3.OperationalError as e:
                tries += 1
                if tries >= max_retries: 
                    logger.error(f"DB locked after {tries} retries: {e}")
                    raise
                time.sleep(0.5 * tries)
            finally:
                if c:
                    try: c.close()
                    except: pass

    def init(self):
        try:
            with self.conn() as c:
                cur = c.cursor()
                cur.execute("""
                CREATE TABLE IF NOT EXISTS news_translated(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title_fr TEXT, content_fr TEXT, importance TEXT,
                    url TEXT, is_sent BOOLEAN DEFAULT 0,
                    content_hash TEXT UNIQUE, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )""")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_news_sent ON news_translated(is_sent)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_news_importance ON news_translated(importance)")
                c.commit()
            logger.info("✅ DB OK")
        except Exception as e:
            logger.exception("DB init error: %s", e)

# ========= DATA =========
class DataProvider:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent":"Mozilla/5.0"})

    def _get(self, url, **kw):
        return self.s.get(url, timeout=kw.pop("timeout", 8), **kw)

    async def crypto(self, sym: str) -> Dict:
        """
        Priorité: Binance 24h ticker (BTCUSDT/ETHUSDT/SOLUSDT)
        Fallback: CoinGecko simple/price
        """
        map_binance = {"bitcoin":"BTCUSDT","ethereum":"ETHUSDT","solana":"SOLUSDT"}
        symbol = map_binance.get(sym, "BTCUSDT")
        # 1) Binance
        try:
            r = self._get("https://api.binance.com/api/v3/ticker/24hr", params={"symbol":symbol})
            if r.status_code == 200:
                j = r.json()
                price = float(j["lastPrice"])
                change = float(j["priceChangePercent"])
                vol = float(j["quoteVolume"])
                return {"price":price, "change_24h":change, "volume_24h":vol, "market_cap":0, "liquidations":0}
        except Exception as e:
            logger.debug(f"Binance fail {sym}: {e}")

        # 2) CoinGecko
        try:
            gecko_id = {"bitcoin":"bitcoin","ethereum":"ethereum","solana":"solana"}.get(sym, sym)
            r = self._get("https://api.coingecko.com/api/v3/simple/price",
                          params={"ids":gecko_id,"vs_currencies":"usd",
                                  "include_24hr_change":"true","include_24hr_vol":"true","include_market_cap":"true"})
            if r.status_code == 200:
                d = r.json().get(gecko_id, {})
                return {
                    "price": d.get("usd", 0.0),
                    "change_24h": d.get("usd_24h_change", 0.0),
                    "volume_24h": d.get("usd_24h_vol", 0.0),
                    "market_cap": d.get("usd_market_cap", 0.0),
                    "liquidations": 0
                }
        except Exception as e:
            logger.debug(f"Gecko fail {sym}: {e}")

        # 3) défauts
        defaults = {
            "bitcoin": (98500, 2.3, 28_500_000_000, 0, 0),
            "ethereum": (3850, 1.8, 16_200_000_000, 0, 0),
            "solana": (195, 3.5, 3_800_000_000, 0, 0),
        }
        p,c,v,m,l = defaults.get(sym, defaults["bitcoin"])
        return {"price":p,"change_24h":c,"volume_24h":v,"market_cap":m,"liquidations":l}

    async def eurusd(self) -> Dict:
        try:
            if ALPHA_VANTAGE_KEY:
                r = self._get("https://www.alphavantage.co/query",
                              params={"function":"CURRENCY_EXCHANGE_RATE","from_currency":"EUR","to_currency":"USD","apikey":ALPHA_VANTAGE_KEY},
                              timeout=6)
                if r.status_code == 200:
                    j = r.json().get("Realtime Currency Exchange Rate", {})
                    rate = float(j.get("5. Exchange Rate", 1.08))
                    ch = j.get("9. Change Percent", "0%").replace("%","")
                    try: ch = float(ch)
                    except: ch = 0.0
                    return {"rate":rate, "change_24h":ch}
        except Exception as e:
            logger.debug(f"AV fail: {e}")
        return {"rate":1.0785, "change_24h":0.15}

    async def gold(self) -> Dict:
        # branche ta vraie API si tu veux un cours live
        return {"price":2650.50, "change_24h":0.85}

# ========= NEWS =========
class NewsTranslator:
    def __init__(self, db: DatabaseManager):
        self.db = db
        self.tr = GoogleTranslator(source="en", target="fr")

    def translate(self, txt: str) -> str:
        if not txt: return ""
        try:
            t = txt if len(txt) <= 500 else txt[:500]+"..."
            res = self.tr.translate(t)
            return res or t
        except Exception:
            return txt

    def importance(self, title: str, content: str) -> str:
        t = f"{title} {content}".lower()

        trump = any(k in t for k in ["trump","donald trump","president trump"])
        urgent = any(k in t for k in ["breaking","urgent","just in","live"])
        if trump and urgent: return "TRUMP_ALERT"

        eco = any(k in t for k in [
            "fed","fomc","powell","federal reserve","ecb","bce","lagarde",
            "interest rate","rate decision","cpi","ppi","inflation","nfp","employment"
        ])
        if eco: return "ECO_ALERT"

        institutions = [
            "blackrock","microstrategy","grayscale","fidelity","ark invest","vanguard",
            "goldman sachs","jpmorgan","jp morgan","morgan stanley",
            "sec approves","etf inflow","etf outflow","spot etf","sec filing","s-1",
        ]
        if any(k in t for k in institutions): return "INSTITUTION_ALERT"

        if any(k in t for k in ["bitcoin","ethereum","solana","btc","eth","sol","sec","etf","regulation","hack","exploit"]):
            return "HIGH"
        return "MEDIUM"

    async def store(self, title: str, content: str, url: str):
        try:
            h = hashlib.md5(f"{title}{url}".encode()).hexdigest()
            with self.db.conn() as c:
                cur = c.cursor()
                cur.execute("SELECT 1 FROM news_translated WHERE content_hash=?", (h,))
                if cur.fetchone(): return
                cur.execute("""
                    INSERT INTO news_translated(title_fr,content_fr,importance,url,content_hash)
                    VALUES(?,?,?,?,?)
                """, (
                    self.translate(title),
                    self.translate(content[:300]),
                    self.importance(title, content),
                    url, h
                ))
                c.commit()
        except Exception as e:
            logger.exception("store news error: %s", e)

# ========= TELEGRAM =========
class TelegramPublisher:
    def __init__(self, token: str, chat_id: int, db: DatabaseManager):
        request = HTTPXRequest(
            connection_pool_size=80,   # ↑
            pool_timeout=90.0,         # ↑
            read_timeout=40.0,
            write_timeout=40.0,
            connect_timeout=40.0,
        )
        self.bot = Bot(token=token, request=request)
        self.chat_id = chat_id
        self.db = db
        self.min_delay = 1.8
        self._last = 0.0

    async def send(self, text: str, parse_mode: str = "HTML") -> bool:
        # Séquence unique → évite le “Pool timeout”
        async with send_lock:
            # petit spacing anti-flood
            now = time.time()
            if now - self._last < self.min_delay:
                await asyncio.sleep(self.min_delay - (now - self._last))
            tries = 0
            while tries < 3:
                try:
                    await self.bot.send_message(self.chat_id, text, parse_mode=parse_mode, disable_web_page_preview=True)
                    self._last = time.time()
                    return True
                except Exception as e:
                    tries += 1
                    logger.warning(f"Send fail ({tries}/3): {e}")
                    await asyncio.sleep(2 * tries)
            logger.error("❌ Envoi abandonné après 3 essais")
            return False

# ========= REPORTS =========
class ReportGenerator:
    def __init__(self):
        self.dp = DataProvider()

    async def crypto(self) -> str:
        btc, eth, sol = await self.dp.crypto("bitcoin"), await self.dp.crypto("ethereum"), await self.dp.crypto("solana")
        trend = "🟢 Haussier" if (btc["change_24h"]+eth["change_24h"]+sol["change_24h"])/3 > 0 else "🔴 Baissier"
        def b(x): return f"{x/1_000_000_000:.1f}B"
        return (
            f"<b>📊 RAPPORT CRYPTO — {datetime.now().strftime('%d/%m/%Y %H:%M')}</b>\n"
            f"────────────────────────────────\n\n"
            f"🟠 <b>BITCOIN</b>\n"
            f"• Prix : <b>${btc['price']:,.0f}</b>\n"
            f"• 24h : <b>{btc['change_24h']:+.2f}%</b>\n"
            f"• Volume : ${b(btc['volume_24h'])}\n\n"
            f"🔷 <b>ETHEREUM</b>\n"
            f"• Prix : <b>${eth['price']:,.0f}</b>\n"
            f"• 24h : <b>{eth['change_24h']:+.2f}%</b>\n"
            f"• Volume : ${b(eth['volume_24h'])}\n\n"
            f"🟣 <b>SOLANA</b>\n"
            f"• Prix : <b>${sol['price']:,.2f}</b>\n"
            f"• 24h : <b>{sol['change_24h']:+.2f}%</b>\n"
            f"• Volume : ${b(sol['volume_24h'])}\n\n"
            f"📈 <b>Analyse</b> : {trend}"
        )

    async def forex(self) -> str:
        fx, au = await self.dp.eurusd(), await self.dp.gold()
        return (
            f"<b>💱 MARCHÉS TRADITIONNELS</b>\n"
            f"──────────────────────────\n\n"
            f"💶 <b>EUR/USD</b>\n"
            f"• Taux : <b>{fx['rate']:.4f}</b>\n"
            f"• 24h : <b>{fx['change_24h']:+.2f}%</b>\n\n"
            f"🥇 <b>OR</b>\n"
            f"• Prix : <b>${au['price']:,.2f}</b>\n"
            f"• 24h : <b>{au['change_24h']:+.2f}%</b>"
        )

# ========= CORE =========
class CryptoBot:
    def __init__(self):
        self.db = DatabaseManager()
        self.news = NewsTranslator(self.db)
        self.pub = TelegramPublisher(TOKEN, CHAT_ID, self.db)
        self.dp = DataProvider()
        self.rg = ReportGenerator()

    async def fetch_news(self):
        feeds = [
            'https://cointelegraph.com/rss',
            'https://www.coindesk.com/arc/outboundfeeds/rss/',
            'https://cryptonews.com/news/feed/',
            'https://feeds.reuters.com/reuters/topNews',
            'https://rss.cnn.com/rss/edition.rss',
        ]
        for f in feeds:
            try:
                feed = feedparser.parse(f)
                limit = 3 if ('reuters' in f or 'cnn' in f) else 5
                for e in feed.entries[:limit]:
                    await self.news.store(
                        e.get("title",""),
                        e.get("summary", e.get("description","")),
                        e.get("link","")
                    )
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(f"Feed fail {f}: {e}")

    async def push_priority_news(self):
        try:
            with self.db.conn() as c:
                cur = c.cursor()
                for tag, label, icon in [
                    ("TRUMP_ALERT", "TRUMP ALERT", "🚨"),
                    ("ECO_ALERT", "ÉVÉNEMENT ÉCO", "📊"),
                    ("INSTITUTION_ALERT", "INSTITUTIONNEL", "🏦"),
                ]:
                    cur.execute("""SELECT id,title_fr,content_fr FROM news_translated
                                   WHERE is_sent=0 AND importance=? ORDER BY timestamp DESC LIMIT 1""",(tag,))
                    row = cur.fetchone()
                    if not row: continue
                    mid, title, content = row
                    msg = (
                        f"{icon} <b>{label}</b>\n"
                        f"────────────────────────\n\n"
                        f"• {title}\n\n"
                        f"{content[:220]}...\n\n"
                        f"⏰ {datetime.now().strftime('%H:%M')} Paris"
                    )
                    if await self.pub.send(msg):
                        cur.execute("UPDATE news_translated SET is_sent=1 WHERE id=?", (mid,))
                        c.commit()
        except Exception as e:
            logger.exception("priority news error: %s", e)

    async def push_digest(self):
        try:
            with self.db.conn() as c:
                cur = c.cursor()
                cur.execute("""SELECT id,title_fr,content_fr FROM news_translated
                               WHERE is_sent=0 AND importance IN ('HIGH','MEDIUM')
                               ORDER BY timestamp DESC LIMIT 3""")
                rows = cur.fetchall()
                if not rows: return
                msg = "<b>📰 CRYPTO NEWS DIGEST</b>\n────────────────────────\n\n"
                for i,(mid,title,content) in enumerate(rows,1):
                    t = title if len(title)<=110 else title[:110]+"…"
                    ct = content if len(content)<=160 else content[:160]+"…"
                    msg += f"• <b>{i}.</b> {t}\n{ct}\n\n"
                    cur.execute("UPDATE news_translated SET is_sent=1 WHERE id=?", (mid,))
                msg += f"⏰ Compilé {datetime.now().strftime('%H:%M')} — {len(rows)} news"
                if await self.pub.send(msg):
                    c.commit()
        except Exception as e:
            logger.exception("digest error: %s", e)

    async def send_daily_report(self):
        intro = (
            f"✅ <b>BOT CRYPTO V4.1 — DÉMARRAGE</b>\n"
            f"────────────────────────────────\n\n"
            f"⏰ {datetime.now().strftime('%d/%m/%Y %H:%M')} — Système opérationnel\n"
            f"• Rapports groupés\n• Alertes Trump/Éco/Institutions\n• Scan news intelligent"
        )
        await self.pub.send(intro)
        await asyncio.sleep(1.5)
       await self.pub.send(await self.rg.crypto())
        await asyncio.sleep(1.5)
        await self.pub.send(await self.rg.forex())

    async def news_cycle(self):
        await self.fetch_news()
        await self.push_priority_news()
        await self.push_digest()

# ========= SCHEDULER =========
import schedule

def run_async(coro):
    # Utilitaire pour exécuter une coroutine de façon sûre (une seule boucle à la fois)
    asyncio.run(coro)

def job_daily_report():
    global bot_instance, last_report_date
    with job_lock:  # évite chevauchements avec d'autres jobs
        try:
            today = datetime.now().date()
            if last_report_date == today: return
            if bot_instance is None: 
                logger.error("Bot non initialisé")
                return
            run_async(bot_instance.send_daily_report())
            last_report_date = today
        except Exception as e:
            logger.exception("daily report job: %s", e)

def job_news_cycle():
    global bot_instance
    with job_lock:
        try:
            if bot_instance is None:
                logger.error("Bot non initialisé")
                return
            run_async(bot_instance.news_cycle())
        except Exception as e:
            logger.exception("news job: %s", e)

# ========= KEEP-ALIVE / FLASK =========
def keep_alive():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url: return
    if not url.startswith("http"): url = "https://" + url
    s = requests.Session()
    fails = 0
    while True:
        try:
            r = s.get(f"{url}/status", timeout=8)
            fails = 0 if r.status_code == 200 else fails + 1
        except Exception:
            fails += 1
        time.sleep(600 if fails>2 else 300)

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)

# ========= MAIN =========
def main():
    global bot_instance
    os.environ["TZ"] = "Europe/Paris"

    # threads services
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()

    # instance unique du bot
    bot_instance = CryptoBot()

    # message de démarrage
    try:
        run_async(bot_instance.pub.send("🟢 <b>BOT DÉMARRÉ</b> — Surveillance active"))
    except Exception as e:
        logger.warning("Start message fail: %s", e)

    # planification
    schedule.every().day.at("08:00").do(job_daily_report)
    schedule.every().day.at("20:00").do(job_daily_report)
    schedule.every(2).hours.do(job_news_cycle)
    schedule.every(30).minutes.do(job_news_cycle)

    logger.info("✅ Scheduler prêt")
    while True:
        schedule.run_pending()
        time.sleep(10)

if __name__ == "__main__":
    main()


