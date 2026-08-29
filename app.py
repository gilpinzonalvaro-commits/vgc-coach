import os
import sqlite3
import requests
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)
DB_FILE = 'vgc_data.db'

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        opponent TEXT,
        my_lead TEXT,
        my_back TEXT,
        opp_lead TEXT,
        opp_back TEXT,
        result TEXT,
        mega TEXT,
        notes TEXT,
        replay_url TEXT,
        date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS tournaments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        cp INTEGER,
        date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    conn.commit()
    conn.close()

init_db()

def parse_showdown_replay(url, user_name=""):
    try:
        clean_url = url.split("?")[0].strip()
        if not clean_url.endswith(".json"):
            json_url = clean_url + ".json"
        else:
            json_url = clean_url

        resp = requests.get(json_url, headers={"User-Agent": "VGC-Coach-App"}, timeout=5)
        if resp.status_code != 200:
            return None

        data = resp.json()
        log = data.get("log", "")
        players = {}
        user_p = "p1"

        for line in log.split("\n"):
            parts = line.split("|")
            if len(parts) > 3 and parts[1] == "player":
                p_id = parts[2]
                p_name = parts[3]
                players[p_id] = p_name
                if user_name and user_name.lower() in p_name.lower():
                    user_p = p_id

        opp_p = "p2" if user_p == "p1" else "p1"
        winner = data.get("winner", "")
        user_won = (winner.lower() == players.get(user_p, "").lower())

        my_team, opp_team = [], []
        my_leads, opp_leads = [], []
        megas = []

        for line in log.split("\n"):
            parts = line.split("|")
            if len(parts) > 3:
                if parts[1] == "poke":
                    p_id = parts[2]
                    mon = parts[3].split(",")[0].strip()
                    if p_id == user_p:
                        if mon not in my_team: my_team.append(mon)
                    else:
                        if mon not in opp_team: opp_team.append(mon)
                elif parts[1] in ["switch", "drag"]:
                    slot = parts[2]
                    mon = parts[3].split(",")[0].strip()
                    if slot.startswith(user_p) and mon not in my_leads and len(my_leads) < 2:
                        my_leads.append(mon)
                    elif slot.startswith(opp_p) and mon not in opp_leads and len(opp_leads) < 2:
                        opp_leads.append(mon)
                elif parts[1] in ["detailschange", "-mega"]:
                    if "Mega" in line:
                        for part in parts:
                            if "Mega" in part:
                                megas.append(part.split(",")[0].strip())

        my_backs = [m for m in my_team if m not in my_leads][:2]
        opp_backs = [m for m in opp_team if m not in opp_leads][:2]

        return {
            "opponent": players.get(opp_p, "Rival Showdown"),
            "result": "Victoria" if user_won else "Derrota",
            "my_lead": " / ".join(my_leads) if my_leads else "N/A",
            "my_back": " / ".join(my_backs) if my_backs else "N/A",
            "opp_lead": " / ".join(opp_leads) if opp_leads else "N/A",
            "opp_back": " / ".join(opp_backs) if opp_backs else "N/A",
            "mega": " / ".join(set(megas)) if megas else "Ninguna",
            "replay_url": clean_url
        }
    except Exception as e:
        print(f"Error parseando replay: {e}")
        return None

@app.route('/')
def index():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("SELECT opponent, my_lead, my_back, opp_lead, opp_back, result, mega, notes, date, replay_url FROM matches")
    matches = cursor.fetchall()

    cursor.execute("SELECT COUNT(*), SUM(CASE WHEN result = 'Victoria' THEN 1 ELSE 0 END) FROM matches")
    total_matches, total_wins = cursor.fetchone()
    total_matches = total_matches or 0
    total_wins = total_wins or 0
    winrate = round((total_wins / total_matches * 100), 1) if total_matches > 0 else 0

    cursor.execute("SELECT SUM(cp) FROM tournaments")
    total_cp = cursor.fetchone()[0] or 0
    cp_pct = round(min((total_cp / 900) * 100, 100), 1)

    coach_advice = []
    if total_matches >= 3:
        if winrate >= 65:
            coach_advice.append("<b>Rendimiento Excelente:</b> Tu tasa de victoria actual justifica priorizar torneos.")
        elif winrate >= 50:
            coach_advice.append("<b>Estabilidad Competitiva:</b> Winrate superior al 50%. Revisa la selección de leads.")
        else:
            coach_advice.append("⚠️ <b>Ajuste Necesario:</b> Winrate por debajo del 50%. Revisa la composición defensiva.")

        cursor.execute("SELECT mega FROM matches WHERE result = 'Derrota' AND mega != 'Ninguna'")
        loss_megas = [r[0] for r in cursor.fetchall()]
        if loss_megas:
            common_mega = max(set(loss_megas), key=loss_megas.count)
            coach_advice.append(f"🔥 <b>Amenaza Crítica:</b> Presentas bajas victorias contra composiciones con <b>{common_mega}</b>.")
    else:
        coach_advice.append("Registra o parsea al menos 3 partidas para habilitar el diagnóstico estadístico.")

    conn.close()
    return render_template('dashboard.html', 
                           matches=matches, 
                           total_matches=total_matches, 
                           total_wins=total_wins, 
                           winrate=winrate, 
                           total_cp=total_cp, 
                           cp_pct=cp_pct, 
                           coach_advice="<br><br>".join(coach_advice))

@app.route('/parse_replay', methods=['POST'])
def parse_replay_route():
    url = request.form.get('replay_url')
    user_name = request.form.get('user_name', '')
    parsed = parse_showdown_replay(url, user_name)
    if parsed:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO matches (opponent, my_lead, my_back, opp_lead, opp_back, result, mega, notes, replay_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (parsed['opponent'], parsed['my_lead'], parsed['my_back'], parsed['opp_lead'], parsed['opp_back'], parsed['result'], parsed['mega'], '', parsed['replay_url']))
        conn.commit()
        conn.close()
    return redirect(url_for('index'))

@app.route('/add_cp', methods=['POST'])
def add_cp():
    name = request.form.get('name') or 'Torneo VGC'
    cp = int(request.form.get('cp') or 0)
    if cp > 0:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tournaments (name, cp) VALUES (?, ?)", (name, cp))
        conn.commit()
        conn.close()
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
