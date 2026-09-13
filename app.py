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
    elif "drizzle" in log_lower or "rain dance" in log_lower or any(p in team_str for p in ["kyogre", "pelipper", "basculegion", "basculegion-m"]):
        return "Rain Weather"
    elif "drought" in log_lower or "sunny day" in log_lower or any(p in team_str for p in ["groudon", "koraidon", "torkoal", "flutter mane"]):
        return "Sun Weather"
    elif any(p in team_str for p in ["rillaboom"]) and any(p in team_str for p in ["sneasler"]):
        return "Grassy Terrain Offense (Rillaboom + Sneasler)"
    elif any(p in team_str for p in ["chi-yu", "flutter mane", "urshifu", "chien-pao", "iron bundle"]):
        return "Hyper Offense"
    else:
        return "Balance / Positional"

def get_deterministic_facts(log):
    facts = []
    tr_used, tailwind_used, weather_active, intimidate_used = False, False, None, False
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
            if parts[3] == "intimidate": intimidate_used = True

    if tr_used:
        facts.append("⚠️ EVENTO DE CAMPO REAL: Espacio Raro (Trick Room) FUE ACTIVADO. Mientras esté activo, los Pokémon más lentos atacan primero (a igual prioridad).")
    else:
        facts.append("ℹ️ CAMPO DE VELOCIDAD: NO se activó Espacio Raro (Trick Room) en ningún turno.")

    if tailwind_used:
        facts.append("🌪️ EVENTO DE VELOCIDAD: Se activó Viento Afín (Tailwind), duplicando (2x) la velocidad de ese equipo.")

    if weather_active:
        if "sandstorm" in weather_active: facts.append("🏜️ CLIMA: Tormenta de Arena (Sandstorm) activa.")
        elif "rain" in weather_active or "raindance" in weather_active: facts.append("🌧️ CLIMA: Lluvia (Rain) activa.")
        elif "sun" in weather_active or "sunnyday" in weather_active: facts.append("☀️ CLIMA: Sol (Sun) activo.")

    if intimidate_used:
        facts.append("📉 HABILIDAD: Intimidación registrada (-1 Ataque físico). Habilidades como Competitivo o Tenacidad reciben +2 en stats.")

    facts.append("⚡ MECÁNICA DE PRIORIDAD ABSOLUTA: Movimientos de alta prioridad (Protección +4, Sorpresa/Fake Out +3, Polvo Ira/Señuelo +2, Velocidad Extrema +2, Golpes Prioritarios +1) SIEMPRE atacan antes que movimientos de prioridad 0, sin importar la velocidad ni Viento Afín.")
    facts.append("🛡️ INMUNIDADES Y DEBILIDADES CLAVE: Tierra es 100% INMUNE a Eléctrico; Agua es DÉBIL (2x) a Eléctrico; Volador es DÉBIL (2x) a Eléctrico; Hada es inmune a Dragón.")
    return facts

def analyze_with_ai(clean_actions_text, user_name, opponent_name, user_won, my_leads, opp_leads, my_team, opp_team, archetype, mechanic_facts):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key: return None
    url = "https://api.openai.com/v1/chat/completions"
    color = "#22c55e" if user_won else "#ef4444"
    resultado = "GANÓ" if user_won else "PERDIÓ"
    my_team_str, my_leads_str = ", ".join(my_team), ", ".join(my_leads)
    opp_team_str, opp_leads_str = ", ".join(opp_team), ", ".join(opp_leads)
    facts_str = "\n".join([f"- {f}" for f in mechanic_facts]) if mechanic_facts else "- No hay hechos de campo."

    system_prompt = (
        "Eres un Coach Experto en Pokémon VGC, analizando el formato custom 'Pokémon Champions 2026 - Regulación M-C'. "
        "Tu objetivo es ayudar al usuario a mejorar su estrategia, toma de decisiones y comprensión de este metagame específico.\n\n"
        "instrucciones:\n"
        "Analiza el registro de batalla basándote strictly en las mecánicas de VGC. Piensa paso a paso (Chain of Thought): "
        "primero identifica las 'win conditions' de ambos equipos, luego evalúa las sinergias (meta M-C) y desglosa cada turno.\n"
        "Si falta información, responde: 'Información no detallada en el registro' y no inventes jugadas.\n\n"
        "REGLAS CRÍTICAS DE EVALUACIÓN (FORMATO M-C):\n"
        "1. RECONOCIMIENTO DE TEMPO Y KOS: Si el usuario consigue un KO o Doble KO, evalúalo como un ÉXITO CRÍTICO. "
        "   NO sugieras curar o proteger cuando el usuario gana la ventaja de turno (tempo) logrando KOs.\n"
        "2. BUFFS Y STATS: Si Competitivo (Competitive) o Tenacidad (Defiant) se activan (ej. por Intimidación), "
        "   relaciona el daño posterior con ese +2 en ataque correspondiente.\n"
        "3. MEGAEVOLUCIONES CUSTOM (VITAL): ESTE FORMATO PERMITE MEGAS NO OFICIALES. Si el log indica Megaevoluciones como "
        "   'Raichu-Mega-Y', 'Staraptor-Mega', 'Scovillain-Mega', 'Floette-Eternal-Mega', 'Golisopod-Mega', acéptalas como VÁLIDAS. "
        "   JAMÁS digas que son errores o alucinaciones.\n"
        "4. RESPETO AL RESULTADO REAL: Si el prompt indica que el usuario GANÓ, asume la victoria. NUNCA digas que perdió.\n"
        "5. LÓGICA DE POSICIÓN: NUNCA sugieras 'cambiar a X' si el Pokémon X ya está combatiendo en el campo.\n"
        "6. TABLA DE TIPOS: No recomiendes dejar a un Pokémon tipo Agua o Volador frente a atacantes Eléctricos. Valora las inmunidades (Tierra vs Eléctrico).\n"
        "7. PRIORIDAD vs VELOCIDAD: Sorpresa (+3), Protección (+4) y Polvo Ira (+2) actúan siempre antes que movimientos de prioridad 0, sin importar Velocidad o Viento Afín. Nunca recrimines a un jugador por ser 'más lento' si el rival usó un ataque de mayor prioridad.\n\n"
        "mecanicas_a_evaluar:\n"
        "1. Prioridad de ataques (+4 a -6) y Speed Tiers.\n"
        "2. Megaevoluciones (oficiales y custom Reg M-C).\n"
        "3. Tabla de tipos y sinergias (ej. Grassy Terrain Rillaboom + Unburden Sneasler, Rain Pelipper + Basculegion).\n"
        "4. Puntos de estadística, climas y campos activos.\n\n"
        "formato_de_salida:\n"
        "Formatea tu respuesta en HTML limpio (sin envoltorio markdown de triples comillas) con la siguiente estructura:\n\n"
        f"<div style='border-bottom: 2px solid {color}; padding-bottom: 6px; margin-bottom: 12px;'>\n"
        f"    <b style='color: {color}; font-size: 1.15em;'>🏆 COACH IA: AUDITORÍA CHAMPIONS VGC (Reg M-C)</b>\n"
        f"</div>\n"
        "<h3>1. DESCRIPCIÓN TÁCTICA:</h3>\n"
        "<p>- <b>Análisis de Equipos (Leads/Backs):</b> [Detalles...]</p>\n"
        "<p>- <b>Sinergias, Climas y Campos:</b> [Detalles...]</p>\n"
        "<p>- <b>Win Conditions:</b> [Detalles...]</p>\n"
        "<h3>2. EXPLICACIÓN TURNO A TURNO:</h3>\n"
        "<div style='background: rgba(255,255,255,0.03); padding: 10px; border-radius: 6px; margin-bottom: 10px;'>\n"
        "    <b>Turno X:</b><br>\n"
        "    ✅ <b>Aciertos:</b> [Aciertos]<br>\n"
        "    ❌ <b>Errores:</b> [Errores]<br>\n"
        "    🎯 <b>El Turno Perfecto:</b> [Jugada óptima]\n"
        "</div>\n"
        "<h3>3. CONCLUSIÓN Y PUNTOS DE MEJORA:</h3>\n"
        "<p>- <b>Resumen General:</b> [Resumen]</p>\n"
        "<p>- <b>Consejos Tácticos:</b> [Consejos]</p>"
    )

    user_prompt = (
        f"AUDITORÍA REGISTRADA:\n"
        f"Usuario: '{user_name}' ({resultado})\n"
        f"Rival: '{opponent_name}'\n"
        f"Arquetipo: {archetype}\n\n"
        f"EQUIPOS:\n"
        f"- Equipo de {user_name}: {my_team_str} (Leads: {my_leads_str})\n"
        f"- Equipo de {opponent_name}: {opp_team_str} (Leads: {opp_leads_str})\n\n"
        f"HECHOS VERIFICADOS:\n{facts_str}\n\n"
        f"LOG COMPLETO DE COMBATE:\n{clean_actions_text}"
    )

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": "gpt-4o-mini", "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}], "temperature": 0.0}
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        if response.status_code == 200:
            data = response.json()
            report = data['choices'][0]['message']['content'].strip()
            report = re.sub(r'^```html\s*', '', report, flags=re.IGNORECASE)
            report = re.sub(r'^```\s*', '', report)
            report = re.sub(r'\s*```$', '', report)
            return report
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
                p_slot, p_name = parts[2].strip(), parts[3].strip()
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
        
        # DETECTOR MEJORADO DE GANADOR
        winner_name = data.get("winner", "")
        for line in log.split("\n"):
            parts = line.split("|")
            if len(parts) > 2 and parts[1] == "win":
                winner_name = parts[2].strip()

        user_won = False
        if winner_name and user_p in players:
            p_real_name = players[user_p]
            norm_winner = "".join(e for e in winner_name.lower() if e.isalnum())
            norm_player = "".join(e for e in p_real_name.lower() if e.isalnum())
            if norm_player and (norm_player in norm_winner or norm_winner in norm_player):
                user_won = True

        my_team, opp_team, my_leads, opp_leads, my_megas, opp_megas = [], [], [], [], [], []
        turns, first_ko, current_turn = 0, None, 0
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
                    clean_actions.append(f"✨ {who} MEGAEVOLUCIONA a {mega_mon}")
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
                if first_ko is None: first_ko = f"T{current_turn} ({fainted_mon})"
                if parts[2].startswith(user_p): turn_logs.append(f"💀 <b>[T{current_turn}] KO:</b> Tu <b>{fainted_mon}</b> cayó.")
                else: turn_logs.append(f"💥 <b>[T{current_turn}] KO:</b> Rival <b>{fainted_mon}</b> cayó.")

        my_backs = [m for m in my_team if m not in my_leads][:2]
        opp_backs = [m for m in opp_team if m not in opp_leads][:2]
        archetype = detect_archetype(log, opp_team)
        my_mega_str = " / ".join(set(my_megas)) if my_megas else "Ninguna"
        opp_mega_str = " / ".join(set(opp_megas)) if opp_megas else "Ninguna"
        tactical_