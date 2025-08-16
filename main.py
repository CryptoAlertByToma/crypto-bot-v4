# 🎯 OPTIMISATION HEURES RENDER - ÉCONOMIE 200H/MOIS

import os
import schedule
from datetime import datetime, time as dt_time

def is_market_hours() -> bool:
    """Détecte si c'est les heures de marché importantes"""
    now = datetime.now()
    current_time = now.time()
    weekday = now.weekday()  # 0=Lundi, 6=Dimanche
    
    # Weekend = mode minimal
    if weekday >= 5:  # Samedi-Dimanche
        return False
    
    # Semaine = heures importantes seulement
    # 6h-22h Paris (heures marchés US/EU/ASIA)
    market_start = dt_time(6, 0)
    market_end = dt_time(22, 0)
    
    return market_start <= current_time <= market_end

def schedule_optimized_tasks():
    """Programmation optimisée selon les heures"""
    
    # === RAPPORTS QUOTIDIENS (INCHANGÉS) ===
    schedule.every().monday.at("08:00").do(send_daily_reports)
    schedule.every().tuesday.at("08:00").do(send_daily_reports)
    schedule.every().wednesday.at("08:00").do(send_daily_reports)
    schedule.every().thursday.at("08:00").do(send_daily_reports)
    schedule.every().friday.at("08:00").do(send_daily_reports)
    
    # Secours 8h15
    schedule.every().monday.at("08:15").do(send_daily_reports_backup)
    schedule.every().tuesday.at("08:15").do(send_daily_reports_backup)
    schedule.every().wednesday.at("08:15").do(send_daily_reports_backup)
    schedule.every().thursday.at("08:15").do(send_daily_reports_backup)
    schedule.every().friday.at("08:15").do(send_daily_reports_backup)

def optimized_main_loop():
    """Boucle principale optimisée selon les heures"""
    
    while True:
        try:
            current_time = datetime.now()
            weekday = current_time.weekday()
            hour = current_time.hour
            
            # === MODE WEEKEND (Vendredi 22h → Lundi 6h) ===
            if weekday >= 5 or (weekday == 4 and hour >= 22) or (weekday == 0 and hour < 6):
                print("😴 Mode Weekend - Économie d'énergie")
                
                # Tâches minimales weekend
                schedule.run_pending()
                
                # Sleep plus long = économie
                time.sleep(300)  # 5 minutes au lieu de 60
                continue
            
            # === MODE NUIT SEMAINE (22h → 6h) ===
            elif not is_market_hours():
                print("🌙 Mode Nuit - Économie partielle")
                
                # News importantes seulement (Trump, crises)
                if current_time.minute % 60 == 0:  # 1x par heure au lieu de 30min
                    await check_urgent_news_only()
                
                schedule.run_pending()
                time.sleep(180)  # 3 minutes au lieu de 60
                continue
            
            # === MODE JOUR SEMAINE (6h → 22h) ===
            else:
                print("🚀 Mode Actif - Surveillance complète")
                
                # News cycle normal
                if current_time.minute % 30 == 0:
                    await news_cycle()
                
                # Événements économiques
                if current_time.minute % 5 == 0:
                    await check_economic_events()
                
                schedule.run_pending()
                time.sleep(60)  # Normal
        
        except Exception as e:
            print(f"❌ Erreur: {e}")
            time.sleep(120)

async def check_urgent_news_only():
    """Mode nuit - News urgentes seulement (Trump, crashes)"""
    try:
        # Cherche seulement Trump et crises majeures
        urgent_keywords = ['trump', 'crash', 'hack', 'regulation ban', 'fed emergency']
        
        # Votre code news existant mais filtré
        # ... (ne traite que les news avec mots-clés urgents)
        
    except Exception as e:
        print(f"❌ Erreur news urgentes: {e}")

# === CALCUL D'ÉCONOMIES ===
def calculate_monthly_hours():
    """Calcule les heures mensuelles avec optimisation"""
    
    # AVANT: 24/7 = 728h/mois
    heures_avant = 24 * 7 * 4.33
    
    # APRÈS OPTIMISATION:
    # - Weekend: 48h × 4.33 semaines = 208h (au lieu de 208h)
    # - Nuit semaine: 16h × 5 jours × 4.33 = 347h (au lieu de 347h)  
    # - Jour semaine: 16h × 5 jours × 4.33 = 347h (normal)
    
    # Weekend: Sleep 5min au lieu de 1min = Économie 75%
    weekend_optimise = 208 * 0.25  # 52h
    
    # Nuit: Sleep 3min au lieu de 1min = Économie 66%
    nuit_optimise = 347 * 0.34  # 118h
    
    # Jour: Normal
    jour_normal = 347  # 347h
    
    heures_apres = weekend_optimise + nuit_optimise + jour_normal
    economie = heures_avant - heures_apres
    
    return {
        'avant': heures_avant,
        'apres': heures_apres,
        'economie': economie,
        'pourcentage': (economie / heures_avant) * 100
    }

# RÉSULTATS ÉCONOMIES
resultats = calculate_monthly_hours()
print(f"""
📊 CALCUL ÉCONOMIES RENDER:

🔴 AVANT: {resultats['avant']:.0f}h/mois (dépassement!)
🟢 APRÈS: {resultats['apres']:.0f}h/mois 
💰 ÉCONOMIE: {resultats['economie']:.0f}h/mois ({resultats['pourcentage']:.1f}%)
✅ MARGE: {750 - resultats['apres']:.0f}h restantes

🎯 STRATÉGIE:
• Weekend: Mode veille (économie 75%)
• Nuit: Mode partiel (économie 66%) 
• Jour: Mode normal (surveillance complète)
• Rapports: Toujours à l'heure (8h00)
""")
