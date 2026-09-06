import os
import re
import sqlite3
import requests
import psycopg2
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)
DEFAULT_USER = "polilla02"
DB_FILE = 'vgc_data.db'

def get_db():
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        return psycopg2.connect(db_url), "postgres"
    else:
        return sqlite3.connect(DB_FILE), "sqlite"

def init_db():
    try:
        conn, db_type = get_db()
        cursor = conn.cursor()
        pk_type = "SERIAL PRIMARY KEY" if db_type == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
        
        cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS user_teams (
            id {pk_type},
            team_name TEXT UNIQUE,
            pokemon_list TEXT,
            pokepaste_url TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            raw_paste TEXT DEFAULT '',
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS series_matches (
            id {pk_type},
            opponent TEXT,
            result TEXT DEFAULT 'En curso',
            misplay_reason TEXT DEFAULT 'Sin categorizar',
            notes TEXT DEFAULT '',
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS games (
            id {pk_type},
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

        cursor.execute(f'''
        CREATE TABLE IF NOT EXISTS tournaments (
            id {pk_type},
            name TEXT,
            cp INTEGER,
            date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        cursor.execute("UPDATE series_matches SET opponent = 'Rival Showdown' WHERE opponent IS NULL OR opponent = '' OR opponent = 'VS';")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error inicializando DB: {e}")

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

# --- MOTOR DE IA UNIVERSAL APLICABLE A CUALQUIER EQUIPO Y ARQUETIPO ---
def analyze_with_ai(clean_actions_text, user_name, opponent_name, user_won, my_leads, opp_leads, my_team, opp_team, archetype):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key: return None
        
    url = "https://api.openai.com/v1/chat/completions"
    color = "#22c55e" if user_won else "#ef4444"
    resultado = "GANÓ" if user_won else "PERDIÓ"
    
    my_team_str = ", ".join(my_team)
    my_leads_str = ", ".join(my_leads)
    opp_team_str = ", ".join(opp_team)
    opp_leads_str = ", ".join(opp_leads)

    system_prompt = (
        "ERES: Coach Táctico de Élite de Pokémon VGC. Tu análisis debe ser 100% REGIDO POR LAS REGLAS OFICIALES Y FIEL AL LOG.\n"
        "FORMATO METAGAME: VGC Gen 9 con Megaevoluciones.\n\n"
        "REGLAS TÁCTICAS DINÁMICAS Y RIGUROSAS:\n"
        "1. TABLA DE TIPOS E INMUNIDADES (OBLIGATORIO): Consulta SIEMPRE la tabla de tipos oficial para los Pokémon involucrados en el combate.\n"
        "   - Respeta estrictamente las inmunidades (Tierra/Eléctrico, Volador/Tierra, Hada/Dragón, Acero/Veneno, Fantasma/Normal-Lucha, Siniestro/Psíquico).\n"
        "   - Evita sugerir movimientos que no afecten por inmunidad o recomendar muros defensivos expuestos a debilidades x4 del rival.\n\n"
        "2. CONTROL DE VELOCIDAD DINÁMICO:\n"
        "   - Evalúa las habilidades de clima/velocidad activas en la partida (Ímpetu Arena, Nado Rápido, Clorofila, Viento Afín, Espacio Raro).\n"
        "   - Aplica los multiplicadores de velocidad reales (2x en clima/Viento Afín) para determinar qué Pokémon actúa primero.\n\n"
        "3. PROHIBIDO ALUCINAR:\n"
        "   - Usa ÚNICAMENTE nombres de ataques y eventos que aparezcan literalmente en el log.\n"
        "   - Basa la recomendación del Game 2 en las 6 opciones del equipo del jugador frente a las 6 del rival, priorizando matchups favorables y control del ritmo."
    )
    
    user_prompt = (
        f"AUDITORÍA DE COMBATE COMPLETO:\n\n"
        f"JUGADORES Y RESULTADO:\n"
        f"- Jugador Principal: '{user_name}' ({resultado} el combate)\n"
        f"- Rival: '{opponent_name}'\n\n"
        f"EQUIPOS:\n"
        f"- Equipo del Jugador: {my_team_str} (Leads Usados: {my_leads_str})\n"
        f"- Equipo del Rival: {opp_team_str} (Leads Usados: {opp_leads_str})\n"
        f"- Arquetipo Detectado: {archetype}\n\n"
        f"LOG REGISTRADO TURNO A TURNO:\n"
        f"{clean_actions_text}\n\n"
        f"Devuelve la auditoría en este formato HTML exacto (sin etiquetas markdown ```html):\n\n"
        f"<div style='border-bottom: 2px solid {color}; padding-bottom: 6px; margin-bottom: 12px;'>\n"
        f"    <b style='color: {color}; font-size: 1.15em;'>🤖 COACH IA: AUDITORÍA TÁCTICA DE NIVEL MUNDIAL</b>\n"
        f"</div>\n"
        f"<p>📌 <b>1. Team Preview y Mega-Fit:</b><br>[Analiza la selección de leads y sinergia según la tabla de tipos oficial...]</p>\n"
        f"<p>⏱️ <b>2. Control del Ritmo y Speed Control:</b><br>[Evalúa la velocidad considerando movimientos de control o habilidades activas en el log...]</p>\n"
        f"<p>📉 <b>3. Punto de Inflexión y KOs Clave:</b><br>[Señala el turno exacto y KOs reales registrados en el log...]</p>\n"
        f"<p>🎯 <b>4. Plan de Ajuste Táctico para el Game 2:</b><br>[Recomendación táctica concreta seleccionando los mejores leads dentro de los 6 Pokémon del jugador frente al rival...]</p>"
    )

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
        "temperature": 0.0
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=25)
        if response.status_code == 200:
            data = response.json()
            return data['choices'][0]['message']['content'].strip()
    except Exception as e:
        print(f"Error OpenAI API: {e}")
        
    return None

def generate_heuristic_report(user_won, my_leads, opp_leads, archetype):
    report_blocks = []
    if not user_won:
        report_blocks.append("<div style='border-bottom: 2px solid var(--loss-color); padding-bottom: 6px; margin-bottom: 12px;'><b style='color: var(--loss-color); font-size: 1.15em;'>👑 INFORME TÁCTICO (MODO OFFLINE)</b></div>")
        report_blocks.append(f"📌 <b>1. Auditoría de Leads:</b> Salida con {' + '.join(my_leads)} contra {' + '.join(opp_leads)}.")
    else:
        report_blocks.append("<div style='border-bottom: 2px solid var(--win-color); padding-bottom: 6px; margin-bottom: 12px;'><b style='color: var(--win-color); font-size: 1.15em;'>👑 ANÁLISIS DE VICTORIA TÁCTICA</b></div>")
        report_blocks.append("✅ <b>Ejecución Impecable:</b> Controlaste el ritmo de la partida.")
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
                p_slot = parts[2].strip()
                p_name = parts[3].strip()
                if p_name: players[p_slot] = p_name
                
        target_user = user_name if (user_name and user_name.strip()) else DEFAULT_USER
        norm_target = "".join(e for e in target_user.lower() if e.isalnum())
        
        user_p = "p1"
        for pid, pname in players.items():
            norm_pname = "".join(e for e in pname.lower() if e.isalnum())
            if norm_target and (norm_target in norm_pname or norm_pname in norm_target):
                user_p = pid
                break
                
        opp_p = "p2" if user_p == "p1" else "p1"
        opponent_name = players.get(opp_p, "Rival Showdown").strip() or "Rival Showdown"

        winner_name = data.get("winner", "")
        user_won = False
        if winner_name:
            norm_winner = "".join(e for e in winner_name.lower() if e.isalnum())
            norm_player = "".join(e for e in players.get(user_p, "").lower() if e.isalnum())
            if norm_player and (norm_player in norm_winner or norm_winner in norm_player):
                user_won = True

        my_team, opp_team, my_leads, opp_leads, my_megas, opp_megas = [], [], [], [], [], []
        turns, first_ko = 0, None
        current_turn = 0
        turn_logs, clean_actions = [], []

        def get_owner(slot_str):
            return target_user if slot_str.startswith(user_p) else opponent_name

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
                hp = parts[4] if len(parts) > 4 else ""
                who = get_owner(slot)
                clean_actions.append(f"🔄 {who} saca a {mon} (HP: {hp})")
                if slot.startswith(user_p) and mon not in my_leads and len(my_leads) < 2: my_leads.append(mon)
                elif slot.startswith(opp_p) and mon not in opp_leads and len(opp_leads) < 2: opp_leads.append(mon)
            elif cmd in ["detailschange", "-mega"] and "Mega" in line:
                slot = parts[2] if len(parts) > 2 else ""
                mega_mon = ""
                for part in parts:
                    if "Mega" in part: mega_mon = part.split(",")[0].strip()
                if mega_mon:
                    who = get_owner(slot)
                    clean_actions.append(f"✨ {who} MEGAEVOLUCIÓN a {mega_mon}")
                    if slot.startswith(user_p):
                        if mega_mon not in my_megas: my_megas.append(mega_mon)
                    else:
                        if mega_mon not in opp_megas: opp_megas.append(mega_mon)
            elif cmd == "move" and len(parts) > 3:
                slot, move = parts[2], parts[3]
                target = parts[4] if len(parts) > 4 else ""
                who = get_owner(slot)
                clean_actions.append(f"⚔️ {who} usa {move}" + (f" -> objetivo {target}" if target else ""))
                if move in ["Tailwind", "Trick Room", "Rain Dance", "Sunny Day"]:
                    turn_logs.append(f"🌪️ <b>[T{current_turn}]</b> {who} usó <b>{move}</b>.")
            elif cmd == "-damage" and len(parts) > 3:
                target, hp = parts[2], parts[3]
                clean_actions.append(f"  └─> Daño a {target}: Queda a {hp}")
            elif cmd == "-heal" and len(parts) > 3:
                target, hp = parts[2], parts[3]
                clean_actions.append(f"  └─> Curación a {target}: Queda a {hp}")
            elif cmd == "-ability" and len(parts) > 3:
                target, ability = parts[2], parts[3]
                clean_actions.append(f"  └─> Habilidad activada en {target}: {ability}")
            elif cmd == "-weather" and len(parts) > 2:
                weather = parts[2]
                clean_actions.append(f"  └─> Clima activo: {weather}")
            elif cmd == "-fieldstart" and len(parts) > 2:
                field = parts[2]
                clean_actions.append(f"  └─> Campo activo: {field}")
            elif cmd == "-boost" and len(parts) > 4:
                target, stat, amt = parts[2], parts[3], parts[4]
                clean_actions.append(f"  └─> {target} sube {stat} (+{amt})")
            elif cmd == "-unboost" and len(parts) > 4:
                target, stat, amt = parts[2], parts[3], parts[4]
                clean_actions.append(f"  └─> {target} baja {stat} (-{amt})")
            elif cmd == "faint" and len(parts) > 2:
                fainted_mon = parts[2].split(":")[1].strip() if ":" in parts[2] else parts[2]
                who_lost = get_owner(parts[2])
                clean_actions.append(f"💀 KO: {who_lost} pierde a {fainted_mon}")
                
                if first_ko is None:
                    first_ko = f"T{current_turn} ({fainted_mon})"
                
                if parts[2].startswith(user_p):
                    turn_logs.append(f"💀 <b>[T{current_turn}] KO:</b> Tu <b>{fainted_mon}</b> cayó.")
                else:
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

        full_actions_str = "\n".join(clean_actions)
        ai_report = analyze_with_ai(full_actions_str, target_user, opponent_name, user_won, my_leads
