import os
import json
import sqlite3
import hmac
import hashlib
import base64
import requests
from datetime import datetime, timedelta, date
from flask import Flask, request, jsonify, session, redirect, render_template_string
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "jilypet-secret-2026")

# ── Config ──────────────────────────────────────────────
SHOPIFY_TOKEN = os.environ.get("SHOPIFY_TOKEN")
SHOPIFY_SHOP = os.environ.get("SHOPIFY_SHOP", "zhnbdz-03.myshopify.com")
RECHARGE_TOKEN = os.environ.get("RECHARGE_TOKEN")
SHOPIFY_SKU = os.environ.get("SHOPIFY_SKU", "AULJP")
SHOPIFY_WEBHOOK_SECRET = os.environ.get("SHOPIFY_WEBHOOK_SECRET", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "jilypet2026")
GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD")
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "hello@jilypet.com")
SEUIL_ALERTE_MOIS = 3

DB_PATH = os.environ.get("DB_PATH", "/data/jilypet.db") if os.path.isdir("/data") else "jilypet.db"

# ── Database ────────────────────────────────────────────
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS conteneurs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL DEFAULT 'litiere',      -- litiere | sacs_plastique
        reference TEXT,
        nb_unites INTEGER NOT NULL DEFAULT 0,      -- sacs de litière ou sacs plastique
        date_commande TEXT,
        date_debut_preparation TEXT,
        date_depart_chine TEXT,
        date_arrivee_france TEXT,
        recu INTEGER DEFAULT 0,                    -- 1 si arrivé et stocké
        prix_yuan REAL DEFAULT 0,
        prix_euro REAL DEFAULT 0,
        notes TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS paiements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conteneur_id INTEGER,
        type TEXT NOT NULL,                        -- litiere_30 | litiere_70 | sacs_100
        montant_euro REAL DEFAULT 0,
        date_due TEXT,
        paye INTEGER DEFAULT 0,
        date_paye TEXT,
        FOREIGN KEY (conteneur_id) REFERENCES conteneurs(id)
    );
    CREATE TABLE IF NOT EXISTS objectifs (
        mois TEXT PRIMARY KEY,                     -- format YYYY-MM
        obj_nouveaux_clients INTEGER DEFAULT 0,
        obj_nouveaux_chats INTEGER DEFAULT 0,
        reel_nouveaux_clients INTEGER,
        reel_nouveaux_chats INTEGER
    );
    CREATE TABLE IF NOT EXISTS stock_cartons (
        taille TEXT PRIMARY KEY,                   -- A14 | A13 | A12
        quantite INTEGER DEFAULT 0,
        sacs_par_carton TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS stock_sacs_plastique (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        quantite INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS mouvements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        type TEXT,                                 -- debit | credit
        quantite INTEGER,
        raison TEXT
    );
    CREATE TABLE IF NOT EXISTS historique_stock (
        date TEXT PRIMARY KEY,
        stock_litiere INTEGER,
        abonnes INTEGER,
        chats INTEGER
    );
    """)
    # Init cartons
    for taille, desc in [("A14","1 sac"),("A13","2 sacs"),("A12","3-4 sacs")]:
        conn.execute("INSERT OR IGNORE INTO stock_cartons (taille, quantite, sacs_par_carton) VALUES (?,0,?)", (taille, desc))
    conn.execute("INSERT OR IGNORE INTO stock_sacs_plastique (id, quantite) VALUES (1, 0)")
    conn.commit()
    conn.close()

init_db()

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

import re
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
        # consommation mensuelle projetée
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

# ── Jobs planifiés ───────────────────────────────────────
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
    # Alertes paiements à 15 jours
    conn = db()
    prochains = conn.execute("""SELECT p.*, c.reference FROM paiements p
        LEFT JOIN conteneurs c ON c.id = p.conteneur_id
        WHERE p.paye = 0 AND p.date_due IS NOT NULL AND p.date_due <> ''
        AND date(p.date_due) <= date('now', '+15 days')""").fetchall()
    conn.close()
    if prochains:
        lignes = "".join(f"<li>{p['type']} — {p['montant_euro']:.0f}€ — due {p['date_due']} ({p['reference'] or ''})</li>" for p in prochains)
        send_email("💰 Paiements fournisseur à venir", f"<h2>Paiements sous 15 jours</h2><ul>{lignes}</ul>")

scheduler = BackgroundScheduler()
scheduler.add_job(snapshot_quotidien, "cron", hour=6)
scheduler.add_job(check_alertes, "cron", day_of_week="mon", hour=8)
scheduler.start()

# ── Webhooks Shopify (conservés) ─────────────────────────
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

# ── API données pour le front ────────────────────────────
@app.route("/api/data")
def api_data():
    stock = get_stock_litiere()
    abonnes, chats, sacs_mois = get_recharge_stats()
    conn = db()

    conteneurs = [dict(r) for r in conn.execute("SELECT * FROM conteneurs ORDER BY date_arrivee_france").fetchall()]
    paiements = [dict(r) for r in conn.execute("""SELECT p.*, c.reference, c.type as ctype FROM paiements p
        LEFT JOIN conteneurs c ON c.id = p.conteneur_id ORDER BY p.date_due""").fetchall()]
    objectifs = [dict(r) for r in conn.execute("SELECT * FROM objectifs ORDER BY mois").fetchall()]
    cartons = [dict(r) for r in conn.execute("SELECT * FROM stock_cartons").fetchall()]
    sacs_pl = conn.execute("SELECT quantite FROM stock_sacs_plastique WHERE id=1").fetchone()
    historique = [dict(r) for r in conn.execute("SELECT * FROM historique_stock ORDER BY date DESC LIMIT 90").fetchall()]
    conn.close()

    mois_rest = round(stock / sacs_mois, 1) if stock and sacs_mois else 0
    date_limite = (datetime.now() + timedelta(days=mois_rest * 30)).strftime("%d/%m/%Y") if mois_rest else "—"
    # date limite commande = date rupture - délai fournisseur 3 mois
    date_commande_limite = (datetime.now() + timedelta(days=max(0, (mois_rest - 3) * 30))).strftime("%d/%m/%Y") if mois_rest else "—"

    return jsonify({
        "stock_litiere": stock,
        "sacs_mois": sacs_mois,
        "mois_restants": mois_rest,
        "date_rupture": date_limite,
        "date_commande_limite": date_commande_limite,
        "abonnes": abonnes,
        "chats": chats,
        "stock_sacs_plastique": sacs_pl["quantite"] if sacs_pl else 0,
        "cartons": cartons,
        "conteneurs": conteneurs,
        "paiements": paiements,
        "objectifs": objectifs,
        "historique": historique,
        "maj": datetime.now().strftime("%d/%m/%Y %H:%M"),
    })

# ── Auth admin ───────────────────────────────────────────
@app.route("/login", methods=["POST"])
def login():
    if request.json.get("password") == ADMIN_PASSWORD:
        session["admin"] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 401

@app.route("/logout", methods=["POST"])
def logout():
    session.pop("admin", None)
    return jsonify({"ok": True})

def admin_required():
    return session.get("admin") == True

# ── API admin ────────────────────────────────────────────
@app.route("/api/conteneur", methods=["POST"])
def add_conteneur():
    if not admin_required():
        return jsonify({"error": "unauthorized"}), 401
    d = request.json
    conn = db()
    cur = conn.execute("""INSERT INTO conteneurs
        (type, reference, nb_unites, date_commande, date_debut_preparation, date_depart_chine, date_arrivee_france, prix_yuan, prix_euro, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (d.get("type","litiere"), d.get("reference",""), d.get("nb_unites",0),
         d.get("date_commande",""), d.get("date_debut_preparation",""),
         d.get("date_depart_chine",""), d.get("date_arrivee_france",""),
         d.get("prix_yuan",0), d.get("prix_euro",0), d.get("notes","")))
    cid = cur.lastrowid
    # Génère les paiements automatiquement
    prix = float(d.get("prix_euro") or 0)
    arrivee = d.get("date_arrivee_france","")
    if prix > 0:
        if d.get("type") == "litiere":
            due70 = ""
            if arrivee:
                try:
                    due70 = (datetime.fromisoformat(arrivee) + timedelta(days=90)).date().isoformat()
                except: pass
            conn.execute("INSERT INTO paiements (conteneur_id, type, montant_euro, date_due) VALUES (?,?,?,?)",
                         (cid, "litiere_30", round(prix*0.3,2), arrivee))
            conn.execute("INSERT INTO paiements (conteneur_id, type, montant_euro, date_due) VALUES (?,?,?,?)",
                         (cid, "litiere_70", round(prix*0.7,2), due70))
        else:
            conn.execute("INSERT INTO paiements (conteneur_id, type, montant_euro, date_due) VALUES (?,?,?,?)",
                         (cid, "sacs_100", prix, arrivee))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "id": cid})

@app.route("/api/conteneur/<int:cid>", methods=["PUT", "DELETE"])
def edit_conteneur(cid):
    if not admin_required():
        return jsonify({"error": "unauthorized"}), 401
    conn = db()
    if request.method == "DELETE":
        conn.execute("DELETE FROM paiements WHERE conteneur_id=?", (cid,))
        conn.execute("DELETE FROM conteneurs WHERE id=?", (cid,))
    else:
        d = request.json
        conn.execute("""UPDATE conteneurs SET type=?, reference=?, nb_unites=?, date_commande=?,
            date_debut_preparation=?, date_depart_chine=?, date_arrivee_france=?, recu=?, prix_yuan=?, prix_euro=?, notes=?
            WHERE id=?""",
            (d.get("type"), d.get("reference"), d.get("nb_unites"), d.get("date_commande"),
             d.get("date_debut_preparation"), d.get("date_depart_chine"), d.get("date_arrivee_france"),
             d.get("recu",0), d.get("prix_yuan",0), d.get("prix_euro",0), d.get("notes",""), cid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/paiement/<int:pid>", methods=["PUT"])
def pay(pid):
    if not admin_required():
        return jsonify({"error": "unauthorized"}), 401
    d = request.json
    conn = db()
    conn.execute("UPDATE paiements SET paye=?, date_paye=? WHERE id=?",
                 (1 if d.get("paye") else 0, datetime.now().date().isoformat() if d.get("paye") else None, pid))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/objectif", methods=["POST"])
def set_objectif():
    if not admin_required():
        return jsonify({"error": "unauthorized"}), 401
    d = request.json
    conn = db()
    conn.execute("""INSERT INTO objectifs (mois, obj_nouveaux_clients, obj_nouveaux_chats, reel_nouveaux_clients, reel_nouveaux_chats)
        VALUES (?,?,?,?,?)
        ON CONFLICT(mois) DO UPDATE SET obj_nouveaux_clients=excluded.obj_nouveaux_clients,
        obj_nouveaux_chats=excluded.obj_nouveaux_chats,
        reel_nouveaux_clients=COALESCE(excluded.reel_nouveaux_clients, objectifs.reel_nouveaux_clients),
        reel_nouveaux_chats=COALESCE(excluded.reel_nouveaux_chats, objectifs.reel_nouveaux_chats)""",
        (d["mois"], d.get("obj_nouveaux_clients",0), d.get("obj_nouveaux_chats",0),
         d.get("reel_nouveaux_clients"), d.get("reel_nouveaux_chats")))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/cartons", methods=["POST"])
def set_cartons():
    if not admin_required():
        return jsonify({"error": "unauthorized"}), 401
    d = request.json
    conn = db()
    for taille, qte in d.items():
        conn.execute("UPDATE stock_cartons SET quantite=? WHERE taille=?", (int(qte), taille))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/sacs-plastique", methods=["POST"])
def set_sacs_pl():
    if not admin_required():
        return jsonify({"error": "unauthorized"}), 401
    conn = db()
    conn.execute("UPDATE stock_sacs_plastique SET quantite=? WHERE id=1", (int(request.json.get("quantite",0)),))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

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
:root{--bg:#0d1420;--card:#16202e;--border:#243247;--txt:#e8eef5;--mut:#8ba0b8;--acc:#4da3ff;--ok:#2ecc71;--warn:#f1c40f;--bad:#e74c3c;--purple:#a78bfa;}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Segoe UI',sans-serif;background:var(--bg);color:var(--txt);padding-bottom:60px}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
header{display:flex;justify-content:space-between;align-items:center;padding:16px 0;flex-wrap:wrap;gap:8px}
h1{font-size:20px}
.maj{color:var(--mut);font-size:12px}
.btn{background:var(--acc);color:#fff;border:none;border-radius:8px;padding:8px 16px;font-size:14px;cursor:pointer}
.btn.sec{background:var(--border)}
.btn.danger{background:var(--bad)}
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
.section-title{font-size:16px;font-weight:600;margin:22px 0 10px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--mut);text-align:left;padding:8px;border-bottom:1px solid var(--border);font-size:11px;text-transform:uppercase}
td{padding:9px 8px;border-bottom:1px solid var(--border)}
.tag{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:600}
.tag.ok{background:#11301f;color:var(--ok)}.tag.warn{background:#332a10;color:var(--warn)}.tag.bad{background:#3d1513;color:var(--bad)}.tag.info{background:#12283d;color:var(--acc)}
.timeline{display:flex;gap:4px;margin-top:6px}
.tstep{flex:1;text-align:center;font-size:10px;color:var(--mut)}
.tdot{width:100%;height:5px;border-radius:3px;background:var(--border);margin-bottom:4px}
.tdot.done{background:var(--ok)}
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:50;align-items:center;justify-content:center;padding:16px}
.modal.open{display:flex}
.mcard{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px;width:100%;max-width:460px;max-height:90vh;overflow:auto}
.mcard h2{font-size:17px;margin-bottom:14px}
label{display:block;font-size:12px;color:var(--mut);margin:10px 0 4px}
input,select{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--txt);border-radius:8px;padding:9px;font-size:14px}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.mfoot{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
canvas{max-height:260px}
.cal{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.calmois{background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:10px}
.calmois h4{font-size:12px;color:var(--acc);margin-bottom:6px}
.calitem{font-size:11px;padding:4px 6px;border-radius:6px;margin-bottom:4px}
.calitem.due{background:#332a10;color:var(--warn)}
.calitem.late{background:#3d1513;color:var(--bad)}
.calitem.paid{background:#11301f;color:var(--ok);text-decoration:line-through;opacity:.6}
.adm{display:none}
body.admin .adm{display:block}
body.admin .admflex{display:flex}
.pill{font-size:10px;background:var(--border);border-radius:20px;padding:2px 8px;color:var(--mut)}
@media(max-width:600px){.big{font-size:24px}.row2{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div><h1>🐱 Jilypet — Gestion Stock</h1><div class="maj" id="maj">Chargement…</div></div>
  <div style="display:flex;gap:8px">
    <button class="btn sec" onclick="loadData()">↻ Actualiser</button>
    <button class="btn" id="adminBtn" onclick="toggleAdmin()">🔒 Admin</button>
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

<div class="section-title">🚢 Conteneurs <button class="btn adm" style="font-size:12px;padding:4px 12px" onclick="openConteneur()">+ Ajouter</button></div>
<div class="card" style="overflow-x:auto"><table id="tblCont"><thead><tr>
<th>Réf</th><th>Type</th><th>Quantité</th><th>Progression</th><th>Arrivée</th><th class="adm">Actions</th>
</tr></thead><tbody></tbody></table></div>

<div class="section-title">💰 Calendrier paiements</div>
<div class="card"><div class="cal" id="calPaiements"></div></div>

<div class="section-title adm">🎯 Objectifs mensuels <button class="btn" style="font-size:12px;padding:4px 12px" onclick="openObjectif()">+ Saisir</button></div>
<div class="card adm" style="overflow-x:auto"><table id="tblObj"><thead><tr>
<th>Mois</th><th>Obj. clients</th><th>Réel clients</th><th>Obj. chats</th><th>Réel chats</th>
</tr></thead><tbody></tbody></table></div>

<div class="section-title adm">⚙️ Stocks manuels</div>
<div class="card adm">
  <div class="row2">
    <div><label>Cartons A14</label><input type="number" id="inpA14"></div>
    <div><label>Cartons A13</label><input type="number" id="inpA13"></div>
  </div>
  <div class="row2">
    <div><label>Cartons A12</label><input type="number" id="inpA12"></div>
    <div><label>Sacs plastique (Chine)</label><input type="number" id="inpSacsPl"></div>
  </div>
  <div class="mfoot"><button class="btn" onclick="saveStocks()">💾 Enregistrer</button></div>
</div>

</div>

<!-- Modal login -->
<div class="modal" id="modalLogin"><div class="mcard">
<h2>🔒 Accès admin</h2>
<label>Mot de passe</label><input type="password" id="pwd" onkeydown="if(event.key==='Enter')doLogin()">
<div class="mfoot"><button class="btn sec" onclick="closeModals()">Annuler</button><button class="btn" onclick="doLogin()">Connexion</button></div>
</div></div>

<!-- Modal conteneur -->
<div class="modal" id="modalCont"><div class="mcard">
<h2 id="contTitle">🚢 Nouveau conteneur</h2>
<input type="hidden" id="cId">
<label>Type</label><select id="cType"><option value="litiere">Litière</option><option value="sacs_plastique">Sacs plastique</option></select>
<label>Référence bateau</label><input id="cRef" placeholder="ex: MSC-2026-08">
<label>Quantité (sacs)</label><input type="number" id="cQte">
<div class="row2">
<div><label>Date commande</label><input type="date" id="cCmd"></div>
<div><label>Début préparation</label><input type="date" id="cPrep"></div>
</div>
<div class="row2">
<div><label>Départ Chine</label><input type="date" id="cDep"></div>
<div><label>Arrivée France</label><input type="date" id="cArr"></div>
</div>
<div class="row2">
<div><label>Prix (yuan)</label><input type="number" step="0.01" id="cYuan"></div>
<div><label>Prix (€) — base paiements</label><input type="number" step="0.01" id="cEuro"></div>
</div>
<label>Notes</label><input id="cNotes">
<label style="display:flex;align-items:center;gap:8px;margin-top:12px"><input type="checkbox" id="cRecu" style="width:auto"> Conteneur reçu et stocké</label>
<div class="mfoot">
<button class="btn danger" id="cDel" style="display:none" onclick="delConteneur()">Supprimer</button>
<button class="btn sec" onclick="closeModals()">Annuler</button>
<button class="btn" onclick="saveConteneur()">💾 Enregistrer</button></div>
</div></div>

<!-- Modal objectif -->
<div class="modal" id="modalObj"><div class="mcard">
<h2>🎯 Objectif mensuel</h2>
<label>Mois</label><input type="month" id="oMois">
<div class="row2">
<div><label>Objectif nouveaux clients</label><input type="number" id="oObjC"></div>
<div><label>Objectif nouveaux chats</label><input type="number" id="oObjCh"></div>
</div>
<div class="row2">
<div><label>Réel clients (fin de mois)</label><input type="number" id="oReelC"></div>
<div><label>Réel chats (fin de mois)</label><input type="number" id="oReelCh"></div>
</div>
<div class="mfoot"><button class="btn sec" onclick="closeModals()">Annuler</button><button class="btn" onclick="saveObjectif()">💾 Enregistrer</button></div>
</div></div>

<script>
let DATA=null, chStock=null, chObj=null, isAdmin=false;

async function loadData(){
  const r = await fetch('/api/data'); DATA = await r.json();
  document.getElementById('maj').textContent = 'Mis à jour: '+DATA.maj;
  render();
}

function render(){
  const d=DATA;
  // Alertes
  const az=document.getElementById('alertZone');
  if(d.mois_restants<3){az.innerHTML=`<div class="alert bad">⚠️ <b>Stock critique !</b> ${d.mois_restants} mois restants (${d.stock_litiere} sacs). Rupture estimée le ${d.date_rupture}. <b>Commander immédiatement.</b></div>`;}
  else{az.innerHTML=`<div class="alert ok">✅ Stock OK — ${d.mois_restants} mois d'autonomie. Prochaine commande à passer avant le ${d.date_commande_limite}.</div>`;}

  // Cards
  document.getElementById('stockLit').textContent = d.stock_litiere?.toLocaleString('fr')+' sacs';
  document.getElementById('stockLitSub').textContent = `${d.mois_restants} mois · ${d.sacs_mois} sacs/mois · rupture ${d.date_rupture}`;
  const pct=Math.min(100,(d.mois_restants/6)*100);
  const bar=document.getElementById('stockLitBar');
  bar.style.width=pct+'%';
  bar.style.background=d.mois_restants<2?'var(--bad)':d.mois_restants<3?'var(--warn)':'var(--ok)';
  document.getElementById('stockSacs').textContent=d.stock_sacs_plastique?.toLocaleString('fr');
  document.getElementById('cartonsList').innerHTML=d.cartons.map(c=>`<div style="display:flex;justify-content:space-between;padding:3px 0"><span>${c.taille} <span class="pill">${c.sacs_par_carton}</span></span><b>${c.quantite.toLocaleString('fr')}</b></div>`).join('');
  document.getElementById('abonnes').textContent=d.abonnes?.toLocaleString('fr')??'—';
  document.getElementById('chatsSub').textContent=`${d.chats?.toLocaleString('fr')} chats actifs`;

  // Conteneurs
  const tb=document.querySelector('#tblCont tbody');
  tb.innerHTML=d.conteneurs.map(c=>{
    const steps=[['Commande',c.date_commande],['Préparation',c.date_debut_preparation],['Départ',c.date_depart_chine],['Arrivée',c.date_arrivee_france]];
    const now=new Date().toISOString().slice(0,10);
    const tl='<div class="timeline">'+steps.map(([n,dt])=>`<div class="tstep"><div class="tdot ${dt&&dt<=now?'done':''}"></div>${n}${dt?'<br>'+fmtD(dt):''}</div>`).join('')+'</div>';
    const badge=c.recu?'<span class="tag ok">✅ Reçu</span>':(c.date_depart_chine&&c.date_depart_chine<=now?'<span class="tag info">🚢 En mer</span>':'<span class="tag warn">🏭 Préparation</span>');
    return `<tr><td><b>${c.reference||'—'}</b></td><td>${c.type==='litiere'?'Litière':'Sacs pl.'}</td><td>${c.nb_unites.toLocaleString('fr')}</td><td style="min-width:220px">${tl}</td><td>${badge}</td><td class="adm"><button class="btn sec" style="font-size:11px;padding:3px 10px" onclick='openConteneur(${JSON.stringify(c)})'>✏️</button></td></tr>`;
  }).join('')||'<tr><td colspan="6" style="color:var(--mut)">Aucun conteneur</td></tr>';

  // Calendrier paiements
  const cal={};
  d.paiements.forEach(p=>{
    if(!p.date_due)return;
    const k=p.date_due.slice(0,7);
    (cal[k]=cal[k]||[]).push(p);
  });
  const now7=new Date().toISOString().slice(0,10);
  document.getElementById('calPaiements').innerHTML=Object.keys(cal).sort().map(m=>{
    const [y,mo]=m.split('-');
    const nom=['Jan','Fév','Mar','Avr','Mai','Juin','Juil','Août','Sep','Oct','Nov','Déc'][+mo-1]+' '+y;
    return `<div class="calmois"><h4>${nom}</h4>`+cal[m].map(p=>{
      const cls=p.paye?'paid':(p.date_due<now7?'late':'due');
      const lbl=p.type==='litiere_30'?'Litière 30%':p.type==='litiere_70'?'Litière 70%':'Sacs 100%';
      const chk=isAdmin?`<input type="checkbox" ${p.paye?'checked':''} onchange="togglePay(${p.id},this.checked)" style="width:auto;margin-right:4px">`:'';
      return `<div class="calitem ${cls}">${chk}${lbl} — ${(+p.montant_euro).toLocaleString('fr')}€ <span style="opacity:.7">(${p.reference||''})</span></div>`;
    }).join('')+'</div>';
  }).join('')||'<div style="color:var(--mut)">Aucun paiement planifié</div>';

  // Objectifs table
  const to=document.querySelector('#tblObj tbody');
  to.innerHTML=d.objectifs.map(o=>`<tr><td>${o.mois}</td><td>${o.obj_nouveaux_clients||0}</td><td>${o.reel_nouveaux_clients??'—'}</td><td>${o.obj_nouveaux_chats||0}</td><td>${o.reel_nouveaux_chats??'—'}</td></tr>`).join('')||'<tr><td colspan="5" style="color:var(--mut)">Aucun objectif saisi</td></tr>';

  // Stocks manuels inputs
  d.cartons.forEach(c=>{const el=document.getElementById('inp'+c.taille);if(el)el.value=c.quantite;});
  document.getElementById('inpSacsPl').value=d.stock_sacs_plastique;

  // Charts
  drawCharts();
}

function fmtD(s){if(!s)return'';const p=s.split('-');return p[2]+'/'+p[1];}

function drawCharts(){
  const h=[...DATA.historique].reverse();
  if(chStock)chStock.destroy();
  chStock=new Chart(document.getElementById('chartStock'),{type:'line',data:{
    labels:h.map(x=>fmtD(x.date)),
    datasets:[{label:'Stock litière',data:h.map(x=>x.stock_litiere),borderColor:'#4da3ff',backgroundColor:'rgba(77,163,255,.1)',fill:true,tension:.3}]
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

// Admin
function toggleAdmin(){
  if(isAdmin){fetch('/logout',{method:'POST'});isAdmin=false;document.body.classList.remove('admin');document.getElementById('adminBtn').textContent='🔒 Admin';render();}
  else{document.getElementById('modalLogin').classList.add('open');}
}
async function doLogin(){
  const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('pwd').value})});
  if(r.ok){isAdmin=true;document.body.classList.add('admin');document.getElementById('adminBtn').textContent='🔓 Quitter admin';closeModals();render();}
  else alert('Mot de passe incorrect');
}
function closeModals(){document.querySelectorAll('.modal').forEach(m=>m.classList.remove('open'));}

function openConteneur(c){
  document.getElementById('contTitle').textContent=c?'✏️ Modifier conteneur':'🚢 Nouveau conteneur';
  document.getElementById('cId').value=c?.id||'';
  document.getElementById('cType').value=c?.type||'litiere';
  document.getElementById('cRef').value=c?.reference||'';
  document.getElementById('cQte').value=c?.nb_unites||'';
  document.getElementById('cCmd').value=c?.date_commande||'';
  document.getElementById('cPrep').value=c?.date_debut_preparation||'';
  document.getElementById('cDep').value=c?.date_depart_chine||'';
  document.getElementById('cArr').value=c?.date_arrivee_france||'';
  document.getElementById('cYuan').value=c?.prix_yuan||'';
  document.getElementById('cEuro').value=c?.prix_euro||'';
  document.getElementById('cNotes').value=c?.notes||'';
  document.getElementById('cRecu').checked=!!c?.recu;
  document.getElementById('cDel').style.display=c?'block':'none';
  document.getElementById('modalCont').classList.add('open');
}
async function saveConteneur(){
  const id=document.getElementById('cId').value;
  const body={type:cType.value,reference:cRef.value,nb_unites:+cQte.value||0,
    date_commande:cCmd.value,date_debut_preparation:cPrep.value,date_depart_chine:cDep.value,
    date_arrivee_france:cArr.value,prix_yuan:+cYuan.value||0,prix_euro:+cEuro.value||0,
    notes:cNotes.value,recu:document.getElementById('cRecu').checked?1:0};
  await fetch(id?'/api/conteneur/'+id:'/api/conteneur',{method:id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  closeModals();loadData();
}
async function delConteneur(){
  if(!confirm('Supprimer ce conteneur et ses paiements ?'))return;
  await fetch('/api/conteneur/'+document.getElementById('cId').value,{method:'DELETE'});
  closeModals();loadData();
}
async function togglePay(id,paye){
  await fetch('/api/paiement/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({paye})});
  loadData();
}
function openObjectif(){document.getElementById('modalObj').classList.add('open');}
async function saveObjectif(){
  const body={mois:oMois.value,obj_nouveaux_clients:+oObjC.value||0,obj_nouveaux_chats:+oObjCh.value||0,
    reel_nouveaux_clients:oReelC.value?+oReelC.value:null,reel_nouveaux_chats:oReelCh.value?+oReelCh.value:null};
  if(!body.mois)return alert('Choisis un mois');
  await fetch('/api/objectif',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  closeModals();loadData();
}
async function saveStocks(){
  await fetch('/api/cartons',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({A14:+inpA14.value||0,A13:+inpA13.value||0,A12:+inpA12.value||0})});
  await fetch('/api/sacs-plastique',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({quantite:+inpSacsPl.value||0})});
  alert('✅ Stocks enregistrés');loadData();
}

loadData();
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
