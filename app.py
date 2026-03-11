import os
import random
import math
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_socketio import SocketIO, join_room, leave_room, emit
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from characters import CHARACTERS, GRADE_STATS, STAR_BONUS, GRADE_ORDER, GACHA_RATES
import json

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///game.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading", logger=True, engineio_logger=True, manage_session=False)
login_manager = LoginManager(app)
login_manager.login_view = 'index'

# ─── MODELS ───────────────────────────────────────────────────────────────────

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    points = db.Column(db.Integer, default=0)
    wins = db.Column(db.Integer, default=0)
    losses = db.Column(db.Integer, default=0)
    pull_count = db.Column(db.Integer, default=0)
    collection = db.Column(db.Text, default='{}')  # JSON: {char_id: {grade, stars}}

    def get_collection(self):
        return json.loads(self.collection)

    def save_collection(self, col):
        self.collection = json.dumps(col)

    def add_starter_characters(self):
        col = self.get_collection()
        for cid, cdata in CHARACTERS.items():
            if cdata['is_starter'] and cid not in col:
                col[cid] = {'grade': cdata['base_grade'], 'stars': 1}
        self.save_collection(col)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ─── AUTH ROUTES ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if current_user.is_authenticated:
        return render_template('index.html', user=current_user)
    return render_template('index.html', user=None)

@app.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if not username or not password:
        return jsonify({'success': False, 'message': 'Username dan password wajib diisi.'})
    if User.query.filter_by(username=username).first():
        return jsonify({'success': False, 'message': 'Username sudah dipakai.'})
    user = User(username=username, password_hash=generate_password_hash(password))
    db.session.add(user)
    db.session.commit()
    user.add_starter_characters()
    db.session.commit()
    login_user(user)
    return jsonify({'success': True})

@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    user = User.query.filter_by(username=data.get('username', '')).first()
    if user and check_password_hash(user.password_hash, data.get('password', '')):
        login_user(user)
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': 'Username atau password salah.'})

@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/me')
def me():
    if not current_user.is_authenticated:
        return jsonify({'error': 'not logged in'}), 401
    col = current_user.get_collection()
    owned = []
    for cid, cinfo in col.items():
        if cid in CHARACTERS:
            c = CHARACTERS[cid]
            grade = cinfo['grade']
            stars = cinfo['stars']
            base = GRADE_STATS[grade]
            bonus = STAR_BONUS[stars]
            owned.append({
                'id': cid,
                'name': c['name'],
                'origin': c['origin'],
                'role': c['role'],
                'image': c['image'],
                'grade': grade,
                'stars': stars,
                'base_grade': c['base_grade'],
                'hp': math.floor(base['hp'] * (1 + bonus)),
                'attack': math.floor(base['attack'] * (1 + bonus)),
                'defense': math.floor(base['defense'] * (1 + bonus)),
                'skills': c['skills'],
            })
    return jsonify({
        'id': current_user.id,
        'username': current_user.username,
        'points': current_user.points,
        'wins': current_user.wins,
        'losses': current_user.losses,
        'pull_count': current_user.pull_count,
        'collection': owned,
    })

# ─── GACHA ────────────────────────────────────────────────────────────────────

def pick_grade(guaranteed_a_plus=False):
    if guaranteed_a_plus:
        pool = {g: r for g, r in GACHA_RATES.items() if GRADE_ORDER.index(g) >= GRADE_ORDER.index('A')}
        total = sum(pool.values())
        pool = {g: r/total for g, r in pool.items()}
    else:
        pool = GACHA_RATES
    r = random.random()
    cumulative = 0
    for grade in ['SS', 'S', 'A', 'B', 'C']:
        cumulative += pool.get(grade, 0)
        if r <= cumulative:
            return grade
    return 'C'

def do_pull(user, count):
    results = []
    col = user.get_collection()
    for i in range(count):
        user.pull_count += 1
        guaranteed = (user.pull_count % 10 == 0)
        grade = pick_grade(guaranteed_a_plus=guaranteed)
        candidates = [cid for cid, c in CHARACTERS.items() if c['base_grade'] == grade]
        if not candidates:
            candidates = [cid for cid, c in CHARACTERS.items() if c['base_grade'] == 'C']
        cid = random.choice(candidates)
        c = CHARACTERS[cid]
        is_new = cid not in col
        if is_new:
            col[cid] = {'grade': grade, 'stars': 1}
        else:
            current_stars = col[cid]['stars']
            current_grade = col[cid]['grade']
            if current_stars < 5:
                col[cid]['stars'] += 1
            else:
                grade_idx = GRADE_ORDER.index(current_grade)
                if grade_idx < len(GRADE_ORDER) - 1:
                    col[cid]['grade'] = GRADE_ORDER[grade_idx + 1]
                    col[cid]['stars'] = 1
        results.append({
            'id': cid,
            'name': c['name'],
            'image': c['image'],
            'grade': col[cid]['grade'],
            'stars': col[cid]['stars'],
            'is_new': is_new,
        })
    user.save_collection(col)
    return results

@app.route('/gacha', methods=['POST'])
@login_required
def gacha():
    data = request.get_json()
    count = data.get('count', 1)
    if count not in [1, 10]:
        return jsonify({'success': False, 'message': 'Invalid pull count.'})
    cost = count
    if current_user.points < cost:
        return jsonify({'success': False, 'message': f'Poin tidak cukup. Butuh {cost} poin.'})
    current_user.points -= cost
    results = do_pull(current_user, count)
    db.session.commit()
    return jsonify({'success': True, 'results': results, 'points': current_user.points})

# ─── BATTLE ROOMS ─────────────────────────────────────────────────────────────

rooms = {}  # room_code -> room_state

def make_battle_char(user, char_id):
    col = user.get_collection()
    if char_id not in col:
        return None
    cinfo = col[char_id]
    c = CHARACTERS[char_id]
    grade = cinfo['grade']
    stars = cinfo['stars']
    base = GRADE_STATS[grade]
    bonus = STAR_BONUS[stars]
    max_hp = math.floor(base['hp'] * (1 + bonus))
    return {
        'id': char_id,
        'name': c['name'],
        'image': c['image'],
        'grade': grade,
        'stars': stars,
        'max_hp': max_hp,
        'hp': max_hp,
        'attack': math.floor(base['attack'] * (1 + bonus)),
        'defense': math.floor(base['defense'] * (1 + bonus)),
        'skills': c['skills'],
        'status_effects': [],
        'buffs': [],
        'is_alive': True,
        'next_skill_boost': 0,
    }

def build_card_pool(battle_chars):
    pool = []
    for bc in battle_chars:
        if bc['is_alive']:
            for skill in bc['skills']:
                pool.append({'char_id': bc['id'], 'char_name': bc['name'], 'skill': skill})
    return pool

def draw_cards(pool, count=5):
    if len(pool) <= count:
        return pool[:]
    return random.sample(pool, count)

def calc_damage(attacker, defender, skill, next_skill_boost=0):
    mult = skill.get('damage_multiplier', 0)
    if mult == 0:
        return 0
    raw = attacker['attack'] * mult

    # next skill boost
    if next_skill_boost > 0:
        raw *= (1 + next_skill_boost)

    # ignore defense
    ignore = skill.get('ignore_defense', 0)
    effective_def = defender['defense'] * (1 - ignore)

    # defense buffs/debuffs
    for b in defender['buffs']:
        if b['type'] == 'defense_up':
            effective_def *= (1 + b['value'])
        elif b['type'] == 'defense_down':
            effective_def *= (1 - b['value'])
        elif b['type'] == 'all_stats_up':
            effective_def *= (1 + b['value'])

    dmg = max(1, math.floor(raw - effective_def * 0.5))
    return dmg

def apply_skill(room, acting_player, char_idx, skill, target_char_id=None):
    """Apply a single skill and return a log entry."""
    players = room['players']
    p = players[acting_player]
    opp = players[1 - acting_player]
    actor = p['chars'][char_idx]
    log = []

    if not actor['is_alive']:
        return log

    stype = skill.get('type', 'damage')
    target_type = skill.get('target', 'single_enemy')
    boost = actor.get('next_skill_boost', 0)
    actor['next_skill_boost'] = 0

    # ── DAMAGE ──
    if stype == 'damage':
        hits = skill.get('hits', 1)
        targets = []
        if target_type == 'single_enemy':
            alive = [c for c in opp['chars'] if c['is_alive']]
            if target_char_id:
                t = next((c for c in alive if c['id'] == target_char_id), None)
                targets = [t] if t else (alive[:1] if alive else [])
            else:
                targets = alive[:1]
        elif target_type == 'all_enemy':
            targets = [c for c in opp['chars'] if c['is_alive']]

        for target in targets:
            # check block
            blocked = any(b['type'] in ['block_all', 'invincible_counter'] for b in target['buffs'])
            total_dmg = 0
            for h in range(hits):
                is_last = (h == hits - 1)
                dmg = calc_damage(actor, target, skill, boost if h == 0 else 0)
                if skill.get('last_hit_double') and is_last:
                    dmg *= 2
                if blocked:
                    # reflect
                    reflect_val = next((b.get('reflect', 0) for b in target['buffs'] if b['type'] in ['block_all', 'invincible_counter']), 0)
                    actor['hp'] = max(0, actor['hp'] - math.floor(dmg * reflect_val))
                    if not actor['is_alive'] and actor['hp'] <= 0:
                        actor['is_alive'] = False
                    log.append(f"{actor['name']} menyerang {target['name']} tapi diblok! {math.floor(dmg * reflect_val)} damage balik ke {actor['name']}.")
                else:
                    # undying check
                    undying = any(b['type'] == 'undying' for b in target['buffs'])
                    target['hp'] = max(1 if undying else 0, target['hp'] - dmg)
                    total_dmg += dmg

            if not blocked:
                log.append(f"{actor['name']} menggunakan {skill['name']} ke {target['name']} -{total_dmg} HP.")
                if target['hp'] <= 0:
                    target['is_alive'] = False
                    log.append(f"{target['name']} telah dikalahkan!")

                # lifesteal
                if skill.get('lifesteal'):
                    heal = math.floor(total_dmg * skill['lifesteal'])
                    actor['hp'] = min(actor['max_hp'], actor['hp'] + heal)
                    log.append(f"{actor['name']} lifesteal +{heal} HP.")

                # drain
                if skill.get('drain'):
                    drain_amt = math.floor(total_dmg * skill['drain'])
                    actor['hp'] = min(actor['max_hp'], actor['hp'] + drain_amt)
                    log.append(f"{actor['name']} drain +{drain_amt} HP.")

                # overflow damage
                if skill.get('overflow_damage') and target['hp'] <= 0:
                    overflow = total_dmg - (target['hp'] + total_dmg)
                    next_alive = next((c for c in opp['chars'] if c['is_alive'] and c['id'] != target['id']), None)
                    if next_alive and overflow > 0:
                        next_alive['hp'] = max(0, next_alive['hp'] - overflow)
                        log.append(f"Overflow damage {overflow} ke {next_alive['name']}!")
                        if next_alive['hp'] <= 0:
                            next_alive['is_alive'] = False
                            log.append(f"{next_alive['name']} telah dikalahkan!")

                # apply effect
                effect = skill.get('effect')
                if effect:
                    chance = effect.get('chance', 1.0)
                    if random.random() <= chance:
                        if effect['type'] in ['burn', 'bleed', 'poison']:
                            target['status_effects'].append({**effect})
                            log.append(f"{target['name']} terkena {effect['type'].upper()}!")
                        elif effect['type'] == 'stun':
                            target['status_effects'].append({**effect})
                            log.append(f"{target['name']} terkena STUN!")
                        elif effect['type'] in ['defense_down', 'attack_down']:
                            target['buffs'].append({**effect})
                            log.append(f"{target['name']} stat turun!")
                        elif effect['type'] == 'skip_turn':
                            target['status_effects'].append({**effect})
                            log.append(f"{target['name']} kehilangan giliran berikutnya!")
                        elif effect['type'] == 'permanent_stat_down':
                            target['buffs'].append({**effect})
                            log.append(f"{target['name']} stat turun permanen!")
                        elif effect['type'] == 'heal_block':
                            target['status_effects'].append({**effect})
                            log.append(f"{target['name']} tidak bisa di-heal!")
                        elif effect['type'] == 'slow':
                            target['status_effects'].append({**effect})
                            log.append(f"{target['name']} terkena SLOW!")

                # dispel buffs
                if skill.get('dispel'):
                    target['buffs'] = []
                    log.append(f"Semua buff {target['name']} dihapus!")

                # aoe debuff (e.g muzan)
                aoe = skill.get('aoe_debuff')
                if aoe:
                    for ec in opp['chars']:
                        if ec['is_alive']:
                            ec['buffs'].append({**aoe})
                    log.append(f"Semua musuh terkena debuff!")

    # ── HEAL ──
    elif stype == 'heal':
        heal_mult = skill.get('heal_multiplier', 1.0)
        heal_amt = math.floor(actor['attack'] * heal_mult)
        targets = []
        if target_type == 'self':
            targets = [actor]
        elif target_type == 'lowest_hp_ally':
            alive = [c for c in p['chars'] if c['is_alive']]
            if alive:
                targets = [min(alive, key=lambda c: c['hp'])]
        elif target_type == 'all_ally':
            targets = [c for c in p['chars'] if c['is_alive']]
        elif target_type == 'single_ally':
            alive = [c for c in p['chars'] if c['is_alive']]
            if target_char_id:
                t = next((c for c in alive if c['id'] == target_char_id), None)
                targets = [t] if t else (alive[:1] if alive else [])
            else:
                targets = alive[:1]

        for t in targets:
            blocked = any(se['type'] == 'heal_block' for se in t['status_effects'])
            if not blocked:
                t['hp'] = min(t['max_hp'], t['hp'] + heal_amt)
                log.append(f"{actor['name']} heal {t['name']} +{heal_amt} HP.")
            else:
                log.append(f"{t['name']} tidak bisa di-heal!")

        # cleanse
        if skill.get('cleanse'):
            for t in targets:
                t['status_effects'] = [se for se in t['status_effects'] if se.get('type') in ['heal_block']]
                log.append(f"Status efek negatif {t['name']} dibersihkan!")

        # shield
        effect = skill.get('effect')
        if effect and effect['type'] == 'shield':
            for t in targets:
                t['buffs'].append({**effect})
                log.append(f"{t['name']} mendapat shield!")

    # ── BUFF ──
    elif stype == 'buff':
        targets = []
        if target_type == 'self':
            targets = [actor]
        elif target_type == 'all_ally':
            targets = [c for c in p['chars'] if c['is_alive']]
        elif target_type == 'single_ally':
            alive = [c for c in p['chars'] if c['is_alive']]
            if target_char_id:
                t = next((c for c in alive if c['id'] == target_char_id), None)
                targets = [t] if t else (alive[:1] if alive else [])
            else:
                targets = alive[:1]

        effect = skill.get('effect')
        if effect:
            for t in targets:
                t['buffs'].append({**effect})
                log.append(f"{t['name']} mendapat buff {effect['type']}!")

        # next skill boost
        if skill.get('next_skill_boost'):
            actor['next_skill_boost'] = skill['next_skill_boost']
            log.append(f"{actor['name']} skill berikutnya +{int(skill['next_skill_boost']*100)}% damage!")

        # self heal
        if skill.get('self_heal'):
            heal_amt = math.floor(actor['max_hp'] * skill['self_heal'])
            actor['hp'] = min(actor['max_hp'], actor['hp'] + heal_amt)
            log.append(f"{actor['name']} heal diri sendiri +{heal_amt} HP.")

    # ── DEBUFF ──
    elif stype == 'debuff':
        targets = []
        if target_type == 'single_enemy':
            alive = [c for c in opp['chars'] if c['is_alive']]
            if target_char_id:
                t = next((c for c in alive if c['id'] == target_char_id), None)
                targets = [t] if t else (alive[:1] if alive else [])
            else:
                targets = alive[:1]
        elif target_type == 'all_enemy':
            targets = [c for c in opp['chars'] if c['is_alive']]

        effect = skill.get('effect')
        if effect:
            for t in targets:
                t['status_effects'].append({**effect})
                log.append(f"{t['name']} terkena {effect['type']}!")

    # reset cards
    if skill.get('reset_cards'):
        pool = build_card_pool(p['chars'])
        p['cards'] = draw_cards(pool)
        log.append(f"Kartu {p['username']} direset!")

    return log

def tick_status_effects(chars):
    log = []
    for c in chars:
        if not c['is_alive']:
            continue
        new_effects = []
        new_buffs = []
        for se in c['status_effects']:
            setype = se.get('type')
            if setype in ['burn', 'bleed', 'poison']:
                dmg = math.floor(c['max_hp'] * se.get('value', 0.05))
                c['hp'] = max(0, c['hp'] - dmg)
                log.append(f"{c['name']} -{dmg} HP dari {setype.upper()}.")
                if c['hp'] <= 0:
                    c['is_alive'] = False
                    log.append(f"{c['name']} telah dikalahkan!")
            se['duration'] -= 1
            if se['duration'] > 0:
                new_effects.append(se)
        for b in c['buffs']:
            if b.get('type') == 'permanent_stat_down':
                new_buffs.append(b)
                continue
            b['duration'] -= 1
            if b['duration'] > 0:
                new_buffs.append(b)
        c['status_effects'] = new_effects
        c['buffs'] = new_buffs
    return log

def check_winner(room):
    players = room['players']
    for i, p in enumerate(players):
        if all(not c['is_alive'] for c in p['chars']):
            return 1 - i
    return None

# ─── SOCKET EVENTS ────────────────────────────────────────────────────────────

from flask_socketio import disconnect
from flask import session as flask_session

connected_users = {}  # sid -> user_id

@socketio.on('connect')
def on_connect():
    # try flask-login first
    try:
        if current_user.is_authenticated:
            connected_users[request.sid] = current_user.id
            return
    except Exception:
        pass
    # try session
    try:
        uid = flask_session.get('_user_id') or flask_session.get('user_id')
        if uid:
            connected_users[request.sid] = int(uid)
    except Exception:
        pass

@socketio.on('auth')
def on_auth(data):
    # frontend sends this after login
    uid = data.get('user_id')
    if uid:
        connected_users[request.sid] = int(uid)

@socketio.on('disconnect')
def on_disconnect():
    connected_users.pop(request.sid, None)

def get_socket_user():
    uid = connected_users.get(request.sid)
    if not uid:
        # fallback: try current_user
        try:
            u = current_user._get_current_object()
            if u.is_authenticated:
                return u
        except Exception:
            pass
        return None
    return User.query.get(uid)

@socketio.on('create_room')
def on_create_room(data):
    user = get_socket_user()
    if not user:
        emit('error', {'message': 'Silakan login dulu.'})
        return
    team = data.get('team', [])
    if len(team) != 3:
        emit('error', {'message': 'Pilih tepat 3 karakter.'})
        return
    code = ''.join(random.choices('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', k=6))
    chars = []
    for cid in team:
        bc = make_battle_char(user, cid)
        if bc:
            chars.append(bc)
    if len(chars) != 3:
        emit('error', {'message': 'Karakter tidak valid.'})
        return
    pool = build_card_pool(chars)
    rooms[code] = {
        'players': [
            {'username': user.username, 'user_id': user.id, 'chars': chars, 'cards': draw_cards(pool), 'sid': request.sid},
        ],
        'turn': 0,
        'phase': 'waiting',
        'log': [],
        'gbk_choices': {},
        'selected_skills': {},
    }
    join_room(code)
    emit('room_created', {'code': code})

@socketio.on('join_room_battle')
def on_join_room(data):
    user = get_socket_user()
    if not user:
        emit('error', {'message': 'Silakan login dulu.'})
        return
    code = data.get('code', '').strip().upper()
    team = data.get('team', [])
    if code not in rooms:
        emit('error', {'message': 'Kode room tidak ditemukan.'})
        return
    room = rooms[code]
    if len(room['players']) >= 2:
        emit('error', {'message': 'Room sudah penuh.'})
        return
    if len(team) != 3:
        emit('error', {'message': 'Pilih tepat 3 karakter.'})
        return
    chars = []
    for cid in team:
        bc = make_battle_char(user, cid)
        if bc:
            chars.append(bc)
    if len(chars) != 3:
        emit('error', {'message': 'Karakter tidak valid.'})
        return
    pool = build_card_pool(chars)
    room['players'].append({
        'username': user.username,
        'user_id': user.id,
        'chars': chars,
        'cards': draw_cards(pool),
        'sid': request.sid,
    })
    join_room(code)
    room['phase'] = 'gbk'
    socketio.emit('battle_start', {'room': sanitize_room(room, 0), 'player_index': 0}, room=room['players'][0]['sid'])
    socketio.emit('battle_start', {'room': sanitize_room(room, 1), 'player_index': 1}, room=room['players'][1]['sid'])

@socketio.on('gbk_choice')
def on_gbk_choice(data):
    user = get_socket_user()
    if not user:
        return
    code = data.get('code')
    choice = data.get('choice')  # 'rock', 'paper', 'scissors'
    if code not in rooms:
        return
    room = rooms[code]
    pidx = next((i for i, p in enumerate(room['players']) if p['user_id'] == user.id), None)
    if pidx is None:
        return
    room['gbk_choices'][pidx] = choice
    if len(room['gbk_choices']) == 2:
        c0, c1 = room['gbk_choices'][0], room['gbk_choices'][1]
        outcomes = {('rock','scissors'): 0, ('scissors','paper'): 0, ('paper','rock'): 0,
                    ('scissors','rock'): 1, ('paper','scissors'): 1, ('rock','paper'): 1}
        result = outcomes.get((c0, c1))
        if result is None:
            room['turn'] = random.randint(0, 1)
        else:
            room['turn'] = result
        room['phase'] = 'battle'
        room['gbk_choices'] = {}
        for i in range(2):
            socketio.emit('gbk_result', {
                'your_choice': room['players'][i].get('gbk_temp', choice),
                'opp_choice': room['players'][1-i].get('gbk_temp', choice),
                'first': room['turn'],
                'room': sanitize_room(room, i),
            }, room=room['players'][i]['sid'])

@socketio.on('select_skills')
def on_select_skills(data):
    user = get_socket_user()
    if not user:
        return
    code = data.get('code')
    selected = data.get('selected', [])  # list of {card_index, target_char_id}
    if code not in rooms:
        return
    room = rooms[code]
    if room['phase'] != 'battle':
        return
    pidx = next((i for i, p in enumerate(room['players']) if p['user_id'] == user.id), None)
    if pidx is None or pidx != room['turn']:
        emit('error', {'message': 'Bukan giliran kamu.'})
        return
    if len(selected) != 3:
        emit('error', {'message': 'Pilih tepat 3 kartu.'})
        return

    p = room['players'][pidx]
    cards = p['cards']
    log = []

    for sel in selected:
        cidx = sel.get('card_index')
        target_id = sel.get('target_char_id')
        if cidx is None or cidx >= len(cards):
            continue
        card = cards[cidx]
        char_idx = next((i for i, c in enumerate(p['chars']) if c['id'] == card['char_id']), None)
        if char_idx is None:
            continue
        skill_log = apply_skill(room, pidx, char_idx, card['skill'], target_id)
        log.extend(skill_log)
        winner = check_winner(room)
        if winner is not None:
            break

    # tick status effects for acting player's chars after their turn
    log.extend(tick_status_effects(p['chars']))

    winner = check_winner(room)
    if winner is not None:
        room['phase'] = 'ended'
        room['log'].extend(log)
        _end_battle(room, winner)
        return

    # refresh acting player cards
    pool = build_card_pool(p['chars'])
    p['cards'] = draw_cards(pool)

    # switch turn
    room['turn'] = 1 - pidx
    next_p = room['players'][room['turn']]

    # tick status effects for next player chars at start of their turn
    tick_log = tick_status_effects(next_p['chars'])
    log.extend(tick_log)

    winner = check_winner(room)
    if winner is not None:
        room['phase'] = 'ended'
        room['log'].extend(log)
        _end_battle(room, winner)
        return

    # Add turn separator
    separator = f"---TURN:{room['players'][pidx]['username']}---"
    room['log'].append(separator)
    room['log'].extend(log)

    for i in range(2):
        socketio.emit('turn_result', {
            'log': log,
            'room': sanitize_room(room, i),
            'turn_username': room['players'][pidx]['username'],
        }, room=room['players'][i]['sid'])

def _end_battle(room, winner_idx):
    loser_idx = 1 - winner_idx
    winner_user = User.query.get(room['players'][winner_idx]['user_id'])
    loser_user = User.query.get(room['players'][loser_idx]['user_id'])
    if winner_user:
        winner_user.wins += 1
        winner_user.points += 1
    if loser_user:
        loser_user.losses += 1
    db.session.commit()
    for i in range(2):
        socketio.emit('battle_ended', {
            'winner': room['players'][winner_idx]['username'],
            'you_win': i == winner_idx,
            'log': room['log'],
            'room': sanitize_room(room, i),
        }, room=room['players'][i]['sid'])
    # cleanup room
    code_to_remove = next((k for k, v in rooms.items() if v is room), None)
    if code_to_remove:
        rooms.pop(code_to_remove, None)

def sanitize_room(room, player_idx):
    """Return room state safe to send to a specific player."""
    players_out = []
    for i, p in enumerate(room['players']):
        players_out.append({
            'username': p['username'],
            'chars': p['chars'],
            'cards': p['cards'] if i == player_idx else [{'char_name': '?', 'skill': {'name': '?'}} for _ in p['cards']],
        })
    return {
        'players': players_out,
        'turn': room['turn'],
        'phase': room['phase'],
        'log': room['log'][-20:],
    }

# ─── MAIN ─────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)