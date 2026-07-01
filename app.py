import os, json, subprocess, re, urllib.request, urllib.parse, tarfile, shutil, threading, ssl, logging, socket, struct, time, zipfile, zlib, io
from logging.handlers import RotatingFileHandler
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, Response, flash, send_file
import bcrypt

app = Flask(__name__)
app.secret_key = os.urandom(32).hex()

USERS_FILE = '/opt/factorio-web/users.json'
SERVER_SETTINGS = '/opt/factorio/server-settings.json'
FACTORIO_CREDENTIALS_FILE = '/opt/factorio-web/factorio-credentials.json'
RCON_CONFIG_FILE = '/opt/factorio-web/rcon.json'
SERVICE_FILE = '/etc/systemd/system/factorio.service'
SAVES_DIR = '/opt/factorio/saves'
MODS_DIR = '/opt/factorio/mods'
FACTORIO_BIN = '/opt/factorio/bin/x64/factorio'
AUDIT_LOG_DIR = '/var/log/factorio-web'
SPACE_AGE_MODS = ['space-age', 'elevated-rails', 'recycler', 'quality']

update_lock = threading.Lock()
update_progress = {'running': False, 'lines': []}
control_lock = threading.Lock()
control_state = {'countdown': False, 'action': None, 'seconds_left': 0}


# ── audit logger ──────────────────────────────────────────────────────────────

def _setup_audit_logger():
    os.makedirs(AUDIT_LOG_DIR, exist_ok=True)
    handler = RotatingFileHandler(
        os.path.join(AUDIT_LOG_DIR, 'audit.log'),
        maxBytes=1024 * 1024,  # 1 MB per file
        backupCount=10,
    )
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger = logging.getLogger('factorio_audit')
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return logger

_audit = _setup_audit_logger()

def audit(action, detail='', user=None, ip=None):
    u = user or session.get('username', 'anonymous')
    i = ip or (request.remote_addr if request else 'internal')
    entry = f'[{u}@{i}] {action}'
    if detail:
        entry += f' {detail}'
    _audit.info(entry)


# ── helpers ──────────────────────────────────────────────────────────────────

def load_users():
    if not os.path.exists(USERS_FILE):
        return []
    with open(USERS_FILE) as f:
        return json.load(f)

def save_users(users):
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)

def find_user(username):
    return next((u for u in load_users() if u['username'] == username), None)

def hash_pw(pw):
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def check_pw(pw, hashed):
    return bcrypt.checkpw(pw.encode(), hashed.encode())

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

ROLES = ['viewer', 'user', 'moderator', 'admin']  # ordered lowest → highest

def role_rank(role):
    try:
        return ROLES.index(role)
    except ValueError:
        return -1

def get_current_role():
    user = find_user(session.get('username', ''))
    return user.get('role', 'viewer') if user else 'viewer'

def has_role(min_role):
    return role_rank(get_current_role()) >= role_rank(min_role)

def role_required(min_role):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not has_role(min_role):
                flash('Access denied.')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated
    return decorator

def can_manage(target_role):
    """True if current user outranks target_role."""
    return role_rank(get_current_role()) > role_rank(target_role)

def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.stdout.strip(), r.returncode

def service_status():
    out, _ = run('systemctl is-active factorio')
    return out  # 'active', 'inactive', 'failed'

def get_active_save():
    try:
        with open(SERVICE_FILE) as f:
            for line in f:
                m = re.search(r'--start-server\s+(\S+)', line)
                if m:
                    return os.path.basename(m.group(1))
    except Exception:
        pass
    return 'unknown'

def get_factorio_version():
    out, _ = run(f'{FACTORIO_BIN} --version 2>/dev/null')
    m = re.search(r'Version:\s+(\S+)', out)
    return m.group(1) if m else 'unknown'

def _urlopen(url, timeout=10):
    req = urllib.request.Request(url, headers={'User-Agent': 'curl/7.88.1'})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except ssl.SSLError:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return urllib.request.urlopen(req, timeout=timeout, context=ctx)

def parse_version(v):
    try:
        return tuple(int(x) for x in str(v).split('.'))
    except Exception:
        return (0, 0, 0)

def get_latest_versions():
    try:
        with _urlopen('https://factorio.com/api/latest-releases') as r:
            data = json.loads(r.read())
        def pick(d):
            return d if isinstance(d, str) else (d.get('headless') or d.get('alpha'))
        return {'stable': pick(data.get('stable', {})), 'experimental': pick(data.get('experimental', {}))}
    except Exception:
        return {'stable': None, 'experimental': None}

def backup_save():
    active = get_active_save()
    if active == 'unknown':
        return None
    src = os.path.join(SAVES_DIR, active)
    if not os.path.exists(src):
        return None
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    dest_name = f'backup_{ts}_{active}'
    shutil.copy2(src, os.path.join(SAVES_DIR, dest_name))
    return dest_name

def write_mod_list(space_age):
    os.makedirs(MODS_DIR, exist_ok=True)
    mods = [{'name': 'base', 'enabled': True}]
    for name in SPACE_AGE_MODS:
        mods.append({'name': name, 'enabled': bool(space_age)})
    with open(os.path.join(MODS_DIR, 'mod-list.json'), 'w') as f:
        json.dump({'mods': mods}, f, indent=2)

def is_space_age_enabled():
    path = os.path.join(MODS_DIR, 'mod-list.json')
    if not os.path.exists(path):
        return None  # unknown / not configured
    try:
        with open(path) as f:
            data = json.load(f)
        enabled = {m['name'] for m in data.get('mods', []) if m.get('enabled')}
        return 'space-age' in enabled
    except Exception:
        return None

def get_uptime():
    out, _ = run('systemctl show factorio --property=ActiveEnterTimestamp --value')
    if not out or out == 'n/a':
        return None
    try:
        dt = datetime.strptime(out.strip(), '%a %Y-%m-%d %H:%M:%S %Z')
        delta = datetime.utcnow() - dt
        s = int(delta.total_seconds())
        if s < 0:
            s = 0
        h, r = divmod(s, 3600)
        m, sec = divmod(r, 60)
        return f'{h}h {m}m {sec}s'
    except Exception:
        return out

def get_last_autosave():
    try:
        saves = sorted(
            [f for f in os.listdir(SAVES_DIR) if f.endswith('.zip')],
            key=lambda x: os.path.getmtime(os.path.join(SAVES_DIR, x)),
            reverse=True
        )
        if saves:
            mtime = os.path.getmtime(os.path.join(SAVES_DIR, saves[0]))
            return datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        pass
    return None

_DEFAULT_SERVER_SETTINGS = {
    'name': '', 'description': '', 'tags': [],
    'game_password': '', 'max_players': 0,
    'visibility': {'public': False, 'lan': True},
    'require_user_verification': True,
    'username': '', 'token': '',
    'auto_pause': True, 'afk_autokick_interval': 0,
    'only_admins_can_pause_the_game': True,
    'allow_commands': 'admins-only',
    'autosave_interval': 10, 'autosave_slots': 5,
    'autosave_only_on_server': True,
    'non_blocking_saving': False,
    'research_queue_setting': 'after-victory',
    'admins': [],
}

def load_server_settings():
    if not os.path.exists(SERVER_SETTINGS):
        return dict(_DEFAULT_SERVER_SETTINGS)
    with open(SERVER_SETTINGS) as f:
        return json.load(f)

def save_server_settings(data):
    os.makedirs(os.path.dirname(SERVER_SETTINGS), exist_ok=True)
    with open(SERVER_SETTINGS, 'w') as f:
        json.dump(data, f, indent=2)

def ensure_user_roles():
    users = load_users()
    if not users:
        return
    changed = False
    for i, u in enumerate(users):
        if 'role' not in u:
            u['role'] = 'admin' if i == 0 else 'user'
            changed = True
    if changed:
        save_users(users)


# ── template context ─────────────────────────────────────────────────────────

@app.context_processor
def inject_globals():
    cr = get_current_role()
    return {
        'current_role': cr,
        'is_admin': cr == 'admin',
        'is_mod_or_above': has_role('moderator'),
    }


# ── setup guard ───────────────────────────────────────────────────────────────

@app.before_request
def check_setup():
    if request.endpoint in ('setup', 'static'):
        return
    if not load_users():
        return redirect(url_for('setup'))


# ── auth ─────────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        submitted = request.form.get('username', '')
        u = find_user(submitted)
        if u and check_pw(request.form.get('password', ''), u['password']):
            session['username'] = u['username']
            audit('LOGIN', user=u['username'])
            return redirect(url_for('dashboard'))
        audit('LOGIN_FAILED', f'user={submitted}', user='anonymous')
        flash('Invalid credentials')
    return render_template('login.html')

@app.route('/logout')
def logout():
    audit('LOGOUT')
    session.clear()
    return redirect(url_for('login'))

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    if load_users():
        return redirect(url_for('login'))
    error = None
    if request.method == 'POST':
        pw = request.form.get('password', '')
        pw2 = request.form.get('password2', '')
        if not pw:
            error = 'Password cannot be empty.'
        elif pw != pw2:
            error = 'Passwords do not match.'
        else:
            save_users([{'username': 'admin', 'password': hash_pw(pw), 'role': 'admin'}])
            audit('SETUP_COMPLETE', user='setup')
            session.clear()
            flash('Admin account created. Please log in.')
            return redirect(url_for('login'))
    return render_template('setup.html', error=error)


# ── dashboard ────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    versions = get_latest_versions()
    installed = get_factorio_version()
    iv = parse_version(installed)
    # Determine which release channel the current install belongs to
    installed_channel = None
    if installed != 'unknown':
        if versions.get('stable') and parse_version(versions['stable'])[:2] == iv[:2]:
            installed_channel = 'stable'
        elif versions.get('experimental') and parse_version(versions['experimental'])[:2] == iv[:2]:
            installed_channel = 'experimental'
    return render_template('dashboard.html',
        status=service_status(),
        version=installed,
        active_save=get_active_save(),
        uptime=get_uptime(),
        last_autosave=get_last_autosave(),
        installed=installed,
        latest_stable=versions['stable'],
        latest_experimental=versions['experimental'],
        stable_is_downgrade=parse_version(versions['stable']) < iv if versions['stable'] else False,
        experimental_is_downgrade=parse_version(versions['experimental']) < iv if versions['experimental'] else False,
        installed_channel=installed_channel,
        installed_space_age=is_space_age_enabled(),
        has_update=(
            installed not in (versions.get('stable'), versions.get('experimental')) and
            any(parse_version(v) > iv for v in versions.values() if v)
        ),
        factorio_bin=FACTORIO_BIN,
        username=session['username']
    )


# ── api ──────────────────────────────────────────────────────────────────────

@app.route('/api/status')
@login_required
def api_status():
    st = service_status()
    return jsonify({
        'status': st,
        'uptime': get_uptime(),
        'last_autosave': get_last_autosave(),
        'players': rcon_get_players() if st == 'active' else [],
        'countdown': control_state['countdown'],
        'countdown_seconds': control_state['seconds_left'],
        'countdown_action': control_state['action'],
    })

# (seconds_left, sleep_after_message)
_COUNTDOWN_STEPS = [(20, 5), (15, 5), (10, 5), (5, 1), (4, 1), (3, 1), (2, 1), (1, 1)]

@app.route('/api/control', methods=['POST'])
@login_required
def api_control():
    if not has_role('user'):
        return jsonify({'ok': False, 'error': 'Access denied'}), 403
    action = request.json.get('action')
    if action not in ('start', 'stop', 'restart'):
        return jsonify({'ok': False, 'error': 'unknown action'}), 400

    if action == 'start':
        _, code = run('systemctl start factorio')
        audit('SERVER_START', f'ok={code == 0}')
        return jsonify({'ok': code == 0, 'status': service_status()})

    if control_lock.locked():
        return jsonify({'ok': False, 'error': 'A shutdown/restart is already in progress'}), 409

    players = rcon_get_players()
    _user, _ip = session.get('username'), request.remote_addr

    def do_action():
        with control_lock:
            if players:
                control_state['countdown'] = True
                control_state['action'] = action
                word = 'restarting' if action == 'restart' else 'shutting down'
                for secs, delay in _COUNTDOWN_STEPS:
                    control_state['seconds_left'] = secs
                    unit = 'second' if secs == 1 else 'seconds'
                    try:
                        rcon_say(f'Server {word} in {secs} {unit}')
                    except Exception:
                        pass
                    time.sleep(delay)
                control_state['countdown'] = False
                control_state['seconds_left'] = 0
                control_state['action'] = None
            audit(f'SERVER_{action.upper()}', user=_user, ip=_ip)
            run(f'systemctl {action} factorio')

    threading.Thread(target=do_action, daemon=True).start()
    return jsonify({'ok': True, 'countdown': bool(players), 'players': players})

def load_factorio_credentials():
    if os.path.exists(FACTORIO_CREDENTIALS_FILE):
        with open(FACTORIO_CREDENTIALS_FILE) as f:
            return json.load(f)
    return {}

def save_factorio_credentials(username, token):
    os.makedirs(os.path.dirname(FACTORIO_CREDENTIALS_FILE), exist_ok=True)
    with open(FACTORIO_CREDENTIALS_FILE, 'w') as f:
        json.dump({'username': username, 'token': token}, f, indent=2)

def load_rcon_config():
    if os.path.exists(RCON_CONFIG_FILE):
        with open(RCON_CONFIG_FILE) as f:
            return json.load(f)
    return {}

def save_rcon_config(port, password):
    os.makedirs(os.path.dirname(RCON_CONFIG_FILE), exist_ok=True)
    with open(RCON_CONFIG_FILE, 'w') as f:
        json.dump({'port': int(port), 'password': password}, f, indent=2)

def rcon_exec(command):
    cfg = load_rcon_config()
    if not cfg.get('password'):
        raise Exception('RCON not configured')
    host, port, password = '127.0.0.1', int(cfg.get('port', 27015)), cfg['password']
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect((host, port))
        def recvn(n):
            buf = b''
            while len(buf) < n:
                chunk = s.recv(n - len(buf))
                if not chunk:
                    raise Exception('RCON connection closed')
                buf += chunk
            return buf
        def send_pkt(pid, ptype, body):
            encoded = body.encode('utf-8') + b'\x00\x00'
            pkt = struct.pack('<ii', pid, ptype) + encoded
            s.sendall(struct.pack('<i', len(pkt)) + pkt)
        def recv_pkt():
            size = struct.unpack('<i', recvn(4))[0]
            data = recvn(size)
            pid = struct.unpack('<i', data[0:4])[0]
            body = data[8:-2].decode('utf-8', errors='replace')
            return pid, body
        send_pkt(1, 3, password)
        pid, _ = recv_pkt()
        if pid == -1:
            raise Exception('RCON authentication failed')
        send_pkt(2, 2, command)
        _, body = recv_pkt()
        return body.strip()

def rcon_get_players():
    try:
        result = rcon_exec('/players online')
        players = []
        for line in result.splitlines():
            name = line.strip()
            if not name or name.startswith('Online players'):
                continue
            name = re.sub(r'\s*\(online\)\s*$', '', name)
            if name:
                players.append(name)
        return players
    except Exception:
        return []

def rcon_say(message):
    rcon_exec(message)  # bare text in RCON console = server chat broadcast

def rcon_kick(player, reason='Kicked by server admin'):
    rcon_exec(f'/kick {player} {reason}')

@app.route('/api/update', methods=['POST'])
@login_required
def api_update():
    if update_lock.locked():
        return jsonify({'ok': False, 'error': 'Update already in progress'}), 409

    channel = (request.json or {}).get('channel', 'experimental')
    space_age = bool((request.json or {}).get('space_age', True))
    if channel not in ('stable', 'experimental'):
        return jsonify({'ok': False, 'error': 'Invalid channel'}), 400

    _user = session.get('username', 'unknown')
    _ip = request.remote_addr
    _space_age = space_age

    def do_update():
        with update_lock:
            update_progress['running'] = True
            update_progress['lines'] = []
            def log(msg):
                update_progress['lines'].append(msg)

            try:
                versions = get_latest_versions()
                target = versions.get(channel)
                if not target:
                    log(f'Error: could not fetch {channel} version from Factorio API.')
                    return

                installed = get_factorio_version()
                is_fresh = installed == 'unknown'
                iv = parse_version(installed)
                tv = parse_version(target)
                is_downgrade = not is_fresh and tv < iv
                is_wrong_channel = not is_fresh and tv[:2] != iv[:2]

                if is_downgrade:
                    log(f'❌ Cannot downgrade: {installed} → {target}')
                    log('Wipe Factorio first via Settings → Danger Zone, then install the version you want.')
                    return
                if is_wrong_channel:
                    log(f'❌ Cannot switch channels: {installed} is on a different release branch than {target}.')
                    log('Wipe Factorio first via Settings → Danger Zone, then install the version you want.')
                    return

                creds = load_factorio_credentials()
                if not creds.get('username') or not creds.get('token'):
                    log('Error: Factorio credentials not configured.')
                    log('Go to Settings → Factorio Account and enter your username and token.')
                    return

                sa_label = ' + Space Age' if _space_age else ''
                log(f'{"Installing" if is_fresh else "Updating"} {target} ({channel}{sa_label})'
                    + ('' if is_fresh else f' (was {installed})'))

                if not is_fresh:
                    log('Backing up active save...')
                    backed_up = backup_save()
                    log(f'Backup: {backed_up}' if backed_up else 'Warning: no active save to back up.')

                audit('UPDATE_START', f'{installed}→{target} channel={channel} space_age={_space_age}', user=_user, ip=_ip)
                log('Stopping Factorio service...')
                run('systemctl stop factorio')

                params = f"username={urllib.parse.quote(creds['username'])}&token={urllib.parse.quote(creds['token'])}"
                url = f'https://www.factorio.com/get-download/{target}/headless/linux64?{params}'
                dest = f'/tmp/factorio_{target}.tar.xz'
                log(f'Downloading {target}...')
                with _urlopen(url) as resp, open(dest, 'wb') as out:
                    shutil.copyfileobj(resp, out)

                log('Extracting...')
                with tarfile.open(dest, 'r:xz') as t:
                    t.extractall('/opt/', filter='data')

                os.remove(dest)
                os.makedirs(SAVES_DIR, exist_ok=True)

                if not os.path.exists(SERVER_SETTINGS):
                    example = '/opt/factorio/data/server-settings.example.json'
                    if os.path.exists(example):
                        shutil.copy2(example, SERVER_SETTINGS)
                        log('Created server-settings.json from example.')
                    else:
                        save_server_settings(_DEFAULT_SERVER_SETTINGS)
                        log('Created default server-settings.json.')

                log(f'{"Enabling" if _space_age else "Disabling"} Space Age mods...')
                write_mod_list(_space_age)
                # Ensure service file uses our mods directory
                if os.path.exists(SERVICE_FILE):
                    with open(SERVICE_FILE) as f:
                        svc = f.read()
                    if '--mod-directory' not in svc:
                        svc = re.sub(r'(ExecStart=.+?)(\n)', rf'\1 --mod-directory {MODS_DIR}\2', svc)
                        with open(SERVICE_FILE, 'w') as f:
                            f.write(svc)
                        run('systemctl daemon-reload')

                if is_fresh:
                    default_save = os.path.join(SAVES_DIR, 'game.zip')
                    log('Generating default save (game.zip)...')
                    _, code = run(f'"{FACTORIO_BIN}" --create "{default_save}"')
                    if code == 0 and os.path.exists(default_save):
                        _write_service_file(default_save)
                        log('Default save created and service configured.')
                    else:
                        log('Warning: could not generate default save. Use Settings → Generate New Map.')

                log('Starting Factorio service...')
                run('systemctl start factorio')
                new_ver = get_factorio_version()
                audit('UPDATE_DONE', f'version={new_ver}', user=_user, ip=_ip)
                log(f'Done! Now running {new_ver}')
            except Exception as e:
                audit('UPDATE_ERROR', f'error={e}', user=_user, ip=_ip)
                log(f'Error: {e}')
                if not update_progress['lines'] or not update_progress['lines'][-1].startswith('Error:'):
                    run('systemctl start factorio')
            finally:
                update_progress['running'] = False

    t = threading.Thread(target=do_update, daemon=True)
    t.start()
    return jsonify({'ok': True})

@app.route('/api/update/progress')
@login_required
def api_update_progress():
    return jsonify({'running': update_progress['running'], 'lines': update_progress['lines']})

@app.route('/api/logs')
@login_required
def api_logs():
    out, _ = run('journalctl -u factorio -n 100 --no-pager --output=short')
    return jsonify({'lines': out.splitlines()})

@app.route('/api/chat')
@login_required
def api_chat():
    out, _ = run('journalctl -u factorio -n 500 --no-pager --output=cat')
    messages = []
    for line in out.splitlines():
        m = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[CHAT\] <server>: \[Web\] (.+?): (.+)', line)
        if m:
            messages.append({'ts': m.group(1), 'time': m.group(1)[11:], 'player': m.group(2), 'text': m.group(3), 'type': 'server'})
            continue
        m = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[CHAT\] (.+?): (.+)', line)
        if m:
            if m.group(2) == '<server>':
                continue
            messages.append({'ts': m.group(1), 'time': m.group(1)[11:], 'player': m.group(2), 'text': m.group(3), 'type': 'chat'})
            continue
        m = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[JOIN\] (.+)', line)
        if m:
            messages.append({'ts': m.group(1), 'time': m.group(1)[11:], 'player': m.group(2), 'text': 'joined the game', 'type': 'join'})
            continue
        m = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[LEAVE\] (.+?)(?:\s*\(.*\))?$', line)
        if m:
            messages.append({'ts': m.group(1), 'time': m.group(1)[11:], 'player': m.group(2), 'text': 'left the game', 'type': 'leave'})
    return jsonify({'messages': messages[-100:]})

@app.route('/api/players')
@login_required
def api_players():
    try:
        return jsonify({'ok': True, 'players': rcon_get_players()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e), 'players': []})

@app.route('/api/say', methods=['POST'])
@login_required
@role_required('user')
def api_say():
    message = request.json.get('message', '').strip()
    if not message:
        return jsonify({'ok': False, 'error': 'Empty message'}), 400
    try:
        sender = session['username']
        rcon_say(f'[Web] {sender}: {message}')
        audit('RCON_SAY', f'message={message[:80]}')
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/api/kick', methods=['POST'])
@login_required
@role_required('moderator')
def api_kick():
    player = request.json.get('player', '').strip()
    if not player:
        return jsonify({'ok': False, 'error': 'No player specified'}), 400
    try:
        rcon_kick(player)
        audit('RCON_KICK', f'player={player}')
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/api/kick-all', methods=['POST'])
@login_required
@role_required('moderator')
def api_kick_all():
    try:
        players = rcon_get_players()
        for p in players:
            rcon_kick(p)
        audit('RCON_KICK_ALL', f'count={len(players)}')
        return jsonify({'ok': True, 'kicked': len(players)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


# ── settings ─────────────────────────────────────────────────────────────────

def _parse_save_mods(data):
    buf = io.BytesIO(data)
    def r8(): return struct.unpack('<B', buf.read(1))[0]
    def r16(): return struct.unpack('<H', buf.read(2))[0]
    def rstr(): return buf.read(r8()).decode('utf-8', errors='replace')
    for _ in range(4): r16()   # format version
    r16()                       # unknown uint16
    rstr(); rstr()              # scenario name + owning mod
    buf.read(20)                # fixed header: unknown(8) + scenario-ver(5) + flags(7)
    count = r8()
    if count > 200:
        return []
    mods = []
    for _ in range(count):
        name = rstr()
        maj, minor, patch = r8(), r8(), r8()
        buf.read(4)             # build uint32
        if name and name != 'base':
            mods.append(f'{name} {maj}.{minor}.{patch}')
    return mods

def get_save_mods(path):
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            # level.dat0 = current game state (zlib-compressed), has last-saved mod versions
            for zname in names:
                if zname.endswith('level.dat0'):
                    with z.open(zname) as f:
                        compressed = f.read()
                    return _parse_save_mods(zlib.decompress(compressed))
            # fallback: level-init.dat (creation-time mod list)
            for zname in names:
                if zname.endswith('level-init.dat'):
                    with z.open(zname) as f:
                        data = f.read(8192)
                    return _parse_save_mods(data)
    except Exception:
        pass
    return []

def get_save_version(path):
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            # level.dat0 = current game state (zlib-compressed), header = last-saved version
            for name in names:
                if name.endswith('level.dat0'):
                    with z.open(name) as f:
                        compressed = f.read(128)
                    data = zlib.decompressobj().decompress(compressed, 6)
                    if len(data) >= 6:
                        major, minor, patch = struct.unpack('<HHH', data)
                        return f'{major}.{minor}.{patch}'
            # fallback: level-init.dat (creation version, only if level.dat0 absent)
            for name in names:
                if name.endswith('level-init.dat'):
                    with z.open(name) as f:
                        data = f.read(6)
                    if len(data) >= 6:
                        major, minor, patch = struct.unpack('<HHH', data)
                        return f'{major}.{minor}.{patch}'
    except Exception:
        pass
    return '?'

def load_saves():
    if not os.path.isdir(SAVES_DIR):
        return []
    files = []
    for f in os.listdir(SAVES_DIR):
        if f.endswith('.zip'):
            path = os.path.join(SAVES_DIR, f)
            files.append({
                'name': f,
                'size': round(os.path.getsize(path) / (1024*1024), 1),
                'modified': datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d %H:%M'),
                'version': get_save_version(path),
                'mods': get_save_mods(path),
            })
    files.sort(key=lambda x: x['modified'], reverse=True)
    return files

@app.route('/settings', methods=['GET', 'POST'])
@login_required
@role_required('moderator')
def settings():
    cfg = load_server_settings()
    if request.method == 'POST':
        cfg['name'] = request.form.get('name', '')
        cfg['description'] = request.form.get('description', '')
        raw_tags = request.form.get('tags', '')
        cfg['tags'] = [t.strip() for t in raw_tags.replace(',', '\n').splitlines() if t.strip()]
        cfg['game_password'] = request.form.get('game_password', '')
        cfg['max_players'] = int(request.form.get('max_players', 0) or 0)
        want_public = request.form.get('visibility_public') == 'on'
        if not isinstance(cfg.get('visibility'), dict):
            cfg['visibility'] = {}
        cfg['visibility']['public'] = want_public
        cfg['visibility']['lan'] = request.form.get('visibility_lan') == 'on'
        cfg['require_user_verification'] = request.form.get('require_user_verification') == 'on'
        cfg['auto_pause'] = request.form.get('auto_pause') == 'on'
        cfg['afk_autokick_interval'] = int(request.form.get('afk_autokick_interval', 0) or 0)
        cfg['only_admins_can_pause_the_game'] = request.form.get('only_admins_can_pause_the_game') == 'on'
        cfg['allow_commands'] = request.form.get('allow_commands', 'admins-only')
        cfg['autosave_interval'] = int(request.form.get('autosave_interval', 10) or 10)
        cfg['autosave_slots'] = int(request.form.get('autosave_slots', 5) or 5)
        cfg['autosave_only_on_server'] = request.form.get('autosave_only_on_server') == 'on'
        cfg['non_blocking_saving'] = request.form.get('non_blocking_saving') == 'on'
        cfg['research_queue_setting'] = request.form.get('research_queue_setting', 'after-victory')
        raw_admins = request.form.get('admins', '')
        cfg['admins'] = [a.strip() for a in raw_admins.splitlines() if a.strip()]
        # Copy credentials into server-settings when public listing is enabled
        if want_public:
            creds_check = load_factorio_credentials()
            if creds_check.get('username') and creds_check.get('token'):
                cfg['username'] = creds_check['username']
                cfg['token'] = creds_check['token']
            else:
                flash('Warning: public listing requires a Factorio account token. Add credentials below first.')
        save_server_settings(cfg)
        run('systemctl restart factorio')
        audit('SETTINGS_SAVED')
        flash('Settings saved and server restarted.')
        return redirect(url_for('settings'))
    creds = load_factorio_credentials()
    rcon = load_rcon_config()
    return render_template('settings.html', cfg=cfg, creds=creds, rcon=rcon, saves=load_saves(),
                           active=get_active_save(), username=session['username'])

@app.route('/settings/factorio-credentials', methods=['POST'])
@login_required
@role_required('moderator')
def settings_factorio_credentials():
    username = request.form.get('factorio_username', '').strip()
    token = request.form.get('factorio_token', '').strip()
    if not username or not token:
        flash('Factorio username and token are required.')
        return redirect(url_for('settings'))
    save_factorio_credentials(username, token)
    audit('FACTORIO_CREDENTIALS_SAVED', f'factorio_user={username}')
    flash('Factorio credentials saved.')
    return redirect(url_for('settings'))

@app.route('/settings/rcon', methods=['POST'])
@login_required
@role_required('moderator')
def settings_rcon():
    port = request.form.get('rcon_port', '27015').strip()
    password = request.form.get('rcon_password', '').strip()
    if not port.isdigit():
        flash('RCON port must be a number.')
        return redirect(url_for('settings'))
    if not password:
        flash('RCON password cannot be empty.')
        return redirect(url_for('settings'))
    try:
        with open(SERVICE_FILE) as f:
            content = f.read()
        content = re.sub(r'\s*--rcon-port\s+\S+', '', content)
        content = re.sub(r'\s*--rcon-password\s+\S+', '', content)
        content = re.sub(
            r'(ExecStart=.+?)(\s*\n)',
            rf'\1 --rcon-port {port} --rcon-password {password}\2',
            content
        )
        with open(SERVICE_FILE, 'w') as f:
            f.write(content)
        run('systemctl daemon-reload')
    except Exception as e:
        flash(f'Warning: could not update service file: {e}')
    save_rcon_config(port, password)
    run('systemctl restart factorio')
    audit('RCON_CONFIG_SAVED', f'port={port}')
    flash('RCON settings saved and server restarted.')
    return redirect(url_for('settings'))


@app.route('/settings/wipe-factorio', methods=['POST'])
@login_required
@role_required('admin')
def settings_wipe_factorio():
    confirm = request.form.get('confirm', '').strip()
    if confirm != 'WIPE':
        flash('You must type WIPE exactly to confirm.')
        return redirect(url_for('settings'))
    run('systemctl stop factorio')
    run('systemctl disable factorio')
    if os.path.exists(SERVICE_FILE):
        os.remove(SERVICE_FILE)
        run('systemctl daemon-reload')
    if os.path.exists('/opt/factorio'):
        shutil.rmtree('/opt/factorio')
    audit('WIPE_FACTORIO')
    flash('Factorio has been wiped. Use the dashboard to install a fresh version.')
    return redirect(url_for('dashboard'))


# ── saves ────────────────────────────────────────────────────────────────────

def _write_service_file(save_path):
    """Create /etc/systemd/system/factorio.service pointing at save_path."""
    content = (
        '[Unit]\n'
        'Description=Factorio Server\n'
        'After=network.target\n\n'
        '[Service]\n'
        'Type=simple\n'
        f'ExecStart={FACTORIO_BIN} --start-server {save_path}'
        f' --server-settings {SERVER_SETTINGS}'
        f' --mod-directory {MODS_DIR}\n'
        'Restart=on-failure\n'
        'RestartSec=5\n\n'
        '[Install]\n'
        'WantedBy=multi-user.target\n'
    )
    with open(SERVICE_FILE, 'w') as f:
        f.write(content)
    run('systemctl daemon-reload')
    run('systemctl enable factorio')

@app.route('/saves/new', methods=['POST'])
@login_required
@role_required('moderator')
def saves_new():
    name = (request.form.get('name', 'game').strip() or 'game')
    if not name.endswith('.zip'):
        name += '.zip'
    name = os.path.basename(name)
    os.makedirs(SAVES_DIR, exist_ok=True)
    path = os.path.join(SAVES_DIR, name)
    _, code = run(f'"{FACTORIO_BIN}" --create "{path}"')
    if code != 0 or not os.path.exists(path):
        flash('Failed to generate map. Check that Factorio is installed correctly.')
        return redirect(url_for('settings'))
    if not os.path.exists(SERVICE_FILE):
        _write_service_file(path)
    else:
        # patch existing service to point at new save
        with open(SERVICE_FILE) as f:
            content = f.read()
        content = re.sub(r'(--start-server\s+)\S+', rf'\g<1>{path}', content)
        with open(SERVICE_FILE, 'w') as f:
            f.write(content)
        run('systemctl daemon-reload')
    audit('SAVE_NEW', f'name={name}')
    flash(f'Map "{name}" generated. You can now start the server.')
    return redirect(url_for('settings'))

@app.route('/saves/download/<name>')
@login_required
@role_required('moderator')
def saves_download(name):
    path = os.path.join(SAVES_DIR, os.path.basename(name))
    if not os.path.exists(path):
        flash('Save file not found.')
        return redirect(url_for('settings'))
    audit('SAVE_DOWNLOAD', f'name={name}')
    return send_file(path, as_attachment=True, download_name=os.path.basename(name))

@app.route('/saves/switch', methods=['POST'])
@login_required
@role_required('moderator')
def saves_switch():
    name = request.form.get('name')
    path = os.path.join(SAVES_DIR, name)
    if not os.path.exists(path):
        flash('Save file not found.')
        return redirect(url_for('settings'))

    if not os.path.exists(SERVICE_FILE):
        _write_service_file(path)
    else:
        with open(SERVICE_FILE) as f:
            content = f.read()
        content = re.sub(r'(--start-server\s+)\S+', rf'\g<1>{path}', content)
        with open(SERVICE_FILE, 'w') as f:
            f.write(content)
        run('systemctl daemon-reload')
    run('systemctl restart factorio')
    audit('SAVE_SWITCH', f'name={name}')
    flash(f'Switched to {name} and restarted server.')
    return redirect(url_for('settings'))

@app.route('/saves/upload', methods=['POST'])
@login_required
@role_required('moderator')
def saves_upload():
    f = request.files.get('file')
    if not f or not f.filename.endswith('.zip'):
        flash('Please upload a .zip save file.')
        return redirect(url_for('settings'))
    os.makedirs(SAVES_DIR, exist_ok=True)
    dest = os.path.join(SAVES_DIR, os.path.basename(f.filename))
    f.save(dest)
    audit('SAVE_UPLOAD', f'name={os.path.basename(f.filename)}')
    flash(f'Uploaded {os.path.basename(f.filename)}.')
    return redirect(url_for('settings'))

@app.route('/saves/delete', methods=['POST'])
@login_required
@role_required('moderator')
def saves_delete():
    name = request.form.get('name')
    if name == get_active_save():
        flash('Cannot delete the active save.')
        return redirect(url_for('settings'))
    path = os.path.join(SAVES_DIR, name)
    if os.path.exists(path):
        os.remove(path)
        audit('SAVE_DELETE', f'name={name}')
        flash(f'Deleted {name}.')
    return redirect(url_for('settings'))


# ── users ────────────────────────────────────────────────────────────────────

@app.route('/users')
@login_required
def users():
    return render_template('users.html', users=load_users(), username=session['username'])

@app.route('/users/add', methods=['POST'])
@login_required
@role_required('moderator')
def users_add():
    new_username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    password2 = request.form.get('password2', '')
    new_role = request.form.get('role', 'user')
    if new_role not in ROLES:
        flash('Invalid role.')
        return redirect(url_for('users'))
    if not can_manage(new_role):
        flash('You cannot create a user with that role.')
        return redirect(url_for('users'))
    if not new_username or not password:
        flash('Username and password required.')
        return redirect(url_for('users'))
    if password != password2:
        flash('Passwords do not match.')
        return redirect(url_for('users'))
    all_users = load_users()
    if any(u['username'] == new_username for u in all_users):
        flash('User already exists.')
        return redirect(url_for('users'))
    all_users.append({'username': new_username, 'password': hash_pw(password), 'role': new_role})
    save_users(all_users)
    audit('USER_ADD', f'target={new_username} role={new_role}')
    flash(f'User {new_username} added.')
    return redirect(url_for('users'))

@app.route('/users/delete', methods=['POST'])
@login_required
@role_required('moderator')
def users_delete():
    target_name = request.form.get('username')
    if target_name == session['username']:
        flash('Cannot delete yourself.')
        return redirect(url_for('users'))
    target = find_user(target_name)
    if not target or not can_manage(target.get('role', 'viewer')):
        flash('You cannot delete that user.')
        return redirect(url_for('users'))
    save_users([u for u in load_users() if u['username'] != target_name])
    audit('USER_DELETE', f'target={target_name} role={target.get("role")}')
    flash(f'User {target_name} deleted.')
    return redirect(url_for('users'))

@app.route('/users/change-password', methods=['POST'])
@login_required
def users_change_password():
    target_name = request.form.get('username')
    new_pw = request.form.get('password', '')
    is_self = target_name == session['username']
    if not is_self:
        target = find_user(target_name)
        if not target or not can_manage(target.get('role', 'viewer')):
            flash('You cannot change that user\'s password.')
            return redirect(url_for('users'))
    if not new_pw:
        flash('Password cannot be empty.')
        return redirect(url_for('users'))
    all_users = load_users()
    for u in all_users:
        if u['username'] == target_name:
            u['password'] = hash_pw(new_pw)
    save_users(all_users)
    audit('USER_CHANGE_PW', f'target={target_name}')
    flash(f'Password updated for {target_name}.')
    return redirect(url_for('users'))

@app.route('/users/set-role', methods=['POST'])
@login_required
@role_required('moderator')
def users_set_role():
    target_name = request.form.get('username')
    new_role = request.form.get('role')
    if new_role not in ROLES:
        flash('Invalid role.')
        return redirect(url_for('users'))
    if target_name == session['username']:
        flash('Cannot change your own role.')
        return redirect(url_for('users'))
    target = find_user(target_name)
    if not target or not can_manage(target.get('role', 'viewer')):
        flash('You cannot change the role of that user.')
        return redirect(url_for('users'))
    if not can_manage(new_role):
        flash('You cannot assign a role equal to or higher than your own.')
        return redirect(url_for('users'))
    all_users = load_users()
    for u in all_users:
        if u['username'] == target_name:
            u['role'] = new_role
    save_users(all_users)
    audit('USER_SET_ROLE', f'target={target_name} role={new_role}')
    flash(f'{target_name} is now a {new_role}.')
    return redirect(url_for('users'))


# ── main ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    ensure_user_roles()
    app.run(host='0.0.0.0', port=8080, debug=False)
