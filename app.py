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
        facts.append("⚠️ EVENTO DE CAMPO REAL: Espacio Raro (Trick Room) FUE ACTIVADO. Mientras esté activo, los Pokémon más lentos atacan primero.")
    else:
        facts.append("ℹ️ CAMPO DE VELOCIDAD: NO se activó Espacio Raro (Trick Room) en ningún turno.")

    if tailwind_used:
        facts.append("🌪️ EVENTO DE VELOCIDAD: Se activó Viento Afín (Tailwind), duplicando (2x) la velocidad.")

    if weather_active:
        if "sandstorm" in weather_active:
            facts.append("🏜️ CLIMA: Tormenta de Arena (Sandstorm) activa.")
        elif "rain" in weather_active or "raindance" in weather_active:
            facts.append("🌧️ CLIMA: Lluvia (Rain) activa.")
        elif "sun" in weather_active or "sunnyday" in weather_active:
            facts.append("☀️ CLIMA: Sol (Sun) activo.")

    if intimidate_used:
        facts.append("📉 HABILIDAD: Intimidación registrada (-1 Ataque físico). Habilidades como Competitivo o Tenacidad reciben +2 en stats.")

    facts.append("🛡️ INMUNIDADES TIPOS (0x): Tierra inmune a Eléctrico; Volador a Tierra; Hada a Dragón; Acero a Veneno; Fantasma a Normal/Lucha; Siniestro a Psíquico.")

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

    facts_str = "\n".join([f"- {f}" for f in mechanic_facts]) if mechanic_facts else "- No hay hechos de campo."

    system_prompt = (
        "Eres un Coach Experto en el Mundial de Pokémon VGC (Video Game Championships). Tu objetivo es analizar "
        "partidas de jugadores para ayudarles a mejorar su estrategia, toma de decisiones y construcción de equipos.\n\n"
        "instrucciones:\n"
        "Analiza el registro de batalla basándote estrictamente en las mecánicas de VGC. Piensa paso a paso (Chain of Thought): "
        "primero identifica las 'win conditions' de ambos equipos a partir de los leads y backs, luego evalúa las sinergias "
        "y finalmente desglosa cada turno.\n"
        "Si en el registro falta información sobre algún turno o movimiento, responde: 'Información no detallada en el registro' "
        "y no inventes jugadas ni asumas acciones que no estén en el texto.\n\n"
        "mecanicas_a_evaluar:\n"
        "1. Megaevoluciones presentes.\n"
        "2. Tabla de tipos (ventajas, debilidades e inmunidades).\n"
        "3. Puntos de estadística (Stats points), límite 66 por Pokémon.\n"
        "4. Naturalezas de los Pokémon.\n"
        "5. Climas activos (Sol, Lluvia, Arena, Nieve, Climas Extremos).\n"
        "6. Campos activos (Eléctrico, Hierba, Psíquico, Niebla).\n"
        "7. Prioridad de ataques.\n"
        "8. Variaciones de stats (buffs/debuffs).\n"
        "9. Habilidades e Objetos.\n"
        "10. Sinergias de tipos (Planta-Fuego-Agua, Hada-Dragón-Acero, BoltBeam, etc.).\n"
        "11. Lectura de Leads y Backs para deducir Win Conditions.\n\n"
        "formato_de_salida:\n"
        "Formatea tu respuesta en HTML limpio (sin envoltorio markdown de triples comillas) con la siguiente estructura:\n\n"
        f"<div style='border-bottom: 2px solid {color}; padding-bottom: 6px; margin-bottom: 12px;'>\n"
        f"    <b style='color: {color}; font-size: 1.15em;'>🏆 COACH IA: AUDITORÍA MUNDIAL DE VGC</b>\n"
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
        mechanic_facts = get_deterministic_facts(log)
        
        ai_report = analyze_with_ai(full_actions_str, target_user, opponent_name, user_won, my_leads, opp_leads, my_team, opp_team, archetype, mechanic_facts)
        
        if ai_report: coach_report_str = ai_report
        else: coach_report_str = generate_heuristic_report(user_won, my_leads, opp_leads, archetype)
        
        if turn_logs:
            turn_by_turn_html = f"<details style='margin-top:16px; cursor:pointer; background: var(--inner-bg); padding: 10px; border-radius: 8px; border: 1px solid var(--border-color);'><summary style='font-weight:900; color:var(--poke-cyan);'>📑 Ver Log Resumido de Eventos Clave</summary><div style='font-size:0.88em; margin-top:10px; color:var(--text-main); line-height: 1.6;'>" + "<br>".join(turn_logs) + "</div></details>"
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
    conn, db_type = get_db()
    cursor = conn.cursor()
    placeholder = "%s" if db_type == "postgres" else "?"
    
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
        opp_display = opp.strip() if (opp and opp.strip()) else "Rival Showdown"
        
        cursor.execute(f"SELECT game_num, team_name, my_lead, my_back, opp_lead, opp_back, result, my_mega, opp_mega, archetype, turns, replay_url, tactical_summary, coach_report FROM games WHERE series_id = {placeholder} ORDER BY game_num ASC", (s_id,))
        games = cursor.fetchall()
        g_wins = sum(1 for g in games if g[6] == 'Victoria')
        g_losses = sum(1 for g in games if g[6] == 'Derrota')
        if g_wins >= 2: calc_result = "Victoria (BO3)"
        elif g_losses >= 2: calc_result = "Derrota (BO3)"
        else: calc_result = f"En curso ({g_wins}-{g_losses})"
        if calc_result == "Victoria (BO3)": total_series_wins += 1
        
        date_str = date.strftime('%Y-%m-%d') if hasattr(date, 'strftime') else str(date)[:10]
        series_list.append({"id": s_id, "opponent": opp_display, "result": calc_result, "misplay": misplay, "notes": notes, "date": date_str, "games": games})
    
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
    
    db_status = "☁️ BASE DE DATOS POSTGRESQL (NEON)" if db_type == "postgres" else "💾 BASE DE DATOS LOCAL (SQLITE)"
    coach_advice = [f"ESTADO DE LA BASE DE DATOS: {db_status}"]
    conn.close()
    return render_template('dashboard.html', user_teams=user_teams, series_list=series_list, series_winrate=series_winrate, total_series_count=total_series_count, total_series_wins=total_series_wins, lead_stats=lead_stats, misplay_stats=misplay_stats, team_performance=team_performance, archetype_stats=archetype_stats, mega_stats=mega_stats, total_cp=total_cp, cp_pct=cp_pct, coach_advice="<br><br>".join(coach_advice), default_user=DEFAULT_USER)

@app.route('/add_team', methods=['GET', 'POST'])
def add_team():
    if request.method == 'GET': return redirect(url_for('index'))
    team_name = request.form.get('team_name')
    pokepaste_url = request.form.get('pokepaste_url', '')
    notes = request.form.get('notes', '')
    raw_paste = fetch_pokepaste(pokepaste_url)
    parsed_mons = parse_showdown_team(raw_paste)
    pokemon_list = ", ".join([mon['name'] for mon in parsed_mons])
    if not pokemon_list: pokemon_list = "⚠️ Error leyendo Paste."
    if team_name:
        conn, db_type = get_db()
        cursor = conn.cursor()
        if db_type == "postgres":
            cursor.execute("""
                INSERT INTO user_teams (team_name, pokemon_list, pokepaste_url, notes, raw_paste) 
                VALUES (%s, %s, %s, %s, %s) 
                ON CONFLICT (team_name) DO UPDATE SET 
                pokemon_list = EXCLUDED.pokemon_list, 
                pokepaste_url = EXCLUDED.pokepaste_url, 
                notes = EXCLUDED.notes, 
                raw_paste = EXCLUDED.raw_paste
            """, (team_name, pokemon_list, pokepaste_url, notes, raw_paste))
        else:
            cursor.execute("INSERT OR REPLACE INTO user_teams (team_name, pokemon_list, pokepaste_url, notes, raw_paste) VALUES (?, ?, ?, ?, ?)", 
                           (team_name, pokemon_list, pokepaste_url, notes, raw_paste))
        conn.commit()
        conn.close()
    return redirect(url_for('index'))

@app.route('/parse_replay', methods=['GET', 'POST'])
def parse_replay_route():
    if request.method == 'GET': return redirect(url_for('index'))
    url = request.form.get('replay_url')
    user_name = request.form.get('user_name') or DEFAULT_USER
    series_id = request.form.get('series_id')
    team_name = request.form.get('team_name') or 'Equipo Principal Polilla'
    parsed = parse_showdown_replay(url, user_name)
    if parsed:
        conn, db_type = get_db()
        cursor = conn.cursor()
        placeholder = "%s" if db_type == "postgres" else "?"
        opp_name_clean = parsed['opponent'].strip() if parsed['opponent'] and parsed['opponent'].strip() else "Rival Showdown"
        
        if not series_id or series_id == "new":
            if db_type == "postgres":
                cursor.execute("INSERT INTO series_matches (opponent, result) VALUES (%s, %s) RETURNING id", (opp_name_clean, 'En cursor'))
                series_id = cursor.fetchone()[0]
            else:
                cursor.execute("INSERT INTO series_matches (opponent, result) VALUES (?, ?)", (opp_name_clean, 'En curso'))
                series_id = cursor.lastrowid
                
        cursor.execute(f"SELECT COUNT(*) FROM games WHERE series_id = {placeholder}", (series_id,))
        game_num = cursor.fetchone()[0] + 1
        
        cursor.execute(f'''INSERT INTO games (series_id, game_num, team_name, my_lead, my_back, opp_lead, opp_back, result, my_mega, opp_mega, archetype, turns, replay_url, tactical_summary, coach_report)
            VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})''', 
            (series_id, game_num, team_name, parsed['my_lead'], parsed['my_back'], parsed['opp_lead'], parsed['opp_back'], parsed['result'], parsed['my_mega'], parsed['opp_mega'], parsed['archetype'], parsed['turns'], parsed['replay_url'], parsed['tactical_summary'], parsed['coach_report']))
        
        cursor.execute(f"SELECT result FROM games WHERE series_id = {placeholder}", (series_id,))
        results = [r[0] for r in cursor.fetchall()]
        wins, losses = results.count('Victoria'), results.count('Derrota')
        if wins >= 2: cursor.execute(f"UPDATE series_matches SET result = 'Victoria (BO3)' WHERE id = {placeholder}", (series_id,))
        elif losses >= 2: cursor.execute(f"UPDATE series_matches SET result = 'Derrota (BO3)' WHERE id = {placeholder}", (series_id,))
        conn.commit()
        conn.close()
    return redirect(url_for('index'))

@app.route('/update_misplay', methods=['GET', 'POST'])
def update_misplay():
    if request.method == 'GET': return redirect(url_for('index'))
    series_id = request.form.get('series_id')
    reason = request.form.get('reason')
    notes = request.form.get('notes', '')
    conn, db_type = get_db()
    cursor = conn.cursor()
    placeholder = "%s" if db_type == "postgres" else "?"
    cursor.execute(f"UPDATE series_matches SET misplay_reason = {placeholder}, notes = {placeholder} WHERE id = {placeholder}", (reason, notes, series_id))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/delete_series', methods=['GET', 'POST'])
def delete_series():
    if request.method == 'GET': return redirect(url_for('index'))
    series_id = request.form.get('series_id')
    conn, db_type = get_db()
    cursor = conn.cursor()
    placeholder = "%s" if db_type == "postgres" else "?"
    cursor.execute(f"DELETE FROM games WHERE series_id = {placeholder}", (series_id,))
    cursor.execute(f"DELETE FROM series_matches WHERE id = {placeholder}", (series_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/delete_team', methods=['GET', 'POST'])
def delete_team():
    if request.method == 'GET': return redirect(url_for('index'))
    team_id = request.form.get('team_id')
    if team_id:
        conn, db_type = get_db()
        cursor = conn.cursor()
        placeholder = "%s" if db_type == "postgres" else "?"
        cursor.execute(f"DELETE FROM user_teams WHERE id = {placeholder}", (team_id,))
        conn.commit()
        conn.close()
    return redirect(url_for('index'))

@app.route('/add_cp', methods=['GET', 'POST'])
def add_cp():
    if request.method == 'GET': return redirect(url_for('index'))
    name = request.form.get('name') or 'Torneo VGC'
    cp = int(request.form.get('cp') or 0)
    if cp > 0:
        conn, db_type = get_db()
        cursor = conn.cursor()
        placeholder = "%s" if db_type == "postgres" else "?"
        cursor.execute(f"INSERT INTO tournaments (name, cp) VALUES ({placeholder}, {placeholder})", (name, cp))
        conn.commit()
        conn.close()
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
