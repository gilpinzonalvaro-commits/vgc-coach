import os
import re
import requests
import psycopg2
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)
DEFAULT_USER = "polilla02"

# Conexión a la Base de Datos en la Nube (Neon / Render)
def get_db():
    db_url = os.environ.get("DATABASE_URL")
    if db_url and db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(db_url)

def init_db():
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Tabla de Equipos
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_teams (
            id SERIAL PRIMARY KEY,
            team_name TEXT UNIQUE,
            pokemon_list TEXT,
            pokepaste_url TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            raw_paste TEXT DEFAULT '',
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        # Tabla de Series BO3
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS series_matches (
            id SERIAL PRIMARY KEY,
            opponent TEXT,
            result TEXT DEFAULT 'En curso',
            misplay_reason TEXT DEFAULT 'Sin categorizar',
            notes TEXT DEFAULT '',
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        # Tabla de Partidas Individuales
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS games (
            id SERIAL PRIMARY KEY,
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

        # Tabla de CPs
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS tournaments (
            id SERIAL PRIMARY KEY,
            name TEXT,
            cp INTEGER,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error inicializando base de datos en la nube: {e}")

# Inicializar DB al arrancar la app
if os.environ.get("DATABASE_URL"):
    init_db()

def fetch_pokepaste(url):
    if not url or "pokepast.es" not in url: return ""
    try:
        raw_url = url.strip().rstrip("/")
        if not raw_url.endswith("/raw"): raw_url += "/raw"
        resp = requests.get(raw_url, headers={"User-Agent": "VGC-Coach"}, timeout=5)
        if resp.status_code == 200: return resp.text
    except Exception as e:
        print(f"Error descargando paste: {e}")
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
    if "trick room" in log_lower or any(p in team_str for p in ["indeedee", "ursaluna", "calyrex-ice", "farigiraf", "torkoal", "dusclops", "sinistcha"]): return "Trick Room"
    elif "tailwind" in log_lower or any(p in team_str for p in ["whimsicott", "tornadus", "talonflame", "roaring moon"]): return "Tailwind / Speed Control"
    elif "drizzle" in log_lower or "rain dance" in log_lower or any(p in team_str for p in ["kyogre", "pelipper", "urshifu-rapid-strike"]): return "Rain Weather"
    elif "drought" in log_lower or "sunny day" in log_lower or any(p in team_str for p in ["groudon", "koraidon", "torkoal", "flutter mane"]): return "Sun Weather"
    elif any(p in team_str for p in ["chi-yu", "flutter mane", "urshifu", "chien-pao", "iron bundle"]): return "Hyper Offense"
    else: return "Balance / Positional"

def analyze_with_ai(clean_actions, user_name, opponent_name, user_won, my_leads, opp_leads, my_team, opp_team):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
        
    url = "https://api.openai.com/v1/chat/completions"
    color = "#22c55e" if user_won else "#ef4444"
    resultado = "GANÓ" if user_won else "PERDIÓ"
    
    system_prompt = """
    ROL: El Coach Táctico Principal de un jugador aspirante al Campeonato Mundial de Pokémon VGC. Tu nivel de análisis es hiper-especializado, quirúrgico y directo.
    FORMATO DEL METAGAME: VGC con Megaevoluciones activas (NO existe la Teracristalización).
    OBJETIVO: Generar una auditoría estricta en HTML usando los datos del combate.
    """
    
    user_prompt = f"""
    Analiza este combate donde tu jugador '{user_name}' {resultado} la partida contra '{opponent_name}'.

    DATOS CLAVE:
    - Equipo de {user_name}: {', '.join(my_team)}
    - Leads de {user_name}: {', '.join(my_leads)}
    - Equipo de {opponent_name}: {', '.join(opp_team)}
    - Leads de {opponent_name}: {', '.join(opp_leads)}

    SECUENCIA DE ACCIONES TURNO A TURNO:
    {clean_actions}

    REGLAS DE EVALUACIÓN TÁCTICA:
    1. TEAM PREVIEW Y MEGA-FIT: Analiza la elección de la Mega frente a los 6 del rival.
    2. SPEED CONTROL: Determina quién controló la velocidad y si se pudo denegar.
    3. PUNTO DE INFLEXIÓN: Identifica el turno exacto donde se decidió el combate.
    4. ADAPTACIÓN GAME 2: Da instrucciones concretas para el siguiente juego (cambio de Lead o Mega).

    DEVUELVE EXACTAMENTE ESTA ESTRUCTURA HTML (Cero Markdown):
    <div style='border-bottom: 2px solid {color}; padding-bottom: 6px; margin-bottom: 12px;'>
        <b style='color: {color}; font-size: 1.15em;'>🤖 COACH IA: AUDITORÍA TÁCTICA DE NIVEL MUNDIAL</b>
    </div>
    <p>📌 <b>1. Team Preview y Mega-Fit:</b><br>[Análisis profundo del lead y la mega]</p>
    <p>⏱️ <b>2. Control del Ritmo y Speed Control:</b><br>[Análisis de control de velocidad]</p>
    <p>📉 <b>3. Punto de Inflexión y KOs Clave:</b><br>[Turno crítico y motivo]</p>
    <p>🎯 <b>4. Plan de Ajuste Táctico para el Game 2:</b><br>[Consejos específicos]</p>
    """

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.2
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=20)
        if response.status_code == 200:
            data = response.json()
            return data['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f"Error OpenAI: {e}")
        
    return None

def generate_heuristic_report(user_won, my_leads, opp_leads, my_backs, user_faints_log, opp_faints_log, opp_speed_control, archetype, opponent_name):
    report_blocks = []
    if not user_won:
        report_blocks.append("<div style='border-bottom: 2px solid var(--loss-color); padding-bottom: 6px; margin-bottom: 12px;'><b style='color: var(--loss-color); font-size: 1.15em;'>👑 INFORME TÁCTICO (MODO OFFLINE)</b></div>")
        my_lead_str = " + ".join(my_leads) if my_leads else "Tu pareja inicial"
        opp_lead_str = " + ".join(opp_leads) if opp_leads else "la pareja rival"
        report_blocks.append(f"📌 <b>1. Auditoría de Leads:</b> Abriste con <b>{my_lead_str}</b> frente a <b>{opp_lead_str}</b>.")
    else:
        report_blocks.append("<div style='border-bottom: 2px solid var(--win-color); padding-bottom: 6px; margin-bottom: 12px;'><b style='color: var(--win-color); font-size: 1.15em;'>👑 ANÁLISIS DE VICTORIA TÁCTICA</b></div>")
        report_blocks.append("✅ <b>Ejecución Impecable:</b> Controlaste el ritmo del combate perfectamente.")
    return "<br><br>".join(report_blocks)

def parse_showdown_replay(url, user_name=DEFAULT_USER):
    try:
        clean_url = url.split("?")[0].strip()
        json_url = clean_url + ".json" if not clean_url.endswith(".json") else clean_url
        resp = requests.get(json_url, headers={"User-Agent": "VGC-Coach"}, timeout=5)
        if resp.status_code != 200: return None
        
        data = resp.json()
        log = data.get("log", "")
        
        players = {}
        for line in log.split("\n"):
            parts = line.split("|")
            if len(parts) > 3 and parts[1] == "player":
                players[parts[2]] = parts[3]
                
        user_p = "p1"
        norm_target = "".join(e for e in user_name.lower() if e.isalnum())
        for pid, pname in players.items():
            norm_pname = "".join(e for e in pname.lower() if e.isalnum())
            if norm_target in norm_pname or norm_pname in norm_target:
                user_p = pid
                break
                
        opp_p = "p2" if user_p == "p1" else "p1"
        opponent_name = players.get(opp_p, "Rival Showdown")

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
        turns, first_ko = 0, None
        current_turn, opp_speed_control = 0, False
        turn_logs, user_faints_log, opp_faints_log, clean_actions = [], [], [], []

        for line in log.split("\n"):
            parts = line.split("|")
            if len(parts) < 2: continue
            cmd = parts[1]

            if cmd == "turn":
                current_turn = int(parts[2])
                turns = current_turn
                clean_actions.append(f"\n--- TURNO {current_turn} ---")
            elif cmd == "poke" and len(parts) > 3:
                p_id, mon = parts[2], parts[3].split(",")[0].strip()
                if p_id == user_p:
                    if mon not in my_team: my_team.append(mon)
                else:
                    if mon not in opp_team: opp_team.append(mon)
            elif cmd in ["switch", "drag"] and len(parts) > 3:
                slot, mon = parts[2], parts[3].split(",")[0].strip()
                who = user_name if slot.startswith(user_p) else opponent_name
                clean_actions.append(f"{who} saca a: {mon}")
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
            elif cmd == "move" and len(parts) > 3:
                slot, move = parts[2], parts[3]
                who = user_name if slot.startswith(user_p) else opponent_name
                clean_actions.append(f"{who} usa {move}")
                if not slot.startswith(user_p) and move in ["Tailwind", "Trick Room"]: opp_speed_control = True
                if move in ["Tailwind", "Trick Room", "Rain Dance", "Sunny Day"]:
                    turn_logs.append(f"🌪️ <b>[T{current_turn}]</b> {'Tú' if slot.startswith(user_p) else 'El rival'} usó <b>{move}</b>.")
            elif cmd == "faint" and len(parts) > 2:
                fainted_mon = parts[2].split(":")[1].strip() if ":" in parts[2] else parts[2]
                who_lost = user_name if parts[2].startswith(user_p) else opponent_name
                clean_actions.append(f"💀 KO: {who_lost} pierde a {fainted_mon}")
                if parts[2].startswith(user_p):
                    user_faints_log.append((current_turn, fainted_mon))
                    if not first_ko: first_ko = f"{fainted_mon} (Tuyo, T{current_turn})"
                    turn_logs.append(f"💀 <b>[T{current_turn}] KO:</b> Tu <b>{fainted_mon}</b> cayó.")
                else:
                    opp_faints_log.append((current_turn, fainted_mon))
                    if not first_ko: first_ko = f"{fainted_mon} (Rival, T{current_turn})"
                    turn_logs.append(f"💥 <b>[T{current_turn}] KO:</b> Rival <b>{fainted_mon}</b> cayó.")

        my_backs = [m for m in my_team if m not in my_leads][:2]
        opp_backs = [m for m in opp_team if m not in opp_leads][:2]
        archetype = detect_archetype(log, opp_team)
        my_mega_str = " / ".join(set(my_megas)) if my_megas else "Ninguna"
        opp_mega_str = " / ".join(set(opp_megas)) if opp_megas else "Ninguna"
        
        tactical_notes = [f"<b>Duración:</b> {turns} turnos", f"<b>Arquetipo:</b> {archetype}"]
        if first_ko: tactical_notes.append(f"<b>Primer KO:</b> {first_ko}")
        if opp_mega_str != "Ninguna": tactical_notes.append(f"<b>Mega Rival:</b> {opp_mega_str}")
        if my_mega_str != "Ninguna": tactical_notes.append(f"<b>Tu Mega:</b> {my_mega_str}")

        actions_summary_str = "\n".join(clean_actions[:60])
        ai_report = analyze_with_ai(actions_summary_str, user_name, opponent_name, user_won, my_leads, opp_leads, my_team, opp_team)
        
        if ai_report: coach_report_str = ai_report
        else: coach_report_str = generate_heuristic_report(user_won, my_leads, opp_leads, my_backs, user_faints_log, opp_faints_log, opp_speed_control, archetype, opponent_name)
        
        if turn_logs:
            turn_by_turn_html = f"<details style='margin-top:16px; cursor:pointer; background: var(--inner-bg); padding: 10px; border-radius: 8px; border: 1px solid var(--border-color);'><summary style='font-weight:900; color:var(--accent-blue);'>📑 Ver Log Básico</summary><div style='font-size:0.88em; margin-top:10px; color:var(--text-color); line-height: 1.6;'>" + "<br>".join(turn_logs) + "</div></details>"
            coach_report_str += turn_by_turn_html

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
    if not os.environ.get("DATABASE_URL"):
        return "⚠️ Error: Falta configurar DATABASE_URL en Render."
        
    conn = get_db()
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
        cursor.execute("SELECT game_num, team_name, my_lead, my_back, opp_lead, opp_back, result, my_mega, opp_mega, archetype, turns, replay_url, tactical_summary, coach_report FROM games WHERE series_id = %s ORDER BY game_num ASC", (s_id,))
        games = cursor.fetchall()
        g_wins = sum(1 for g in games if g[6] == 'Victoria')
        g_losses = sum(1 for g in games if g[6] == 'Derrota')
        if g_wins >= 2: calc_result = "Victoria (BO3)"
        elif g_losses >= 2: calc_result = "Derrota (BO3)"
        else: calc_result = f"En curso ({g_wins}-{g_losses})"
        if calc_result == "Victoria (BO3)": total_series_wins += 1
        series_list.append({"id": s_id, "opponent": opp, "result": calc_result, "misplay": misplay, "notes": notes, "date": date.strftime('%Y-%m-%d') if date else '', "games": games})
    
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
    
    coach_advice = ["¡BASE DE DATOS SEGURA EN LA NUBE! Tus equipos ya no se borrarán nunca más."]
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
    if not pokemon_list: pokemon_list = "⚠️ Error leyendo Paste."
    if team_name:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_teams (team_name, pokemon_list, pokepaste_url, notes, raw_paste) 
            VALUES (%s, %s, %s, %s, %s) 
            ON CONFLICT (team_name) DO UPDATE SET 
            pokemon_list = EXCLUDED.pokemon_list, 
            pokepaste_url = EXCLUDED.pokepaste_url, 
            notes = EXCLUDED.notes, 
            raw_paste = EXCLUDED.raw_paste
        """, (team_name, pokemon_list, pokepaste_url, notes, raw_paste))
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
        conn = get_db()
        cursor = conn.cursor()
        if not series_id or series_id == "new":
            cursor.execute("INSERT INTO series_matches (opponent, result) VALUES (%s, %s) RETURNING id", (parsed['opponent'], 'En curso'))
            series_id = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM games WHERE series_id = %s", (series_id,))
        game_num = cursor.fetchone()[0] + 1
        cursor.execute('''INSERT INTO games (series_id, game_num, team_name, my_lead, my_back, opp_lead, opp_back, result, my_mega, opp_mega, archetype, turns, replay_url, tactical_summary, coach_report)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''', 
            (series_id, game_num, team_name, parsed['my_lead'], parsed['my_back'], parsed['opp_lead'], parsed['opp_back'], parsed['result'], parsed['my_mega'], parsed['opp_mega'], parsed['archetype'], parsed['turns'], parsed['replay_url'], parsed['tactical_summary'], parsed['coach_report']))
        
        cursor.execute("SELECT result FROM games WHERE series_id = %s", (series_id,))
        results = [r[0] for r in cursor.fetchall()]
        wins, losses = results.count('Victoria'), results.count('Derrota')
        if wins >= 2: cursor.execute("UPDATE series_matches SET result = 'Victoria (BO3)' WHERE id = %s", (series_id,))
        elif losses >= 2: cursor.execute("UPDATE series_matches SET result = 'Derrota (BO3)' WHERE id = %s", (series_id,))
        conn.commit()
        conn.close()
    return redirect(url_for('index'))

@app.route('/update_misplay', methods=['POST'])
def update_misplay():
    series_id = request.form.get('series_id')
    reason = request.form.get('reason')
    notes = request.form.get('notes', '')
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE series_matches SET misplay_reason = %s, notes = %s WHERE id = %s", (reason, notes, series_id))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/delete_series', methods=['POST'])
def delete_series():
    series_id = request.form.get('series_id')
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM games WHERE series_id = %s", (series_id,))
    cursor.execute("DELETE FROM series_matches WHERE id = %s", (series_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/delete_team', methods=['POST'])
def delete_team():
    team_id = request.form.get('team_id')
    if team_id:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_teams WHERE id = %s", (team_id,))
        conn.commit()
        conn.close()
    return redirect(url_for('index'))

@app.route('/add_cp', methods=['POST'])
def add_cp():
    name = request.form.get('name') or 'Torneo VGC'
    cp = int(request.form.get('cp') or 0)
    if cp > 0:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tournaments (name, cp) VALUES (%s, %s)", (name, cp))
        conn.commit()
        conn.close()
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
