import os
import re
import sqlite3
import requests
import psycopg2
from flask import Flask, render_template, request, redirect, url_for

app = Flask(__name__)
DEFAULT_USER = "polilla02"
DB_FILE = 'vgc_data.db'

# --- MOTOR HÍBRIDO DE BASE DE DATOS (POSTGRES / SQLITE) ---
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

# --- MOTOR CON RIGOR TÁCTICO Y PROMPT ANTI-HALLUCINATION ---
def analyze_with_ai(clean_actions_text, user_name, opponent_name, user_won, my_leads, opp_leads, my_team, opp_team, archetype):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key: return None
        
    url = "https://api.openai.com/v1/chat/completions"
    color = "#22c55e" if user_won else "#ef4444"
    resultado = "GANÓ" if user_won else "PERDIÓ"
    
    system_prompt = f"""
    ERES: Coach Táctico de Élite de Pokémon VGC (Nivel Campeonato Mundial). Tu análisis debe ser 100% RIGUROSO y FIEL al log proporcionado.
    FORMATO METAGAME: VGC con Megaevoluciones activas (NO existe la Teracristalización).

    REGLAS ESTRICTAS DE AUDITORÍA (PROHIBIDO ALUCINAR):
    1. Basarás tus afirmaciones EXCLUSIVAMENTE en los movimientos, turnos y eventos reales registrados en el log.
    2. JAMÁS inventes nombres de movimientos que no aparezcan en el registro (ej. si usó Volt Switch o Thunderbolt, NO lo llames Zap Cannon).
    3. Si el arquetipo del rival o del juego es '{archetype}', NO afirmes que el equipo carece de dicha herramienta si la activó durante el combate.
    4. Analiza la interacción real de habilidades (Intimidación, Polvo Ira) y cambios de campo/clima en los turnos 1 y 2.
    5. Usa lenguaje y jerga profesional de VGC (Win Condition, Speed Control, Pivote, Damage Calc, Pressure, Matchup).
    """
    
    user_prompt = f"""
    AUDITORÍA DE COMBATE COMPLETO:

    JUGADORES Y RESULTADO:
    - Jugador Principal: '{user_name}' ({resultado} el combate)
    - Rival: '{opponent_name}'

    EQUIPOS Y ALINEACIÓN INICIAL:
    - Equipo de {user_name}: {', '.join(my_team)}
    - Leads de {user_name}: {', '.join(my_leads)}
    - Equipo del Rival ({opponent_name}): {', '.join(opp_team)}
    - Leads del Rival: {', '.join(opp_leads)}

    LOG TURNO A TURNO DEL COMBATE:
    {clean_actions_text}

    GENERA LA AUDITORÍA EN HTML STRICTO (Sin sintaxis Markdown ```` html o asteriscos):
    <div style='border-bottom: 2px solid {color}; padding-bottom: 6px; margin-bottom: 12px;'>
        <b style='color: {color}; font-size: 1.15em;'>🤖 COACH IA: AUDITORÍA TÁCTICA DE NIVEL MUNDIAL</b>
    </div>
    <p>📌 <b>1. Team Preview y Mega-Fit:</b><br>[Analiza la elección de la Mega y la sinergia de los leads frente a los 6 del rival]</p>
    <p>⏱️ <b>2. Control del Ritmo y Speed Control:</b><br>[Evalúa el control de velocidad: Viento Afín, Espacio Raro, Sorpresa, Cambios de Base Speed o prioridades]</p>
    <p>📉 <b>3. Punto de Inflexión y KOs Clave:</b><br>[Indica el turno exacto (ej. Turno 2) en que la partida se decantó y por qué cayó el primer Pokémon]</p>
    <p>🎯 <b>4. Plan de Ajuste Táctico para el Game 2:</b><br>[Instrucción concreta para la siguiente partida: ajuste de leads, timing de Megaevolución o jugadas defensivas]</p>
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
        "temperature": 0.1
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

# --- PARSER INTEGRAL DE SHOWDOWN REPLAYS ---
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
            norm_pname
