import os
import json
import hmac
import hashlib
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# Config
SHOPIFY_TOKEN = os.environ.get("SHOPIFY_TOKEN")
SHOPIFY_SHOP = os.environ.get("SHOPIFY_SHOP")
RECHARGE_TOKEN = os.environ.get("RECHARGE_TOKEN")
SHOPIFY_SKU = os.environ.get("SHOPIFY_SKU", "AULJP")
SHOPIFY_WEBHOOK_SECRET = os.environ.get("SHOPIFY_WEBHOOK_SECRET")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
GOOGLE_SERVICE_ACCOUNT = os.environ.get("GOOGLE_SERVICE_ACCOUNT")
GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD")
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "hello@jilypet.com")
ALERT_THRESHOLD_MONTHS = 3

# Shopify API
def shopify_get(endpoint):
    url = f"https://{SHOPIFY_SHOP}/admin/api/2026-04/{endpoint}"
    headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN}
    r = requests.get(url, headers=headers)
    return r.json()

def shopify_put(endpoint, data):
    url = f"https://{SHOPIFY_SHOP}/admin/api/2026-04/{endpoint}"
    headers = {"X-Shopify-Access-Token": SHOPIFY_TOKEN, "Content-Type": "application/json"}
    r = requests.put(url, headers=headers, json=data)
    return r.json()

# Recharge API
def recharge_get(endpoint):
    url = f"https://api.rechargeapps.com/{endpoint}"
    headers = {"X-Recharge-Access-Token": RECHARGE_TOKEN}
    r = requests.get(url, headers=headers)
    return r.json()

# Google Sheets
def get_sheet():
    if not GOOGLE_SERVICE_ACCOUNT or not GOOGLE_SHEET_ID:
        return None
    try:
        creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT)
        scopes = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        return gc.open_by_key(GOOGLE_SHEET_ID)
    except Exception as e:
        print(f"Sheets error: {e}")
        return None

def log_movement(order_id, sacs, mouvement, raison):
    sh = get_sheet()
    if not sh:
        return
    try:
        ws = sh.worksheet("Stock")
        ws.append_row([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            order_id,
            mouvement,
            sacs,
            raison
        ])
    except Exception as e:
        print(f"Sheet log error: {e}")

# Calcul nb sacs depuis nom bundle
def extract_sacs_from_bundle(bundle_name):
    if not bundle_name:
        return 1
    bundle_lower = bundle_name.lower()
    for n in range(5, 0, -1):
        if f"{n} chat" in bundle_lower:
            return n
    return 1

# Calcul nb sacs depuis line items commande
def get_sacs_from_order(order):
    total_sacs = 0
    for item in order.get("line_items", []):
        sku = item.get("sku", "")
        if sku == SHOPIFY_SKU:
            total_sacs += item.get("quantity", 1)
    return total_sacs if total_sacs > 0 else 1

# Stock Shopify
def get_current_stock():
    try:
        data = shopify_get(f"products.json?fields=variants")
        for product in data.get("products", []):
            for variant in product.get("variants", []):
                if variant.get("sku") == SHOPIFY_SKU:
                    return variant.get("inventory_quantity", 0), variant.get("id")
        return 0, None
    except Exception as e:
        print(f"Stock error: {e}")
        return 0, None

def update_stock(variant_id, new_qty):
    try:
        shopify_put(f"variants/{variant_id}.json", {"variant": {"id": variant_id, "inventory_quantity": new_qty}})
    except Exception as e:
        print(f"Update stock error: {e}")

# Abonnements Recharge
def get_active_subscriptions():
    try:
        subs = []
        page = 1
        while True:
            data = recharge_get(f"subscriptions?status=active&limit=250&page={page}")
            batch = data.get("subscriptions", [])
            subs.extend(batch)
            if len(batch) < 250:
                break
            page += 1
        return subs
    except Exception as e:
        print(f"Recharge error: {e}")
        return []

def get_subscription_stats():
    subs = get_active_subscriptions()
    total_abonnes = len(subs)
    total_chats = 0
    sacs_par_mois = 0
    for s in subs:
        titre = s.get("product_title", "") + " " + s.get("variant_title", "")
        nb_chats = extract_sacs_from_bundle(titre)
        total_chats += nb_chats
        freq_jours = s.get("order_interval_frequency", 30)
        freq_unit = s.get("order_interval_unit", "day")
        if freq_unit == "month":
            freq_jours_calc = int(freq_jours) * 30
        elif freq_unit == "week":
            freq_jours_calc = int(freq_jours) * 7
        else:
            freq_jours_calc = int(freq_jours)
        sacs_par_mois += nb_chats / (freq_jours_calc / 30)
    return total_abonnes, total_chats, round(sacs_par_mois, 1)

# Email alerte
def send_email(subject, body):
    if not GMAIL_USER or not GMAIL_PASSWORD:
        print(f"EMAIL (no config): {subject}")
        return
    import smtplib
    from email.mime.text import MIMEText
    try:
        msg = MIMEText(body, "html")
        msg["Subject"] = subject
        msg["From"] = GMAIL_USER
        msg["To"] = ALERT_EMAIL
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASSWORD)
            s.sendmail(GMAIL_USER, ALERT_EMAIL, msg.as_string())
    except Exception as e:
        print(f"Email error: {e}")

# Vérification alerte stock
def check_stock_alert():
    stock, _ = get_current_stock()
    _, _, sacs_mois = get_subscription_stats()
    if sacs_mois <= 0:
        return
    mois_restants = stock / sacs_mois
    if mois_restants < ALERT_THRESHOLD_MONTHS:
        date_limite = datetime.now() + timedelta(days=mois_restants * 30)
        body = f"""
        <h2>⚠️ Alerte stock Jilypet</h2>
        <p>Stock actuel : <b>{stock} sacs</b></p>
        <p>Consommation : <b>{sacs_mois} sacs/mois</b></p>
        <p>Stock restant : <b>{mois_restants:.1f} mois</b></p>
        <p>Date limite commande : <b>{date_limite.strftime('%d/%m/%Y')}</b></p>
        <p>⚡ Commandez dès maintenant pour éviter une rupture de stock !</p>
        """
        send_email("⚠️ ALERTE STOCK - Commande fournisseur urgente", body)

# Weekly report
def weekly_report():
    stock, _ = get_current_stock()
    abonnes, chats, sacs_mois = get_subscription_stats()
    mois_restants = stock / sacs_mois if sacs_mois > 0 else 0
    statut = "🟢 OK" if mois_restants >= ALERT_THRESHOLD_MONTHS else "🔴 URGENT"
    body = f"""
    <h2>📊 Rapport hebdomadaire Jilypet</h2>
    <table border="1" cellpadding="8">
    <tr><td>Stock actuel</td><td><b>{stock} sacs</b></td></tr>
    <tr><td>Abonnés actifs</td><td><b>{abonnes}</b> / objectif 600-700</td></tr>
    <tr><td>Chats actifs</td><td><b>{chats}</b> / objectif 1000-1200</td></tr>
    <tr><td>Consommation</td><td><b>{sacs_mois} sacs/mois</b></td></tr>
    <tr><td>Stock restant</td><td><b>{mois_restants:.1f} mois</b></td></tr>
    <tr><td>Statut</td><td><b>{statut}</b></td></tr>
    </table>
    """
    send_email("📊 Rapport hebdo stock Jilypet", body)
    check_stock_alert()

# Verify webhook Shopify
def verify_webhook(data, hmac_header):
    if not SHOPIFY_WEBHOOK_SECRET:
        return True
    digest = hmac.new(SHOPIFY_WEBHOOK_SECRET.encode("utf-8"), data, hashlib.sha256).digest()
    import base64
    computed = base64.b64encode(digest).decode()
    return hmac.compare_digest(computed, hmac_header or "")

# WEBHOOKS
@app.route("/webhook/order-created", methods=["POST"])
def order_created():
    data = request.get_data()
    if not verify_webhook(data, request.headers.get("X-Shopify-Hmac-Sha256")):
        return jsonify({"error": "unauthorized"}), 401
    order = request.json
    order_id = order.get("id")
    sacs = get_sacs_from_order(order)
    stock, variant_id = get_current_stock()
    if variant_id:
        update_stock(variant_id, stock - sacs)
    log_movement(order_id, sacs, "DEBIT", f"Commande #{order.get('order_number')}")
    check_stock_alert()
    return jsonify({"ok": True, "sacs_debites": sacs})

@app.route("/webhook/order-cancelled", methods=["POST"])
def order_cancelled():
    data = request.get_data()
    if not verify_webhook(data, request.headers.get("X-Shopify-Hmac-Sha256")):
        return jsonify({"error": "unauthorized"}), 401
    order = request.json
    order_id = order.get("id")
    fulfillment = order.get("fulfillment_status")
    if fulfillment in [None, "unfulfilled", ""]:
        sacs = get_sacs_from_order(order)
        stock, variant_id = get_current_stock()
        if variant_id:
            update_stock(variant_id, stock + sacs)
        log_movement(order_id, sacs, "CREDIT", f"Annulation #{order.get('order_number')}")
    return jsonify({"ok": True})

# DASHBOARD
@app.route("/")
def dashboard():
    stock, _ = get_current_stock()
    abonnes, chats, sacs_mois = get_subscription_stats()
    mois_restants = stock / sacs_mois if sacs_mois > 0 else 0
    date_limite = datetime.now() + timedelta(days=mois_restants * 30)
    couleur = "#22c55e" if mois_restants >= 4 else ("#f59e0b" if mois_restants >= 2 else "#ef4444")
    pct_abonnes = min(100, int((abonnes / 650) * 100))
    pct_chats = min(100, int((chats / 1100) * 100))
    pct_stock = min(100, int((mois_restants / 6) * 100))

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Jilypet — Stock Manager</title>
        <style>
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{ font-family: -apple-system, sans-serif; background: #0f172a; color: #e2e8f0; padding: 20px; }}
            h1 {{ font-size: 22px; margin-bottom: 4px; color: #f8fafc; }}
            .sub {{ color: #94a3b8; font-size: 13px; margin-bottom: 24px; }}
            .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 24px; }}
            .card {{ background: #1e293b; border-radius: 12px; padding: 20px; border: 1px solid #334155; }}
            .card-title {{ font-size: 12px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }}
            .card-value {{ font-size: 32px; font-weight: 600; margin-bottom: 4px; }}
            .card-sub {{ font-size: 13px; color: #64748b; }}
            .bar-bg {{ background: #334155; border-radius: 99px; height: 8px; margin-top: 12px; }}
            .bar {{ height: 8px; border-radius: 99px; transition: width 1s; }}
            .alert {{ background: #7f1d1d; border: 1px solid #ef4444; border-radius: 12px; padding: 16px; margin-bottom: 24px; }}
            .alert-title {{ color: #fca5a5; font-weight: 600; margin-bottom: 4px; }}
            .ok {{ background: #14532d; border: 1px solid #22c55e; border-radius: 12px; padding: 16px; margin-bottom: 24px; }}
            .ok-title {{ color: #86efac; font-weight: 600; }}
            .updated {{ color: #475569; font-size: 12px; margin-top: 24px; text-align: center; }}
        </style>
    </head>
    <body>
        <h1>🐱 Jilypet — Stock Manager</h1>
        <p class="sub">Mis à jour le {datetime.now().strftime('%d/%m/%Y à %H:%M')}</p>

        {"<div class='alert'><div class='alert-title'>⚠️ Stock critique !</div>Il reste " + f"{mois_restants:.1f} mois de stock. Commandez avant le {date_limite.strftime('%d/%m/%Y')}.</div>" if mois_restants < ALERT_THRESHOLD_MONTHS else "<div class='ok'><div class='ok-title'>✅ Stock OK</div></div>"}

        <div class="grid">
            <div class="card">
                <div class="card-title">Stock restant</div>
                <div class="card-value" style="color:{couleur}">{mois_restants:.1f} mois</div>
                <div class="card-sub">{stock} sacs · limite: {date_limite.strftime('%d/%m/%Y')}</div>
                <div class="bar-bg"><div class="bar" style="width:{pct_stock}%;background:{couleur}"></div></div>
            </div>
            <div class="card">
                <div class="card-title">Consommation</div>
                <div class="card-value" style="color:#60a5fa">{sacs_mois}</div>
                <div class="card-sub">sacs / mois projetés</div>
            </div>
            <div class="card">
                <div class="card-title">Abonnés actifs</div>
                <div class="card-value" style="color:#a78bfa">{abonnes}</div>
                <div class="card-sub">objectif 600–700</div>
                <div class="bar-bg"><div class="bar" style="width:{pct_abonnes}%;background:#a78bfa"></div></div>
            </div>
            <div class="card">
                <div class="card-title">Chats actifs</div>
                <div class="card-value" style="color:#34d399">{chats}</div>
                <div class="card-sub">objectif 1 000–1 200</div>
                <div class="bar-bg"><div class="bar" style="width:{pct_chats}%;background:#34d399"></div></div>
            </div>
        </div>
        <p class="updated">Rafraîchir la page pour mettre à jour · Alertes email automatiques</p>
    </body>
    </html>
    """
    return html

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})

# Scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(weekly_report, "cron", day_of_week="mon", hour=8)
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
