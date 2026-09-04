import os
import re
import sqlite3
import requests
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)
DB_FILE = 'vgc_data.db'
DEFAULT_USER = "polilla02"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS user_teams (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        team_name TEXT UNIQUE,
        pokemon_list TEXT,
        pokepaste_url TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        raw_paste TEXT DEFAULT '',
        date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    try: cursor.execute("ALTER TABLE user_teams ADD COLUMN pokepaste_url TEXT DEFAULT ''")
    except: pass
    try: cursor.execute("ALTER TABLE user_teams ADD COLUMN raw_paste TEXT DEFAULT ''")
    except: pass

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS series_matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        opponent TEXT,
        result TEXT DEFAULT 'En curso',
        misplay_reason TEXT DEFAULT 'Sin categorizar',
        notes TEXT DEFAULT '',
        date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        series_id INTEGER,
        game_num INTEGER,
        team_name TEXT DEFAULT 'Equipo Principal Polilla',
        my_lead TEXT,
        my_back TEXT,
        opp_lead TEXT,
        opp_back TEXT,
        result TEXT,
        my_mega TEXT DEFAULT 'Ninguna',
        opp_mega TEXT DEFAULT 'Ninguna',
        archetype TEXT,
        turns INTEGER,
        first_ko TEXT,
        replay_url TEXT,
        tactical_summary TEXT,
        coach_report TEXT DEFAULT '',
        FOREIGN KEY (series_id) REFERENCES series_matches (id)
    )
    ''')
    try: cursor.execute("ALTER TABLE games ADD COLUMN coach_report TEXT DEFAULT ''")
    except: pass

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

def fetch_pokepaste(url):
    if not url or "pokepast.es" not in url: return ""
    try:
        raw_url = url.strip().rstrip("/")
        if not raw_url.endswith("/raw"): raw_url += "/raw"
        resp = requests.get(raw_url, headers={"User-Agent": "VGC-Coach"}, timeout=5)
        if resp.status_code == 200: return resp.text
    except Exception as e: print(f"Error descargando paste: {e}")
    return ""

def parse_showdown_team(raw_paste):
    if not raw_paste: return []
    text = raw_paste.replace('\r\n', '\n').replace('\r', '\n').strip()
    if not text: return []
    mons = []
    blocks = re.split(r'\n\s*\n', text)
    for block in blocks:
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        if not lines: continue
        first_line = lines[0]
        if '@' in first_line:
            name_part, item = first_line.split('@', 1)
            item = item.strip()
        else:
            name_part = first_line.strip()
            item = "Sin Objeto"
        if '(' in name_part and ')' in name_part:
            inside = name_part[name_part.find('(')+1 : name_part.find(')')].strip()
            if inside in ["M", "F"]: name = name_part.split('(')[0].strip()
            else: name = inside
        else: name = name_part.strip()
        evs, nature, ability = "Sin EVs", "Neutra", "Desconocida"
        for line in lines[1:]:
            line_lower = line.lower()
            if line_lower.startswith("ability:") or line_lower.startswith("habilidad:"): ability = line.split(":", 1)[1].strip()
            elif line_lower.startswith("evs:"): evs = line.split(":", 1)[1].strip()
            elif "nature" in line_lower or "naturaleza" in line_lower:
                clean_nat = line_lower.replace("nature", "").replace("naturaleza", "").strip()
                nature = clean_nat.capitalize()
        mons.append({"name": name, "item": item, "ability": ability, "evs": evs, "nature": nature})
    return mons

def detect_archetype(log_text, opp_team):
    log_lower = log_text.lower()
    team_str = " ".join(opp_team).lower()
    if "trick room" in log_lower or any(p in team_str for p in ["indeedee", "ursaluna", "calyrex-ice", "farigiraf", "torkoal", "dusclops"]): return "Trick Room"
    elif "tailwind" in log_lower or any(p in team_str for p in ["whimsicott", "tornadus", "talonflame", "roaring moon"]): return "Tailwind / Speed Control"
    elif "drizzle" in log_lower or "rain dance" in log_lower or any(p in team_str for p in ["kyogre", "pelipper", "urshifu-rapid-strike"]): return "Rain Weather"
    elif "drought" in log_lower or "sunny day" in log_lower or any(p in team_str for p in ["groudon", "koraidon", "torkoal", "flutter mane"]): return "Sun Weather"
    elif any(p in team_str for p in ["chi-yu", "flutter mane", "urshifu", "chien-pao", "iron bundle"]): return "Hyper Offense"
    else: return "Balance / Positional"

# --- MOTOR DE ANÁLISIS DE REPLAY QUIRÚRGICO TURNO A TURNO ---
def parse_showdown_replay(url, user_name=DEFAULT_USER):
    try:
        clean_url = url.split("?")[0].strip()
        json_url = clean_url + ".json" if not clean_url.endswith(".json") else clean_url
        resp = requests.get(json_url, headers={"User-Agent": "VGC-Coach-App"}, timeout=5)
        if resp.status_code != 200: return None
        
        data = resp.json()
        log = data.get("log", "")
        
        # Extracción de Jugadores
        players = {}
        for line in log.split("\n"):
            parts = line.split("|")
            if len(parts) > 3 and parts[1] == "player":
                players[parts[2]] = parts[3]
                
        user_p = "p1"
        if not user_name: user_name = DEFAULT_USER
        norm_target = "".join(e for e in user_name.lower() if e.isalnum())
        
        for pid, pname in players.items():
            norm_pname = "".join(e for e in pname.lower() if e.isalnum())
            if norm_target in norm_pname or norm_pname in norm_target:
                user_p = pid
                break
                
        opp_p = "p2" if user_p == "p1" else "p1"
        opponent_name = players.get(opp_p, "Rival Showdown")
        if not opponent_name.strip(): opponent_name = "Rival Showdown"

        winner_name = data.get("winner", "")
        if not winner_name:
            for line in log.split("\n"):
                parts = line.split("|")
                if len(parts) > 2 and parts[1] == "win":
                    winner_name = parts[2]

        user_won = False
        if winner_name:
            norm_winner = "".join(e for e in winner_name.lower() if e.isalnum())
            norm_player = "".join(e for e in players.get(user_p, "").lower() if e.isalnum())
            if norm_player and (norm_player in norm_winner or norm_winner in norm_player):
                user_won = True
        
        if "|init|battle" in log:
            games_logs = log.split("|init|battle")
            if len(games_logs) > 1: log = "|init|battle" + games_logs[-1]

        my_team, opp_team, my_leads, opp_leads, my_megas, opp_megas = [], [], [], [], [], []
        turns = 0
        first_ko = None
        
        # AUDITORÍA DETALLADA
        current_turn = 0
        current_attacker_is_user = False
        last_attacker_mon = ""
        last_move_used = ""
        turn_logs = []

        for line in log.split("\n"):
            parts = line.split("|")
            if len(parts) < 2: continue
            cmd = parts[1]

            if cmd == "turn":
                current_turn = int(parts[2])
                turns = current_turn
            elif cmd == "poke" and len(parts) > 3:
                p_id, mon = parts[2], parts[3].split(",")[0].strip()
                if p_id == user_p:
                    if mon not in my_team: my_team.append(mon)
                else:
                    if mon not in opp_team: opp_team.append(mon)
            elif cmd in ["switch", "drag"] and len(parts) > 3:
                slot, mon = parts[2], parts[3].split(",")[0].strip()
                if slot.startswith(user_p) and mon not in my_leads and len(my_leads) < 2: my_leads.append(mon)
                elif slot.startswith(opp_p) and mon not in opp_leads and len(opp_leads) < 2: opp_leads.append(mon)
            elif cmd in ["detailschange", "-mega"] and "Mega" in line:
                slot = parts[2] if len(parts) > 2 else ""
                mega_mon = ""
                for part in parts:
                    if "Mega" in part: mega_mon = part.split(",")[0].strip()
                if mega_mon:
                    if slot.startswith(user_p):
                        if mega_mon not in my_megas: my_megas.append(mega_mon)
                    else:
                        if mega_mon not in opp_megas: opp_megas.append(mega_mon)

            # RASTREO TÁCTICO
            elif cmd == "move" and len(parts) > 3:
                slot = parts[2]
                last_move_used = parts[3]
                last_attacker_mon = slot.split(":")[1].strip() if ":" in slot else slot
                current_attacker_is_user = slot.startswith(user_p)
                
                if last_move_used in ["Tailwind", "Trick Room", "Rain Dance", "Sunny Day"]:
                    who = "Tú" if current_attacker_is_user else f"El rival ({opponent_name})"
                    turn_logs.append(f"<b>[T{current_turn}]</b> {who} activó <b>{last_move_used}</b>.")

            elif cmd in ["-immune", "-fail"]:
                if current_attacker_is_user:
                    target = parts[2].split(":")[1].strip() if len(parts) > 2 and ":" in parts[2] else "su objetivo"
                    turn_logs.append(f"⚠️ <b>[T{current_turn}] Error:</b> Atacaste con <b>{last_attacker_mon}</b> ({last_move_used}) a <b>{target}</b> pero FUE INMUNE / FALLÓ.")

            elif cmd == "-singleturn" and "Protect" in line:
                if current_attacker_is_user:
                    target = parts[2].split(":")[1].strip() if len(parts) > 2 and ":" in parts[2] else "el objetivo"
                    turn_logs.append(f"🛡️ <b>[T{current_turn}] Predicción Fallida:</b> Atacaste con <b>{last_attacker_mon}</b> ({last_move_used}) pero <b>{target}</b> usó Protección.")

            elif cmd == "miss":
                if current_attacker_is_user:
                    turn_logs.append(f"🎯 <b>[T{current_turn}] Miss de Precisión:</b> Tu <b>{last_attacker_mon}</b> falló {last_move_used}.")

            elif cmd == "faint" and len(parts) > 2:
                fainted_mon = parts[2].split(":")[1].strip() if ":" in parts[2] else parts[2]
                side = "Tu" if parts[2].startswith(user_p) else "El rival perdió a"
                if not first_ko:
                    first_ko = f"{fainted_mon} ({'Tuyo' if parts[2].startswith(user_p) else 'Rival'}, T{current_turn})"
                turn_logs.append(f"💀 <b>[T{current_turn}] KO:</b> {side} <b>{fainted_mon}</b> cayó debilitado.")

        my_backs = [m for m in my_team if m not in my_leads][:2]
        opp_backs = [m for m in opp_team if m not in opp_leads][:2]
        archetype = detect_archetype(log, opp_team)
        my_mega_str = " / ".join(set(my_megas)) if my_megas else "Ninguna"
        opp_mega_str = " / ".join(set(opp_megas)) if opp_megas else "Ninguna"
        
        tactical_notes = [f"<b>Duración:</b> {turns} turnos", f"<b>Arquetipo:</b> {archetype}"]
        if first_ko: tactical_notes.append(f"<b>Primer KO:</b> {first_ko}")
        if opp_mega_str != "Ninguna": tactical_notes.append(f"<b>Mega Rival:</b> {opp_mega_str}")
        if my_mega_str != "Ninguna": tactical_notes.append(f"<b>Tu Mega:</b> {my_mega_str}")

        # INFORME DETALLADO
        report_header = "<span style='color: var(--loss-color); font-weight: 900;'>❌ ANÁLISIS DETALLADO DE LA DERROTA:</span>" if not user_won else "<span style='color: var(--win-color); font-weight: 900;'>✅ ANÁLISIS DE LA VICTORIA:</span>"
        
        if turn_logs:
            coach_report_str = f"{report_header}<br><br>" + "<br>".join(turn_logs)
        else:
            coach_report_str = f"{report_header}<br>Combate fluido sin errores de precisión ni chocar contra Protecciones. Se resolvió por posicionamiento y daño directo."

        return {
            "opponent": opponent_name,
            "result": "Victoria" if user_won else "Derrota",
            "my_lead": " / ".join(my_leads) if my_leads else "N/A",
            "my_back": " / ".join(my_backs) if my_backs else "N/A",
            "opp_lead": " / ".join(opp_leads) if opp_leads else "N/A",
            "opp_back": " / ".join(opp_backs) if opp_backs else "N/A",
            "my_mega": my_mega_str, "opp_mega": opp_mega_str,
            "archetype": archetype, "turns": turns,
            "first_ko": first_ko or "Sin KOs", "replay_url": clean_url,
            "tactical_summary": " • ".join(tactical_notes),
            "coach_report": coach_report_str
        }
    except Exception as e:
        print(f"Error parseando replay: {e}")
        return None

@app.route('/')
def index():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id, team_name, pokemon_list, pokepaste_url, notes, raw_paste FROM user_teams ORDER BY id DESC")
    user_teams = []
    for r in cursor.fetchall():
        parsed_mons = parse_showdown_team(r[5]) if r[5] else []
        user_teams.append({"id": r[0], "name": r[1], "pokemon": r[2], "paste": r[3], "notes": r[4], "raw_paste": r[5], "parsed_mons": parsed_mons})
    cursor.execute("SELECT id, opponent, result, misplay_reason, notes, date FROM series_matches ORDER BY id DESC")
    series_rows = cursor.fetchall()
    series_list, total_series_wins = [], 0
    total_series_count = len(series_rows)
    for s in series_rows:
        s_id, opp, s_res, misplay, notes, date = s
        cursor.execute("SELECT game_num, team_name, my_lead, my_back, opp_lead, opp_back, result, my_mega, opp_mega, archetype, turns, replay_url, tactical_summary, coach_report FROM games WHERE series_id = ? ORDER BY game_num ASC", (s_id,))
        games = cursor.fetchall()
        g_wins = sum(1 for g in games if g[6] == 'Victoria')
        g_losses = sum(1 for g in games if g[6] == 'Derrota')
        if g_wins >= 2: calc_result = "Victoria (BO3)"
        elif g_losses >= 2: calc_result = "Derrota (BO3)"
        else: calc_result = f"En curso ({g_wins}-{g_losses})"
        if calc_result == "Victoria (BO3)": total_series_wins += 1
        series_list.append({"id": s_id, "opponent": opp, "result": calc_result, "misplay": misplay, "notes": notes, "date": date, "games": games})
    
    series_winrate = round((total_series_wins / total_series_count * 100), 1) if total_series_count > 0 else 0
    cursor.execute("SELECT my_lead, COUNT(*), SUM(CASE WHEN result = 'Victoria' THEN 1 ELSE 0 END) FROM games GROUP BY my_lead HAVING COUNT(*) >= 1")
    lead_stats = [{"lead": r[0], "total": r[1], "wins": r[2], "wr": round((r[2]/r[1]*100), 1)} for r in cursor.fetchall()]
    cursor.execute("SELECT misplay_reason, COUNT(*) FROM series_matches WHERE result LIKE 'Derrota%' GROUP BY misplay_reason")
    misplay_stats = [{"reason": r[0], "count": r[1]} for r in cursor.fetchall()]
    cursor.execute("SELECT team_name, COUNT(*), SUM(CASE WHEN result = 'Victoria' THEN 1 ELSE 0 END) FROM games GROUP BY team_name")
    team_performance = [{"name": r[0], "total": r[1], "wins": r[2], "wr": round((r[2]/r[1]*100), 1)} for r in cursor.fetchall()]
    cursor.execute("SELECT archetype, COUNT(*), SUM(CASE WHEN result = 'Victoria' THEN 1 ELSE 0 END) FROM games GROUP BY archetype")
    archetype_stats = [{"arch": r[0], "total": r[1], "wins": r[2], "wr": round((r[2]/r[1]*100), 1)} for r in cursor.fetchall()]
    cursor.execute("SELECT opp_mega, COUNT(*), SUM(CASE WHEN result = 'Victoria' THEN 1 ELSE 0 END) FROM games WHERE opp_mega != 'Ninguna' GROUP BY opp_mega")
    mega_stats = [{"mega": r[0], "total": r[1], "wins": r[2], "wr": round((r[2]/r[1]*100), 1)} for r in cursor.fetchall()]
    cursor.execute("SELECT SUM(cp) FROM tournaments")
    total_cp = cursor.fetchone()[0] or 0
    cp_pct = round(min((total_cp / 900) * 100, 100), 1)
    
    coach_advice = ["Auditoría por turno activada. Los replays ahora desglosan exactamente qué ocurrió en cada momento crítico."]
    conn.close()
    return render_template('dashboard.html', user_teams=user_teams, series_list=series_list, series_winrate=series_winrate, total_series_count=total_series_count, total_series_wins=total_series_wins, lead_stats=lead_stats, misplay_stats=misplay_stats, team_performance=team_performance, archetype_stats=archetype_stats, mega_stats=mega_stats, total_cp=total_cp, cp_pct=cp_pct, coach_advice="<br><br>".join(coach_advice), default_user=DEFAULT_USER)

@app.route('/add_team', methods=['POST'])
def add_team():
    team_name = request.form.get('team_name')
    pokepaste_url = request.form.get('pokepaste_url', '')
    notes = request.form.get('notes', '')
    raw_paste = fetch_pokepaste(pokepaste_url)
    parsed_mons = parse_showdown_team(raw_paste)
    pokemon_list = ", ".join([mon['name'] for mon in parsed_mons])
    if not pokemon_list: pokemon_list = "⚠️ Error leyendo Paste (enlace inválido o sin contenido)."
    if team_name:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO user_teams (team_name, pokemon_list, pokepaste_url, notes, raw_paste) VALUES (?, ?, ?, ?, ?)", 
                       (team_name, pokemon_list, pokepaste_url, notes, raw_paste))
        conn.commit()
        conn.close()
    return redirect(url_for('index'))

@app.route('/parse_replay', methods=['POST'])
def parse_replay_route():
    url = request.form.get('replay_url')
    user_name = request.form.get('user_name') or DEFAULT_USER
    series_id = request.form.get('series_id')
    team_name = request.form.get('team_name') or 'Equipo Principal Polilla'
    parsed = parse_showdown_replay(url, user_name)
    if parsed:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        if not series_id or series_id == "new":
            cursor.execute("INSERT INTO series_matches (opponent, result) VALUES (?, ?)", (parsed['opponent'], 'En curso'))
            series_id = cursor.lastrowid
        cursor.execute("SELECT COUNT(*) FROM games WHERE series_id = ?", (series_id,))
        game_num = cursor.fetchone()[0] + 1
        cursor.execute('''INSERT INTO games (series_id, game_num, team_name, my_lead, my_back, opp_lead, opp_back, result, my_mega, opp_mega, archetype, turns, first_ko, replay_url, tactical_summary, coach_report)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', 
            (series_id, game_num, team_name, parsed['my_lead'], parsed['my_back'], parsed['opp_lead'], parsed['opp_back'], parsed['result'], parsed['my_mega'], parsed['opp_mega'], parsed['archetype'], parsed['turns'], parsed['first_ko'], parsed['replay_url'], parsed['tactical_summary'], parsed['coach_report']))
        cursor.execute("SELECT result FROM games WHERE series_id = ?", (series_id,))
        results = [r[0] for r in cursor.fetchall()]
        wins, losses = results.count('Victoria'), results.count('Derrota')
        if wins >= 2: cursor.execute("UPDATE series_matches SET result = 'Victoria (BO3)' WHERE id = ?", (series_id,))
        elif losses >= 2: cursor.execute("UPDATE series_matches SET result = 'Derrota (BO3)' WHERE id = ?", (series_id,))
        conn.commit()
        conn.close()
    return redirect(url_for('index'))

@app.route('/update_misplay', methods=['POST'])
def update_misplay():
    series_id = request.form.get('series_id')
    reason = request.form.get('reason')
    notes = request.form.get('notes', '')
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE series_matches SET misplay_reason = ?, notes = ? WHERE id = ?", (reason, notes, series_id))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/delete_series', methods=['POST'])
def delete_series():
    series_id = request.form.get('series_id')
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM games WHERE series_id = ?", (series_id,))
    cursor.execute("DELETE FROM series_matches WHERE id = ?", (series_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/delete_team', methods=['POST'])
def delete_team():
    team_id = request.form.get('team_id')
    if team_id:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_teams WHERE id = ?", (team_id,))
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
