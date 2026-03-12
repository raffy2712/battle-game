import os
import random
import math
import time
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
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading", logger=True, engineio_logger=True, manage_session=False, ping_timeout=60, ping_interval=25)
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
    collection = db.Column(db.Text, default='{}')

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

# ─── ADMIN ───────────────────────────────────────────────────────────────────

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'raffyadmin')

def is_admin():
    return current_user.is_authenticated and current_user.username == ADMIN_USERNAME

# FIX #9: tambah @login_required di semua admin routes sebagai lapis pertama
@app.route('/admin')
@login_required
def admin_panel():
    if not is_admin():
        return redirect(url_for('index'))
    users = User.query.all()
    users_data = []
    for u in users:
        if u.username == ADMIN_USERNAME:
            continue
        users_data.append({
            'id': u.id,
            'username': u.username,
            'points': u.points,
            'wins': u.wins,
            'losses': u.losses,
            'collection_count': len(json.loads(u.collection or '{}')),
        })
    return render_template('admin.html', users=users_data)

@app.route('/admin/give_points', methods=['POST'])
@login_required
def admin_give_points():
    if not is_admin():
        return jsonify({'success': False, 'message': 'Unauthorized'})
    data = request.get_json()
    username = data.get('username')
    points = int(data.get('points', 0))
    user = User.query.filter_by(username=username).first()
    if not user:
        return jsonify({'success': False, 'message': 'User tidak ditemukan'})
    user.points += points
    db.session.commit()
    return jsonify({'success': True, 'message': f'+{points} poin diberikan ke {username}', 'new_points': user.points})

@app.route('/admin/set_points', methods=['POST'])
@login_required
def admin_set_points():
    if not is_admin():
        return jsonify({'success': False, 'message': 'Unauthorized'})
    data = request.get_json()
    username = data.get('username')
    points = int(data.get('points', 0))
    user = User.query.filter_by(username=username).first()
    if not user:
        return jsonify({'success': False, 'message': 'User tidak ditemukan'})
    user.points = points
    db.session.commit()
    return jsonify({'success': True, 'message': f'Poin {username} diset ke {points}', 'new_points': user.points})

@app.route('/admin/reset_user', methods=['POST'])
@login_required
def admin_reset_user():
    if not is_admin():
        return jsonify({'success': False, 'message': 'Unauthorized'})
    data = request.get_json()
    username = data.get('username')
    user = User.query.filter_by(username=username).first()
    if not user:
        return jsonify({'success': False, 'message': 'User tidak ditemukan'})
    user.points = 0
    user.wins = 0
    user.losses = 0
    user.pull_count = 0
    col = {}
    for cid, cdata in CHARACTERS.items():
        if cdata['is_starter']:
            col[cid] = {'grade': cdata['base_grade'], 'stars': 1}
    user.save_collection(col)
    db.session.commit()
    return jsonify({'success': True, 'message': f'Akun {username} direset'})

# ─── BATTLE ROOMS ─────────────────────────────────────────────────────────────

rooms = {}
ROOM_TTL_SECONDS = 600  # FIX #8: 10 menit TTL untuk room waiting

def cleanup_stale_rooms():
    """Hapus room waiting yang sudah lebih dari 10 menit tidak ada yang join."""
    now = time.time()
    stale = [
        code for code, room in rooms.items()
        if room['phase'] == 'waiting' and now - room.get('created_at', now) > ROOM_TTL_SECONDS
    ]
    for code in stale:
        rooms.pop(code, None)

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

    for b in attacker['buffs']:
        if b['type'] == 'attack_up':
            raw *= (1 + b['value'])
        elif b['type'] == 'attack_down':
            raw *= (1 - b['value'])
        elif b['type'] == 'all_stats_up':
            raw *= (1 + b['value'])

    for b in attacker['buffs']:
        if b['type'] == 'permanent_stat_down':
            raw *= (1 - b.get('attack_down', 0))

    if next_skill_boost > 0:
        raw *= (1 + next_skill_boost)

    ignore = skill.get('ignore_defense', 0)
    effective_def = defender['defense'] * (1 - ignore)

    for b in defender['buffs']:
        if b['type'] == 'defense_up':
            effective_def *= (1 + b['value'])
        elif b['type'] == 'defense_down':
            effective_def *= (1 - b['value'])
        elif b['type'] == 'all_stats_up':
            effective_def *= (1 + b['value'])

    for b in defender['buffs']:
        if b['type'] == 'permanent_stat_down':
            effective_def *= (1 - b.get('defense_down', 0))

    dmg = max(1, math.floor(raw - effective_def * 0.5))
    return dmg

def apply_skill(room, acting_player, char_idx, skill, target_char_id=None):
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
            # `blocked` didefinisikan di sini, dalam scope target — tidak bocor ke heal
            blocked = any(b['type'] in ['block_all', 'invincible_counter'] for b in target['buffs'])
            counter_buff = next((b for b in target['buffs'] if b['type'] == 'counter'), None)
            total_dmg = 0
            start_hp = target['hp']
            for h in range(hits):
                is_last = (h == hits - 1)
                dmg = calc_damage(actor, target, skill, boost if h == 0 else 0)
                if skill.get('last_hit_double') and is_last:
                    dmg *= 2
                if blocked:
                    reflect_val = next((b.get('reflect', 0) for b in target['buffs'] if b['type'] in ['block_all', 'invincible_counter']), 0)
                    actor['hp'] = max(0, actor['hp'] - math.floor(dmg * reflect_val))
                    if actor['hp'] <= 0:
                        actor['is_alive'] = False
                    log.append(f"{actor['name']} menyerang {target['name']} tapi diblok! {math.floor(dmg * reflect_val)} damage balik ke {actor['name']}.")
                else:
                    # Terapkan shield: kurangi damage masuk berdasarkan total nilai shield aktif
                    shield_factor = 1.0
                    for b in target['buffs']:
                        if b['type'] == 'shield':
                            shield_factor *= max(0.0, 1.0 - b.get('value', 0.0))
                    if shield_factor < 1.0 and dmg > 0:
                        dmg = math.floor(dmg * shield_factor)
                    undying = any(b['type'] == 'undying' for b in target['buffs'])
                    target['hp'] = max(1 if undying else 0, target['hp'] - dmg)
                    total_dmg += dmg

            if not blocked:
                log.append(f"{actor['name']} menggunakan {skill['name']} ke {target['name']} -{total_dmg} HP.")
                if target['hp'] <= 0:
                    target['is_alive'] = False
                    log.append(f"{target['name']} telah dikalahkan!")

                if counter_buff and total_dmg > 0:
                    reflect_dmg = math.floor(total_dmg * counter_buff.get('value', 0))
                    if reflect_dmg > 0:
                        actor['hp'] = max(0, actor['hp'] - reflect_dmg)
                        log.append(f"{target['name']} counter! {reflect_dmg} damage balik ke {actor['name']}.")
                        if actor['hp'] <= 0:
                            actor['is_alive'] = False
                            log.append(f"{actor['name']} telah dikalahkan!")

                if skill.get('lifesteal'):
                    heal = math.floor(total_dmg * skill['lifesteal'])
                    actor['hp'] = min(actor['max_hp'], actor['hp'] + heal)
                    log.append(f"{actor['name']} lifesteal +{heal} HP.")

                if skill.get('drain'):
                    drain_amt = math.floor(total_dmg * skill['drain'])
                    actor['hp'] = min(actor['max_hp'], actor['hp'] + drain_amt)
                    log.append(f"{actor['name']} drain +{drain_amt} HP.")

                if skill.get('self_heal'):
                    heal_amt = math.floor(actor['max_hp'] * skill['self_heal'])
                    actor['hp'] = min(actor['max_hp'], actor['hp'] + heal_amt)
                    log.append(f"{actor['name']} heal diri sendiri +{heal_amt} HP.")

                if skill.get('overflow_damage') and target['hp'] <= 0:
                    # Sisa damage yang "kelebihan" dari HP awal target dialirkan ke musuh berikutnya
                    overflow = max(0, total_dmg - start_hp)
                    next_alive = next((c for c in opp['chars'] if c['is_alive'] and c['id'] != target['id']), None)
                    if next_alive and overflow > 0:
                        next_alive['hp'] = max(0, next_alive['hp'] - overflow)
                        log.append(f"Overflow damage {overflow} ke {next_alive['name']}!")
                        if next_alive['hp'] <= 0:
                            next_alive['is_alive'] = False
                            log.append(f"{next_alive['name']} telah dikalahkan!")

                effect = skill.get('effect')
                if effect:
                    chance = effect.get('chance', 1.0)
                    if random.random() <= chance:
                        if effect['type'] in ['burn', 'bleed', 'poison']:
                            target['status_effects'].append({**effect})
                            log.append(f"{target['name']} terkena {effect['type'].upper()}!")
                        elif effect['type'] == 'stun':
                            target['status_effects'].append({**effect})
                            log.append(f"{target['name']} terkena STUN! Tidak bisa bertindak {effect.get('duration',1)} giliran.")
                        elif effect['type'] in ['defense_down', 'attack_down']:
                            target['buffs'].append({**effect})
                            log.append(f"{target['name']} stat turun!")
                        elif effect['type'] == 'skip_turn':
                            target['status_effects'].append({**effect})
                            log.append(f"{target['name']} kehilangan {effect.get('duration',1)} giliran!")
                        elif effect['type'] == 'permanent_stat_down':
                            target['buffs'].append({**effect})
                            log.append(f"{target['name']} stat turun permanen!")
                        elif effect['type'] == 'heal_block':
                            target['status_effects'].append({**effect})
                            log.append(f"{target['name']} tidak bisa di-heal!")
                        elif effect['type'] == 'slow':
                            target['status_effects'].append({**effect})
                            log.append(f"{target['name']} terkena SLOW! Hanya bisa pakai 1 skill.")

                if skill.get('dispel'):
                    target['buffs'] = []
                    log.append(f"Semua buff {target['name']} dihapus!")

                aoe = skill.get('aoe_debuff')
                if aoe:
                    for ec in opp['chars']:
                        if ec['is_alive']:
                            ec['buffs'].append({**aoe})
                    log.append(f"Semua musuh terkena debuff!")

        # ally_buff untuk damage skills (e.g. Cyclops big_bang)
        ally_buff = skill.get('ally_buff')
        if ally_buff:
            for t in [c for c in p['chars'] if c['is_alive']]:
                t['buffs'].append({**ally_buff})
            log.append(f"Semua ally mendapat buff {ally_buff['type']}!")

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

        NEGATIVE_EFFECTS = ['burn', 'bleed', 'poison', 'stun', 'slow', 'skip_turn', 'attack_down', 'defense_down', 'heal_block']

        for t in targets:
            # FIX #1: cek heal_block per-target dengan variable lokal, tidak pakai `blocked` dari scope damage
            heal_blocked = any(se.get('type') == 'heal_block' for se in t.get('status_effects', []))
            if not heal_blocked:
                t['hp'] = min(t['max_hp'], t['hp'] + heal_amt)
                log.append(f"{actor['name']} heal {t['name']} +{heal_amt} HP.")
                hot_turns = skill.get('heal_over_turns', 0)
                if hot_turns > 0:
                    hot_per_turn = math.floor(heal_amt * 0.5)
                    t['status_effects'].append({
                        'type': 'heal_over_time',
                        'value': hot_per_turn,
                        'duration': hot_turns,
                    })
                    log.append(f"{t['name']} akan heal +{hot_per_turn} HP tiap giliran selama {hot_turns} giliran.")
            else:
                log.append(f"{t['name']} tidak bisa di-heal!")

        if skill.get('cleanse'):
            for t in targets:
                before = len(t['status_effects'])
                t['status_effects'] = [se for se in t['status_effects'] if se.get('type') not in NEGATIVE_EFFECTS]
                t['buffs'] = [b for b in t['buffs'] if b.get('type') not in ['attack_down', 'defense_down']]
                if len(t['status_effects']) < before:
                    log.append(f"Status efek negatif {t['name']} dibersihkan!")

        effect = skill.get('effect')
        if effect and effect.get('type') == 'shield':
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

        ally_buff = skill.get('ally_buff')
        if ally_buff:
            for t in [c for c in p['chars'] if c['is_alive']]:
                t['buffs'].append({**ally_buff})
            log.append(f"Semua ally mendapat buff {ally_buff['type']}!")

        if skill.get('next_skill_boost'):
            actor['next_skill_boost'] = skill['next_skill_boost']
            log.append(f"{actor['name']} skill berikutnya +{int(skill['next_skill_boost']*100)}% damage!")

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

    if skill.get('reset_cards'):
        pool = build_card_pool(p['chars'])
        p['cards'] = draw_cards(pool)
        p['cards_reset_this_turn'] = True
        log.append(f"Kartu {p['username']} direset!")

    return log

def tick_dot_effects(chars):
    """
    Dipanggil di AWAL giliran player (sebelum dia bertindak).
    Hanya proses DOT (burn/bleed/poison) dan heal_over_time.
    TIDAK decrement duration stun/skip_turn — karena efek itu baru
    boleh berkurang SETELAH giliran si pemilik selesai diblokir.
    """
    log = []
    for c in chars:
        if not c['is_alive']:
            continue
        for se in c['status_effects']:
            setype = se.get('type')
            if setype in ['burn', 'bleed', 'poison']:
                dmg = math.floor(c['max_hp'] * se.get('value', 0.05))
                c['hp'] = max(0, c['hp'] - dmg)
                log.append(f"{c['name']} -{dmg} HP dari {setype.upper()}.")
                if c['hp'] <= 0:
                    c['is_alive'] = False
                    log.append(f"{c['name']} telah dikalahkan!")
            elif setype == 'heal_over_time':
                heal_val = se.get('value', 0)
                c['hp'] = min(c['max_hp'], c['hp'] + heal_val)
                log.append(f"{c['name']} +{heal_val} HP dari heal bertahap.")
    return log

def tick_duration_effects(chars):
    """
    Dipanggil di AKHIR giliran player (setelah dia selesai bertindak).
    Decrement semua duration efek — stun/skip_turn/slow/burn/bleed/dll.
    Ini memastikan efek kontrol benar-benar memblokir giliran dulu
    sebelum durasinya berkurang.
    """
    log = []
    for c in chars:
        if not c['is_alive']:
            continue
        new_effects = []
        new_buffs = []
        for se in c['status_effects']:
            setype = se.get('type')
            se['duration'] -= 1
            if se['duration'] > 0:
                new_effects.append(se)
            else:
                if setype in ['stun', 'skip_turn']:
                    log.append(f"{c['name']} sudah pulih dari efek {setype}.")
        for b in c['buffs']:
            if b.get('type') == 'permanent_stat_down':
                new_buffs.append(b)
                continue
            b['duration'] = b.get('duration', 1) - 1
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

def get_max_actions(player):
    stunned_ids = set()
    slowed_ids = set()
    for c in player['chars']:
        if not c['is_alive']:
            continue
        for se in c.get('status_effects', []):
            if se['type'] in ['stun', 'skip_turn']:
                stunned_ids.add(c['id'])
            elif se['type'] == 'slow':
                slowed_ids.add(c['id'])
    return {
        'stunned_char_ids': stunned_ids,
        'slowed_char_ids': slowed_ids,
    }

# ─── SOCKET EVENTS ────────────────────────────────────────────────────────────

from flask_socketio import disconnect
from flask import session as flask_session

connected_users = {}

@socketio.on('connect')
def on_connect():
    try:
        if current_user.is_authenticated:
            connected_users[request.sid] = current_user.id
            return
    except Exception:
        pass
    try:
        uid = flask_session.get('_user_id') or flask_session.get('user_id')
        if uid:
            connected_users[request.sid] = int(uid)
    except Exception:
        pass

@socketio.on('auth')
def on_auth(data):
    uid = data.get('user_id')
    if uid:
        connected_users[request.sid] = int(uid)

@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    connected_users.pop(sid, None)
    # FIX #5: cleanup semua phase, bukan hanya 'battle'
    for code, room in list(rooms.items()):
        for i, p in enumerate(room['players']):
            if p.get('sid') == sid:
                if room['phase'] in ('waiting', 'gbk'):
                    # Room belum mulai — hapus langsung, tidak perlu notify siapapun
                    rooms.pop(code, None)
                elif room['phase'] == 'battle':
                    opp_idx = 1 - i
                    if len(room['players']) > opp_idx:
                        opp_sid = room['players'][opp_idx].get('sid')
                        if opp_sid:
                            socketio.emit('opponent_disconnected', {
                                'message': f"{p['username']} terputus dari pertandingan."
                            }, room=opp_sid)
                    room['phase'] = 'ended'
                    rooms.pop(code, None)
                break

@socketio.on('rejoin_battle')
def on_rejoin_battle(data):
    user = get_socket_user()
    if not user:
        return
    code = data.get('code', '').strip().upper()
    if code not in rooms:
        emit('error', {'message': 'Room tidak ditemukan.'})
        return
    room = rooms[code]
    pidx = next((i for i, p in enumerate(room['players']) if p['user_id'] == user.id), None)
    if pidx is None:
        return
    room['players'][pidx]['sid'] = request.sid
    join_room(code)
    emit('turn_result', {
        'log': [],
        'room': sanitize_room(room, pidx),
        'turn_username': room['players'][room['turn']]['username'],
        'your_player_index': pidx,
    })

def get_socket_user():
    uid = connected_users.get(request.sid)
    if not uid:
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

    # FIX #8: cleanup room stale sebelum buat baru
    cleanup_stale_rooms()

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
        'created_at': time.time(),  # FIX #8: timestamp untuk TTL cleanup
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
    choice = data.get('choice')
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
        choices_snapshot = {0: c0, 1: c1}
        room['gbk_choices'] = {}
        for i in range(2):
            socketio.emit('gbk_result', {
                'your_choice': choices_snapshot[i],
                'opp_choice': choices_snapshot[1-i],
                'first': room['turn'],
                'room': sanitize_room(room, i),
                'your_player_index': i,
            }, room=room['players'][i]['sid'])

@socketio.on('select_skills')
def on_select_skills(data):
    user = get_socket_user()
    if not user:
        emit('error', {'message': 'Silakan login dulu.'})
        return
    code = data.get('code')
    selected = data.get('selected', [])
    if code not in rooms:
        emit('error', {'message': 'Room tidak ditemukan.'})
        return
    room = rooms[code]
    if room['phase'] != 'battle':
        emit('error', {'message': 'Battle belum dimulai atau sudah selesai.'})
        return
    pidx = next((i for i, p in enumerate(room['players']) if p['user_id'] == user.id), None)
    if pidx is None or pidx != room['turn']:
        emit('error', {'message': 'Bukan giliran kamu.'})
        return
    if len(selected) != 3:
        emit('error', {'message': 'Pilih tepat 3 aksi (skill atau skip).'})
        return

    p = room['players'][pidx]
    cards = p['cards']
    log = []
    p['cards_reset_this_turn'] = False

    action_info = get_max_actions(p)
    stunned_ids = action_info['stunned_char_ids']
    slowed_ids = action_info['slowed_char_ids']
    char_skill_count = {}

    for sel in selected:
        if sel.get('is_skip'):
            log.append(f"{p['username']} melewati 1 aksi.")
            continue

        cidx = sel.get('card_index')
        target_id = sel.get('target_char_id')

        if cidx is None or cidx >= len(cards):
            continue

        card = cards[cidx]
        char_id = card['char_id']
        char_idx = next((i for i, c in enumerate(p['chars']) if c['id'] == char_id), None)
        if char_idx is None:
            continue

        actor = p['chars'][char_idx]

        if char_id in stunned_ids:
            log.append(f"{actor['name']} masih kena stun, aksi dibatalkan!")
            continue

        if char_id in slowed_ids:
            char_skill_count[char_id] = char_skill_count.get(char_id, 0) + 1
            if char_skill_count[char_id] > 1:
                log.append(f"{actor['name']} kena slow, hanya bisa pakai 1 skill per giliran!")
                continue

        skill_log = apply_skill(room, pidx, char_idx, card['skill'], target_id)
        log.extend(skill_log)

        if p.get('cards_reset_this_turn'):
            p['cards_reset_this_turn'] = False
            log.append(f"Sisa aksi dibatalkan karena kartu sudah direset.")
            break

        # Cek winner di dalam loop tapi jangan proses di sini — cukup break
        if check_winner(room) is not None:
            break

    # FIX #3: proses winner di SATU tempat setelah loop — hindari double emit/call
    final_winner = check_winner(room)
    if final_winner is not None:
        room['phase'] = 'ended'
        room['log'].extend(log)
        _end_battle(room, final_winner)
        return

    # Switch turn
    room['turn'] = 1 - pidx
    curr_p = room['players'][pidx]
    next_p = room['players'][room['turn']]

    # Step 1: Decrement duration efek player yang BARU SELESAI bertindak.
    # Stun/skip_turn sudah memblokir giliran mereka, baru sekarang boleh berkurang.
    duration_log = tick_duration_effects(curr_p['chars'])
    log.extend(duration_log)

    # Step 2: Tick DOT (burn/bleed/poison/heal_over_time) player berikutnya di awal gilirannya.
    dot_log = tick_dot_effects(next_p['chars'])
    log.extend(dot_log)

    pool = build_card_pool(next_p['chars'])
    next_p['cards'] = draw_cards(pool)

    # Cek lagi setelah tick (bisa mati kena burn/bleed setelah giliran ganti)
    final_winner = check_winner(room)
    if final_winner is not None:
        room['phase'] = 'ended'
        room['log'].extend(log)
        _end_battle(room, final_winner)
        return

    separator = f"---TURN:{room['players'][pidx]['username']}---"
    room['log'].append(separator)
    room['log'].extend(log)

    code_for_emit = next((k for k, v in rooms.items() if v is room), None)
    if code_for_emit:
        for i in range(2):
            socketio.emit('turn_result', {
                'log': log,
                'room': sanitize_room(room, i),
                'turn_username': room['players'][pidx]['username'],
                'your_player_index': i,
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
    code_to_remove = next((k for k, v in rooms.items() if v is room), None)
    if code_to_remove:
        rooms.pop(code_to_remove, None)

def sanitize_room(room, player_idx):
    players_out = []
    current_turn = room['turn']
    for i, p in enumerate(room['players']):
        show_cards = (i == player_idx) and (player_idx == current_turn)
        action_info = get_max_actions(p) if i == player_idx else {'stunned_char_ids': set(), 'slowed_char_ids': set()}
        players_out.append({
            'username': p['username'],
            'chars': p['chars'],
            'cards': p['cards'] if show_cards else [{'char_name': '?', 'skill': {'name': '?'}} for _ in p['cards']],
            'stunned_char_ids': list(action_info['stunned_char_ids']),
            'slowed_char_ids': list(action_info['slowed_char_ids']),
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
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)