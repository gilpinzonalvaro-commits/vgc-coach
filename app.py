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
    if "|move|" in log_lower and "|trick room|" in log_lower:
        return "Trick Room"
    elif any(p in team_str for p in ["indeedee", "ursaluna", "calyrex-ice", "farigiraf", "torkoal", "dusclops", "sinistcha"]) and "trick room" in log_lower:
        return "Trick Room"
    elif "tailwind" in log_lower or any(p in team_str for p in ["whimsicott", "tornadus", "talonflame", "roaring moon"]):
        return "Tailwind / Speed Control"
    elif "drizzle" in log_lower or "rain dance" in log_lower or any(p in team_str for p in ["kyogre", "pelipper", "urshifu-rapid-strike"]):
        return "Rain Weather"
    elif "drought" in log_lower or "sunny day" in log_lower or any(p in team_str for p in ["groudon", "koraidon", "torkoal", "flutter mane"]):
        return "Sun Weather"
    elif any(p in team_str for p in ["chi-yu", "flutter mane", "urshifu", "chien-pao", "iron bundle"]):
        return "Hyper Offense"
    else:
        return "Balance / Positional"

def get_deterministic_facts(log):
    facts = []
    tr_used = False
    tailwind_used = False
    weather_active = None
    intimidate_used = False

    for line in log.split('\n'):
        parts = [p.strip().lower() for p in line.split('|')]
        if len(parts) < 2: continue
        cmd = parts[1]

        if cmd == "move" and len(parts) > 3:
            move_name = parts[3]
            if move_name == "trick room": tr_used = True
            elif move_name == "tailwind": tailwind_used = True
        elif cmd == "-weather" and len(parts) > 2:
            w = parts[2]
            if w != "none": weather_active = w
        elif cmd == "-ability" and len(parts) > 3:
            ability = parts[3]
            if ability == "intimidate": intimidate_used = True

    if tr_used:
        facts.append("⚠️ EVENTO DE CAMPO REAL: Espacio Raro (Trick Room) FUE ACTIVADO en la partida. Mientras esté activo, los Pokémon con menor velocidad atacan primero.")
    else:
        facts.append("ℹ️ CAMPO DE VELOCIDAD: NO se activó Espacio Raro (Trick Room) en ningún turno de esta partida.")

    if tailwind_used:
        facts.append("🌪️ EVENTO DE VELOCIDAD: Se activó Viento Afín (Tailwind), duplicando (2x) la velocidad del equipo atacante.")

    if weather_active:
        if "sandstorm" in weather_active:
            facts.append("🏜️ CLIMA REGISTRADO: Tormenta de Arena (Sandstorm) activa. Habilidades como Ímpetu Arena duplican velocidad (2x) y la Def. Sp. de los tipo Roca aumenta +50%.")
        elif "rain" in weather_active or "raindance" in weather_active:
            facts.append("🌧️ CLIMA REGISTRADO: Lluvia (Rain) activa. Habilidades como Nado Rápido duplican velocidad (2x). Potencia Agua +50%, Fuego -50%.")
        elif "sun" in weather_active or "sunnyday" in weather_active:
            facts.append("☀️ CLIMA REGISTRADO: Sol (Sun) activo. Habilidades como Clorofila duplican velocidad (2x). Potencia Fuego +50%, Agua -50%.")

    if intimidate_used:
        facts.append("📉 EVENTO DE HABILIDAD: Intimidación entró en juego (-1 Ataque físico). IMPORTANTE: Si el jugador tiene Pokémon con Competitivo o Tenacidad, les otorga +2 en sus estadísticas (Ataque Especial / Ataque).")

    facts.append("🛡️ INMUNIDADES SAGRADAS DE TIPOS (0x): Tierra es 100% INMUNE a Eléctrico; Volador a Tierra; Hada a Dragón; Acero a Veneno; Fantasma a Normal y Lucha; Siniestro a Psíquico.")

    return facts

def analyze_with_ai(clean_actions_text, user_name, opponent_name, user_won, my_leads, opp_leads, my_team, opp_team, archetype, mechanic_facts):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key: return None
        
    url = "https://api.openai.com/v1/chat/completions"
    color = "#22c55e" if user_won else "#ef4444"
    resultado = "GANÓ" if user_won else "PERDIÓ"
    
    my_team_str = ", ".join(my_team)
    my_leads_str = ", ".join(my_leads)
    opp_team_str = ", ".join(opp_team)
    opp_leads_str = ", ".join(opp_leads)

    facts_str = "\n".join([f"- {f}" for f in mechanic_facts]) if mechanic_facts else "- No se detectaron estados alterados de campo."

    system_prompt = f"""Eres un Coach Experto en el Mundial de Pokémon VGC (Video Game Championships). Tu objetivo es analizar partidas de jugadores para ayudarles a mejorar su estrategia, toma de decisiones y construcción de equipos.

<instrucciones>
Analiza el registro de batalla proporcionado basándote strictly en las mecánicas de VGC. Piensa paso a paso (Chain of Thought): primero identifica las "win conditions" de ambos equipos a partir de los leads y backs, luego evalúa las sinergias y finalmente desglosa cada turno. 
Si en el registro falta información sobre algún turno o movimiento, responde: "Información no detallada en el registro" y no inventes jugadas ni asumas acciones que no estén en el texto.
</instrucciones>

<mecanicas_a_evaluar>
Debes hacer un cruce lógico exhaustivo de los siguientes 11 puntos en tu análisis:
1. Megaevoluciones presentes.
2. Tabla de tipos (ventajas, debilidades e inmunidades).
3. Puntos de estadística (Stats points), teniendo en cuenta el límite máximo de 66 por Pokémon (según Statcrusher).
4. Naturalezas de los Pokémon y cómo potencian o merman sus estadísticas.
5. Climas activos: Sol, Lluvia, Tormenta de arena, Nieve, y climas extremos (Sol abrasador, Mar del albor/origen, Turbulencias).
6. Campos activos: Eléctrico, Hierba, Psíquico y Niebla.
7. Prioridad de los ataques utilizados.
8. Ataques que provoquen variaciones en los puntos de estadística (buffs/debuffs).
9. Interacción de Habilidades y Objetos equipados.
10. Sinergia de tipos en el campo: Planta-Fuego-Agua, Hada-Dragón-Acero, Acero-Volador, Fantasma-Normal, Agua-Tierra, Eléctrico-Hielo (BoltBeam), Fantasma-Lucha, Roca-Tierra.
11. Lectura de Leads (abridores) y Backs (reservas) para deducir la Win Condition propia y la del rival.
</mecanicas_a_evaluar>

<formato_de_salida>
Tu respuesta DEBE formatearse exclusivamente en código HTML estricto (sin etiquetas markdown ```html) siguiendo esta estructura:

<div style='border-bottom: 2px solid {color}; padding-bottom: 6px; margin-bottom: 12px;'>
    <b style='color: {color}; font-size: 1.15em;'>🏆 COACH IA: AUDITORÍA MUNDIAL DE VGC</b>
</div>

<h3>1. DESCRIPCIÓN TÁCTICA:</h3>
<p>- <b>Análisis de Equipos (Leads/Backs):</b> [Análisis del equipo del usuario y del rival basándose en Leads/Backs]</p>
<p>- <b>Sinergias, Climas y Campos:</b> [Identificación de Sinergias (punto 10) y manejo de Climas/Campos (puntos 5 y 6)]</p>
<p>- <b>Win Conditions:</b> [Definición clara de la Win Condition de ambos jugadores]</p>

<h3>2. EXPLICACIÓN TURNO A TURNO:</h3>
<p><i>Para cada turno registrado en el log debes detallar:</i></p>
<div style='background: rgba(255,255,255,0.03); padding: 10px; border-radius: 6px; margin-bottom: 10px;'>
    <b>Turno X:</b><br>
    ✅ <b>Aciertos:</b> [Qué se hizo bien]<br>
    ❌ <b>Errores:</b> [Qué falló o se leyó mal]<br>
    🎯 <b>El Turno Perfecto:</b> [Describe la jugada óptima considerando todas las mecánicas evaluables]
</div>

<h3>3. CONCLUSIÓN Y PUNTOS DE MEJORA:</h3>
<p>- <b>Resumen General:</b> [Resumen del desempeño general]</p>
<p>- <b>Consejos Tácticos:</b> [Áreas específicas a cubrir y consejos de mejora táctica para futuras partidas]</p>
</formato_de_salida>"""

    user_prompt = f"""AUDITORÍA REGISTRADA PARA ANÁLISIS:

DATOS GENERALES:
- Usuario: '{user_name}' ({resultado} la partida)
- Rival: '{opponent_name}'
- Arquetipo General: {archetype}

EQUIPOS Y ALINEACIONES:
- Equipo de {user_name}: {my_team_str}
  * Leads elegidos: {my_leads_str}
- Equipo de {opponent_name}: {opp_team_str}
  * Leads elegidos: {opp_leads_str}

HECHOS VERIFICADOS DE CAMPO Y MECÁNICAS (OBLIGATORIOS):
{facts_str}

REGISTRO COMPLETO TURNO A TURNO DEL COMBATE:
{clean_actions_text}

Por favor, genera el Análisis del Coach siguiendo las instrucciones y el formato de salida requerido."""

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
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        if response.status_code == 200:
            data = response.json()
            report = data['choices'][0]['message']['content'].strip()
            report = re.sub(r'^```html\s*', '', report, flags=re.IGNORECASE)
            report = re.sub(r'^```\s*', '', report)
            report = re.sub(r'\s*
