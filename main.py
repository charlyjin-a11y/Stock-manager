import os
import json
import sqlite3
import hmac
import hashlib
import base64
import re
import requests
from datetime import datetime, timedelta, date
from flask import Flask, request, jsonify, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# ── Config ──────────────────────────────────────────────
SHOPIFY_TOKEN = os.environ.get("SHOPIFY_TOKEN")
SHOPIFY_SHOP = os.environ.get("SHOPIFY_SHOP", "zhnbdz-03.myshopify.com")
RECHARGE_TOKEN = os.environ.get("RECHARGE_TOKEN")
SHOPIFY_SKU = os.environ.get("SHOPIFY_SKU", "AULJP")
SHOPIFY_WEBHOOK_SECRET = os.environ.get("SHOPIFY_WEBHOOK_SECRET", "")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_SERVICE_ACCOUNT = os.environ.get("GOOGLE_SERVICE_ACCOUNT", "")
GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD")
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "hello@jilypet.com")
SEUIL_ALERTE_MOIS = 3

DB_PATH = os.environ.get("DB_PATH", "/data/jilypet.db") if os.path.isdir("/data") else "jilypet.db"

# ── Database locale (historique seulement) ───────────────
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS mouvements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, type TEXT, quantite INTEGER, raison TEXT
    );
    CREATE TABLE IF NOT EXISTS historique_stock (
        date TEXT PRIMARY KEY,
        stock_litiere INTEGER, abonnes INTEGER, chats INTEGER
    );
    """)
    conn.commit()
    conn.close()

init_db()

# ── Google Sheets ────────────────────────────────────────
_sheets_cache = {"data": None, "ts": 0}
CACHE_SECONDS = 120

def get_sheets_data():
    """Lit les onglets Conteneurs, Objectifs, Stocks depuis Google Sheets avec cache"""
    import time
    now = time.time()
    if _sheets_cache["data"] and now - _sheets_cache["ts"] < CACHE_SECONDS:
        return _sheets_cache["data"]
    if not GOOGLE_SHEET_ID or not GOOGLE_SERVICE_ACCOUNT:
        return {"conteneurs": [], "objectifs": [], "stocks": {}}
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_SERVICE_ACCOUNT),
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GOOGLE_SHEET_ID)

        # Conteneurs
        conteneurs = []
        try:
            rows = sh.worksheet("Conteneurs").get_all_values()[1:]
            for r in rows:
                r = r + [""] * (13 - len(r))
                ref, ctype, qte, dcmd, dprep, ddep, darr, pyuan, peuro, p30, p70, masque, notes = r[:13]
                if not ref or "EXEMPLE" in ref.upper():
                    continue
                conteneurs.append({
                    "reference": ref.strip(),
                    "type": "sacs_plastique" if "sac" in ctype.lower() else "litiere",
                    "nb_unites": parse_int(qte),
                    "date_commande": parse_date(dcmd),
                    "date_debut_preparation": parse_date(dprep),
                    "date_depart_chine": parse_date(ddep),
                    "date_arrivee_france": parse_date(darr),
                    "prix_yuan": parse_float(pyuan),
                    "prix_euro": parse_float(peuro),
                    "paye_30": bool(p30.strip()),
                    "paye_70": bool(p70.strip()),
                    "masque": bool(masque.strip()),
                    "notes": notes.strip(),
                })
        except Exception as e:
            print(f"Sheets Conteneurs error: {e}")

        # Objectifs
        objectifs = []
        try:
            rows = sh.worksheet("Objectifs").get_all_values()[1:]
            for r in rows:
                r = r + [""] * (6 - len(r))
                mois, objc, reelc, objch, reelch, masque = r[:6]
                if not mois.strip():
                    continue
                objectifs.append({
                    "mois": mois.strip(),
                    "obj_nouveaux_clients": parse_int(objc),
                    "reel_nouveaux_clients": parse_int(reelc) if reelc.strip() else None,
                    "obj_nouveaux_chats": parse_int(objch),
                    "reel_nouveaux_chats": parse_int(reelch) if reelch.strip() else None,
                    "masque": bool(masque.strip()),
                })
        except Exception as e:
            print(f"Sheets Objectifs error: {e}")

        # Stocks
        stocks = {}
        try:
            rows = sh.worksheet("Stocks").get_all_values()[1:]
            for r in rows:
                if len(r) >= 2 and r[0].strip():
                    stocks[r[0].strip()] = parse_int(r[1])
        except Exception as e:
            print(f"Sheets Stocks error: {e}")

        data = {"conteneurs": conteneurs, "objectifs": objectifs, "stocks": stocks}
        _sheets_cache["data"] = data
        _sheets_cache["ts"] = now
        return data
    except Exception as e:
        print(f"Sheets error: {e}")
        return _sheets_cache["data"] or {"conteneurs": [], "objectifs": [], "stocks": {}}

def parse_int(s):
    try:
        return int(str(s).replace(" ", "").replace(",", "").replace("\u202f", "") or 0)
    except:
        return 0

def parse_float(s):
    try:
        return float(str(s).replace(" ", "").replace(",", ".").replace("€", "").replace("\u202f", "") or 0)
    except:
        return 0

def parse_date(s):
    s = str(s).strip()
    if not s:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except:
            pass
    return ""

# ── APIs externes ────────────────────────────────────────
def shopify_get(endpoint):
    url = f"https://{SHOPIFY_SHOP}/admin/api/2026-04/{endpoint}"
    r = requests.get(url, headers={"X-Shopify-Access-Token": SHOPIFY_TOKEN}, timeout=20)
    return r.json()

def get_stock_litiere():
    try:
        data = shopify_get("products.json?fields=variants&limit=250")
        for p in data.get("products", []):
            for v in p.get("variants", []):
                if v.get("sku") == SHOPIFY_SKU:
                    return v.get("inventory_quantity", 0)
        return 0
    except Exception as e:
        print(f"Shopify error: {e}")
        return None

def recharge_get(endpoint):
    r = requests.get(f"https://api.rechargeapps.com/{endpoint}",
                     headers={"X-Recharge-Access-Token": RECHARGE_TOKEN}, timeout=30)
    return r.json()

def extract_chats(titre):
    m = re.search(r'(\d+)\s*[Cc]hat', titre or "")
    return int(m.group(1)) if m else 1

def get_recharge_stats():
    try:
        subs, page = [], 1
        while True:
            data = recharge_get(f"subscriptions?status=active&limit=250&page={page}")
            batch = data.get("subscriptions", [])
            subs.extend(batch)
            if len(batch) < 250:
                break
            page += 1
        subs_reels = [s for s in subs if float(s.get("price", 0) or 0) > 0]
        abonnes = len(subs_reels)
        chats = sum(extract_chats((s.get("product_title") or "") + " " + (s.get("variant_title") or "")) for s in subs_reels)
        sacs_mois = 0
        for s in subs_reels:
            nb = extract_chats((s.get("product_title") or "") + " " + (s.get("variant_title") or ""))
            freq = int(s.get("order_interval_frequency") or 30)
            unit = s.get("order_interval_unit", "day")
            jours = freq * 30 if unit == "month" else freq * 7 if unit == "week" else freq
            sacs_mois += nb / (jours / 30)
        return abonnes, chats, round(sacs_mois, 1)
    except Exception as e:
        print(f"Recharge error: {e}")
        return None, None, None

# ── Email ────────────────────────────────────────────────
def send_email(subject, html):
    if not GMAIL_USER or not GMAIL_PASSWORD:
        print(f"[EMAIL non configuré] {subject}")
        return
    import smtplib
    from email.mime.text import MIMEText
    try:
        msg = MIMEText(html, "html")
        msg["Subject"], msg["From"], msg["To"] = subject, GMAIL_USER, ALERT_EMAIL
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(GMAIL_USER, GMAIL_PASSWORD)
            s.sendmail(GMAIL_USER, ALERT_EMAIL, msg.as_string())
    except Exception as e:
        print(f"Email error: {e}")

# ── Jobs ─────────────────────────────────────────────────
def snapshot_quotidien():
    stock = get_stock_litiere()
    abonnes, chats, _ = get_recharge_stats()
    if stock is None:
        return
    conn = db()
    conn.execute("INSERT OR REPLACE INTO historique_stock (date, stock_litiere, abonnes, chats) VALUES (?,?,?,?)",
                 (date.today().isoformat(), stock, abonnes or 0, chats or 0))
    conn.commit()
    conn.close()

def check_alertes():
    stock = get_stock_litiere()
    _, _, sacs_mois = get_recharge_stats()
    if not stock or not sacs_mois:
        return
    mois_rest = stock / sacs_mois
    if mois_rest < SEUIL_ALERTE_MOIS:
        limite = (datetime.now() + timedelta(days=mois_rest * 30)).strftime("%d/%m/%Y")
        send_email("⚠️ ALERTE STOCK JILYPET",
            f"<h2>Stock critique</h2><p>Stock: <b>{stock} sacs</b> ({mois_rest:.1f} mois)</p>"
            f"<p>Commander avant le <b>{limite}</b></p>")
    # Paiements sous 15 jours (depuis Sheets)
    sheets = get_sheets_data()
    paiements = build_paiements(sheets["conteneurs"])
    limite15 = (date.today() + timedelta(days=15)).isoformat()
    prochains = [p for p in paiements if not p["paye"] and p["date_due"] and p["date_due"] <= limite15]
    if prochains:
        lignes = "".join(f"<li>{p['label']} — {p['montant_euro']:.0f}€ — due {p['date_due']} ({p['reference']})</li>" for p in prochains)
        send_email("💰 Paiements fournisseur à venir", f"<h2>Paiements sous 15 jours</h2><ul>{lignes}</ul>")

scheduler = BackgroundScheduler()
scheduler.add_job(snapshot_quotidien, "cron", hour=6)
scheduler.add_job(check_alertes, "cron", day_of_week="mon", hour=8)
scheduler.start()

# ── Paiements générés depuis conteneurs ──────────────────
def build_paiements(conteneurs):
    paiements = []
    for i, c in enumerate(conteneurs):
        if c.get("masque"):
            continue
        prix = c.get("prix_euro") or 0
        arrivee = c.get("date_arrivee_france") or ""
        if prix <= 0:
            continue
        if c["type"] == "litiere":
            due70 = ""
            if arrivee:
                try:
                    due70 = (datetime.fromisoformat(arrivee) + timedelta(days=90)).date().isoformat()
                except:
                    pass
            paiements.append({"id": f"{i}-30", "reference": c["reference"], "label": "Litière 30%",
                              "montant_euro": round(prix * 0.3, 2), "date_due": arrivee, "paye": c["paye_30"]})
            paiements.append({"id": f"{i}-70", "reference": c["reference"], "label": "Litière 70%",
                              "montant_euro": round(prix * 0.7, 2), "date_due": due70, "paye": c["paye_70"]})
        else:
            paiements.append({"id": f"{i}-100", "reference": c["reference"], "label": "Sacs plastique 100%",
                              "montant_euro": prix, "date_due": arrivee, "paye": c["paye_30"]})
    return paiements

# ── Webhooks Shopify ─────────────────────────────────────
def verify_webhook(data, hmac_header):
    if not SHOPIFY_WEBHOOK_SECRET:
        return True
    digest = hmac.new(SHOPIFY_WEBHOOK_SECRET.encode(), data, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), hmac_header or "")

def log_mouvement(qte, type_, raison):
    conn = db()
    conn.execute("INSERT INTO mouvements (date, type, quantite, raison) VALUES (?,?,?,?)",
                 (datetime.now().isoformat(), type_, qte, raison))
    conn.commit()
    conn.close()

def sacs_from_order(order):
    total = sum(i.get("quantity", 1) for i in order.get("line_items", []) if i.get("sku") == SHOPIFY_SKU)
    return total or 1

@app.route("/webhook/order-created", methods=["POST"])
def wh_created():
    if not verify_webhook(request.get_data(), request.headers.get("X-Shopify-Hmac-Sha256")):
        return jsonify({"error": "unauthorized"}), 401
    order = request.json
    log_mouvement(sacs_from_order(order), "debit", f"Commande #{order.get('order_number')}")
    return jsonify({"ok": True})

@app.route("/webhook/order-cancelled", methods=["POST"])
def wh_cancelled():
    if not verify_webhook(request.get_data(), request.headers.get("X-Shopify-Hmac-Sha256")):
        return jsonify({"error": "unauthorized"}), 401
    order = request.json
    if order.get("fulfillment_status") in [None, "unfulfilled", ""]:
        log_mouvement(sacs_from_order(order), "credit", f"Annulation #{order.get('order_number')}")
    return jsonify({"ok": True})

@app.route("/webhook/order-refunded", methods=["POST"])
def wh_refunded():
    if not verify_webhook(request.get_data(), request.headers.get("X-Shopify-Hmac-Sha256")):
        return jsonify({"error": "unauthorized"}), 401
    refund = request.json
    qte = sum(i.get("quantity", 0) for i in refund.get("refund_line_items", [])
              if i.get("line_item", {}).get("sku") == SHOPIFY_SKU)
    if qte:
        log_mouvement(qte, "credit", f"Remboursement #{refund.get('order_id')}")
    return jsonify({"ok": True})

# ── API données front ────────────────────────────────────
@app.route("/api/data")
def api_data():
    stock = get_stock_litiere()
    abonnes, chats, sacs_mois = get_recharge_stats()
    sheets = get_sheets_data()

    conteneurs_visibles = [c for c in sheets["conteneurs"] if not c.get("masque")]
    objectifs_visibles = [o for o in sheets["objectifs"] if not o.get("masque")]
    paiements = build_paiements(sheets["conteneurs"])
    stocks = sheets["stocks"]

    conn = db()
    historique = [dict(r) for r in conn.execute("SELECT * FROM historique_stock ORDER BY date DESC LIMIT 90").fetchall()]
    conn.close()

    mois_rest = round(stock / sacs_mois, 1) if stock and sacs_mois else 0
    date_limite = (datetime.now() + timedelta(days=mois_rest * 30)).strftime("%d/%m/%Y") if mois_rest else "—"
    date_commande_limite = (datetime.now() + timedelta(days=max(0, (mois_rest - 3) * 30))).strftime("%d/%m/%Y") if mois_rest else "—"

    def find_stock(key_part):
        for k, v in stocks.items():
            if key_part.lower() in k.lower():
                return v
        return 0

    return jsonify({
        "stock_litiere": stock,
        "sacs_mois": sacs_mois,
        "mois_restants": mois_rest,
        "date_rupture": date_limite,
        "date_commande_limite": date_commande_limite,
        "abonnes": abonnes,
        "chats": chats,
        "stock_sacs_plastique": find_stock("plastique"),
        "cartons": [
            {"taille": "A14", "sacs_par_carton": "1 sac", "quantite": find_stock("A14")},
            {"taille": "A13", "sacs_par_carton": "2 sacs", "quantite": find_stock("A13")},
            {"taille": "A12", "sacs_par_carton": "3-4 sacs", "quantite": find_stock("A12")},
        ],
        "conteneurs": conteneurs_visibles,
        "paiements": paiements,
        "objectifs": objectifs_visibles,
        "historique": historique,
        "sheets_ok": bool(GOOGLE_SHEET_ID and GOOGLE_SERVICE_ACCOUNT),
        "maj": datetime.now().strftime("%d/%m/%Y %H:%M"),
    })

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

# ── Frontend ─────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(PAGE_HTML)

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jilypet — Gestion Stock</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--bg:#0d1420;--card:#16202e;--border:#243247;--txt:#e8eef5;--mut:#8ba0b8;--acc:#4da3ff;--ok:#2ecc71;--warn:#f1c40f;--bad:#e74c3c;}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Segoe UI',sans-serif;background:var(--bg);color:var(--txt);padding-bottom:60px}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
header{display:flex;justify-content:space-between;align-items:center;padding:16px 0;flex-wrap:wrap;gap:8px}
h1{font-size:20px}
.maj{color:var(--mut);font-size:12px}
.btn{background:var(--acc);color:#fff;border:none;border-radius:8px;padding:8px 16px;font-size:14px;cursor:pointer;text-decoration:none;display:inline-block}
.btn.sec{background:var(--border)}
.grid{display:grid;gap:12px;margin-bottom:16px}
.g4{grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:16px}
.card h3{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin-bottom:8px}
.big{font-size:30px;font-weight:700}
.sub{font-size:12px;color:var(--mut);margin-top:2px}
.bar{background:var(--border);height:8px;border-radius:99px;margin-top:10px;overflow:hidden}
.bar>div{height:100%;border-radius:99px;transition:width .8s}
.alert{border-radius:12px;padding:14px;margin-bottom:14px;font-size:14px}
.alert.bad{background:#3d1513;border:1px solid var(--bad);color:#ffb3ad}
.alert.ok{background:#11301f;border:1px solid var(--ok);color:#a8e6c1}
.alert.info{background:#12283d;border:1px solid var(--acc);color:#a8d4ff}
.section-title{font-size:16px;font-weight:600;margin:22px 0 10px;display:flex;align-items:center;gap:10px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--mut);text-align:left;padding:8px;border-bottom:1px solid var(--border);font-size:11px;text-transform:uppercase}
td{padding:9px 8px;border-bottom:1px solid var(--border)}
.tag{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600}
.tag.ok{background:#11301f;color:var(--ok)}.tag.warn{background:#332a10;color:var(--warn)}.tag.info{background:#12283d;color:var(--acc)}
.timeline{display:flex;gap:4px;margin-top:6px}
.tstep{flex:1;text-align:center;font-size:10px;color:var(--mut)}
.tdot{width:100%;height:5px;border-radius:3px;background:var(--border);margin-bottom:4px}
.tdot.done{background:var(--ok)}
canvas{max-height:260px}
.cal{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px}
.calmois{background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:10px}
.calmois h4{font-size:12px;color:var(--acc);margin-bottom:6px}
.calitem{font-size:11px;padding:4px 6px;border-radius:6px;margin-bottom:4px}
.calitem.due{background:#332a10;color:var(--warn)}
.calitem.late{background:#3d1513;color:var(--bad)}
.calitem.paid{background:#11301f;color:var(--ok);text-decoration:line-through;opacity:.6}
.pill{font-size:10px;background:var(--border);border-radius:20px;padding:2px 8px;color:var(--mut)}
@media(max-width:600px){.big{font-size:24px}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div><h1>🐱 Jilypet — Gestion Stock</h1><div class="maj" id="maj">Chargement…</div></div>
  <div style="display:flex;gap:8px">
    <button class="btn sec" onclick="loadData()">↻ Actualiser</button>
    <a class="btn" id="sheetLink" target="_blank" style="display:none">📝 Modifier (Sheets)</a>
  </div>
</header>

<div id="alertZone"></div>

<div class="grid g4">
  <div class="card"><h3>🏭 Stock litière</h3><div class="big" id="stockLit">—</div><div class="sub" id="stockLitSub"></div><div class="bar"><div id="stockLitBar"></div></div></div>
  <div class="card"><h3>🛍️ Sacs plastique</h3><div class="big" id="stockSacs">—</div><div class="sub">en Chine (prêts à remplir)</div></div>
  <div class="card"><h3>📦 Cartons GLS</h3><div id="cartonsList" style="font-size:14px;margin-top:4px">—</div></div>
  <div class="card"><h3>👥 Abonnés / Chats</h3><div class="big" id="abonnes">—</div><div class="sub" id="chatsSub"></div></div>
</div>

<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr))">
  <div class="card"><h3>📉 Évolution du stock (90 jours)</h3><canvas id="chartStock"></canvas></div>
  <div class="card"><h3>📊 Objectifs vs Réel — nouveaux clients</h3><canvas id="chartObj"></canvas></div>
</div>

<div class="section-title">🚢 Conteneurs</div>
<div class="card" style="overflow-x:auto"><table id="tblCont"><thead><tr>
<th>Réf</th><th>Type</th><th>Quantité</th><th>Progression</th><th>Statut</th>
</tr></thead><tbody></tbody></table></div>

<div class="section-title">💰 Calendrier paiements</div>
<div class="card"><div class="cal" id="calPaiements"></div></div>

<div class="section-title">🎯 Objectifs mensuels</div>
<div class="card" style="overflow-x:auto"><table id="tblObj"><thead><tr>
<th>Mois</th><th>Obj. clients</th><th>Réel clients</th><th>Obj. chats</th><th>Réel chats</th>
</tr></thead><tbody></tbody></table></div>

</div>

<script>
let DATA=null, chStock=null, chObj=null;
const SHEET_ID = "";  // rempli côté serveur si besoin

async function loadData(){
  const r = await fetch('/api/data'); DATA = await r.json();
  document.getElementById('maj').textContent = 'Mis à jour: '+DATA.maj;
  render();
}

function render(){
  const d=DATA;
  const az=document.getElementById('alertZone');
  let alerts='';
  if(!d.sheets_ok){alerts+=`<div class="alert info">ℹ️ Google Sheets non connecté — configure GOOGLE_SHEET_ID et GOOGLE_SERVICE_ACCOUNT dans Railway.</div>`;}
  if(d.mois_restants<3){alerts+=`<div class="alert bad">⚠️ <b>Stock critique !</b> ${d.mois_restants} mois restants (${d.stock_litiere?.toLocaleString('fr')} sacs). Rupture estimée le ${d.date_rupture}. <b>Commander immédiatement.</b></div>`;}
  else{alerts+=`<div class="alert ok">✅ Stock OK — ${d.mois_restants} mois d'autonomie. Prochaine commande avant le ${d.date_commande_limite}.</div>`;}
  az.innerHTML=alerts;

  document.getElementById('stockLit').textContent = (d.stock_litiere??0).toLocaleString('fr')+' sacs';
  document.getElementById('stockLitSub').textContent = `${d.mois_restants} mois · ${d.sacs_mois} sacs/mois · rupture ${d.date_rupture}`;
  const pct=Math.min(100,(d.mois_restants/6)*100);
  const bar=document.getElementById('stockLitBar');
  bar.style.width=pct+'%';
  bar.style.background=d.mois_restants<2?'var(--bad)':d.mois_restants<3?'var(--warn)':'var(--ok)';
  document.getElementById('stockSacs').textContent=(d.stock_sacs_plastique??0).toLocaleString('fr');
  document.getElementById('cartonsList').innerHTML=d.cartons.map(c=>`<div style="display:flex;justify-content:space-between;padding:3px 0"><span>${c.taille} <span class="pill">${c.sacs_par_carton}</span></span><b>${(c.quantite??0).toLocaleString('fr')}</b></div>`).join('');
  document.getElementById('abonnes').textContent=d.abonnes?.toLocaleString('fr')??'—';
  document.getElementById('chatsSub').textContent=`${d.chats?.toLocaleString('fr')??'—'} chats actifs`;

  const now=new Date().toISOString().slice(0,10);
  const tb=document.querySelector('#tblCont tbody');
  tb.innerHTML=d.conteneurs.map(c=>{
    const steps=[['Commande',c.date_commande],['Préparation',c.date_debut_preparation],['Départ',c.date_depart_chine],['Arrivée',c.date_arrivee_france]];
    const tl='<div class="timeline">'+steps.map(([n,dt])=>`<div class="tstep"><div class="tdot ${dt&&dt<=now?'done':''}"></div>${n}${dt?'<br>'+fmtD(dt):''}</div>`).join('')+'</div>';
    const arrived=c.date_arrivee_france&&c.date_arrivee_france<=now;
    const badge=arrived?'<span class="tag ok">✅ Arrivé</span>':(c.date_depart_chine&&c.date_depart_chine<=now?'<span class="tag info">🚢 En mer</span>':'<span class="tag warn">🏭 Préparation</span>');
    return `<tr><td><b>${c.reference||'—'}</b></td><td>${c.type==='litiere'?'Litière':'Sacs pl.'}</td><td>${(c.nb_unites??0).toLocaleString('fr')}</td><td style="min-width:220px">${tl}</td><td>${badge}</td></tr>`;
  }).join('')||'<tr><td colspan="5" style="color:var(--mut)">Aucun conteneur visible — ajoute-les dans Google Sheets</td></tr>';

  const cal={};
  d.paiements.forEach(p=>{
    if(!p.date_due)return;
    const k=p.date_due.slice(0,7);
    (cal[k]=cal[k]||[]).push(p);
  });
  document.getElementById('calPaiements').innerHTML=Object.keys(cal).sort().map(m=>{
    const [y,mo]=m.split('-');
    const nom=['Jan','Fév','Mar','Avr','Mai','Juin','Juil','Août','Sep','Oct','Nov','Déc'][+mo-1]+' '+y;
    return `<div class="calmois"><h4>${nom}</h4>`+cal[m].map(p=>{
      const cls=p.paye?'paid':(p.date_due<now?'late':'due');
      return `<div class="calitem ${cls}">${p.label} — ${(+p.montant_euro).toLocaleString('fr')}€ <span style="opacity:.7">(${p.reference})</span></div>`;
    }).join('')+'</div>';
  }).join('')||'<div style="color:var(--mut)">Aucun paiement — saisis les conteneurs avec prix dans Google Sheets</div>';

  const to=document.querySelector('#tblObj tbody');
  to.innerHTML=d.objectifs.map(o=>`<tr><td>${o.mois}</td><td>${o.obj_nouveaux_clients||0}</td><td>${o.reel_nouveaux_clients??'—'}</td><td>${o.obj_nouveaux_chats||0}</td><td>${o.reel_nouveaux_chats??'—'}</td></tr>`).join('')||'<tr><td colspan="5" style="color:var(--mut)">Aucun objectif — saisis-les dans Google Sheets</td></tr>';

  drawCharts();
}

function fmtD(s){if(!s)return'';const p=s.split('-');return p[2]+'/'+p[1];}

function drawCharts(){
  const h=[...DATA.historique].reverse();
  if(chStock)chStock.destroy();
  chStock=new Chart(document.getElementById('chartStock'),{type:'line',data:{
    labels:h.map(x=>fmtD(x.date)),
    datasets:[{label:'Stock',data:h.map(x=>x.stock_litiere),borderColor:'#4da3ff',backgroundColor:'rgba(77,163,255,.1)',fill:true,tension:.3}]
  },options:{plugins:{legend:{display:false}},scales:{x:{ticks:{color:'#8ba0b8',maxTicksLimit:8}},y:{ticks:{color:'#8ba0b8'}}}}});

  const o=DATA.objectifs.slice(-12);
  if(chObj)chObj.destroy();
  chObj=new Chart(document.getElementById('chartObj'),{type:'bar',data:{
    labels:o.map(x=>x.mois),
    datasets:[
      {label:'Objectif',data:o.map(x=>x.obj_nouveaux_clients),backgroundColor:'rgba(167,139,250,.5)'},
      {label:'Réel',data:o.map(x=>x.reel_nouveaux_clients),backgroundColor:'rgba(46,204,113,.7)'}
    ]},options:{plugins:{legend:{labels:{color:'#8ba0b8'}}},scales:{x:{ticks:{color:'#8ba0b8'}},y:{ticks:{color:'#8ba0b8'}}}}});
}

loadData();
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
