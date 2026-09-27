import os, json, threading, re, sqlite3, time, uuid
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.path.join(BASE, 'media')
ADS_FILE  = os.path.join(BASE, 'ads.json')
DB        = os.path.join(BASE, 'cameras.db')
os.makedirs(MEDIA_DIR, exist_ok=True)

app = Flask(__name__)

CAM_RE     = re.compile(r'^CAMERA-\d{3,}$')
LK         = threading.Lock()
CLIENTS    = {}          # client_id -> {role,last_poll,events,seq,camera_id,name}
CAMS       = {}          # camera_id -> client_id
GC_TIMEOUT = 10
MAX_EVENTS = 200

# ---- ON-AIR state -----------------------------------------------------------
PROGRAM = {'rev': 0, 'source': 'slate', 'camera_id': None,
           'title': 'STANDBY', 'ad': None, 'ends_at': None}
SCORE   = {'visible': False, 'title': '', 'line1': '', 'line2': '', 'note': ''}
ADS     = []
BREAK   = {'active': False, 'token': 0, 'prev': None}

OVERLAY = {
    'header': {'enabled': False, 'text': 'WELCOME TO OUR LIVE BROADCAST', 'mode': 'scroll',
               'bg': '#b81d1d', 'color': '#ffffff', 'speed': 24, 'font_size': 16},
    'footer': {'enabled': False, 'text': 'SUBSCRIBE  ·  LIKE  ·  SHARE', 'mode': 'scroll',
               'bg': '#0b3d91', 'color': '#ffffff', 'speed': 24, 'font_size': 16,
               'position': 'above_score'},
    'logo':   {'enabled': False, 'url': '', 'height': 60, 'opacity': 1.0},
}


def ov_snapshot():
    with LK:
        return json.loads(json.dumps(OVERLAY))


# ============================ DATABASE =======================================
def db():
    c = sqlite3.connect(DB, check_same_thread=False, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS cameras(camera_id TEXT PRIMARY KEY,
            name TEXT, status TEXT DEFAULT 'offline', last_seen TEXT, created_at TEXT)''')
        c.execute("UPDATE cameras SET status='offline'")
        c.commit()


def list_cams():
    with db() as c:
        return [dict(r) for r in c.execute(
            'SELECT camera_id,name,status,last_seen FROM cameras ORDER BY camera_id')]


def upsert_cam(cid, name, status):
    now = datetime.utcnow().isoformat()
    with db() as c:
        if c.execute('SELECT 1 FROM cameras WHERE camera_id=?', (cid,)).fetchone():
            c.execute('UPDATE cameras SET name=COALESCE(?,name),status=?,last_seen=? WHERE camera_id=?',
                      (name, status, now, cid))
        else:
            c.execute('INSERT INTO cameras VALUES(?,?,?,?,?)', (cid, name or cid, status, now, now))
        c.commit()


# ============================== ADS ==========================================
def load_ads():
    global ADS
    try:
        with open(ADS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        ADS = data if isinstance(data, list) else []
    except Exception:
        ADS = []


def save_ads():
    try:
        with open(ADS_FILE, 'w', encoding='utf-8') as f:
            json.dump(ADS, f, indent=2)
    except Exception as e:
        print('! ads save failed:', e)


def get_ad(aid):
    for a in list(ADS):
        if a.get('id') == aid:
            return a
    return None


# ============================ EVENT BUS ======================================
def queue_evt(cl, evt, data):
    cl['seq'] += 1
    cl['events'].append((cl['seq'], evt, data))
    if len(cl['events']) > MAX_EVENTS:
        cl['events'] = cl['events'][-100:]


def push(tcid, evt, data):
    with LK:
        cl = CLIENTS.get(tcid)
        if cl:
            queue_evt(cl, evt, data)


def broadcast(evt, data):
    with LK:
        for cl in CLIENTS.values():
            queue_evt(cl, evt, data)


def audience_count():
    now = time.time()
    with LK:
        return sum(1 for cl in CLIENTS.values()
                   if cl.get('role') in ('viewer', 'audience')
                   and now - cl['last_poll'] < GC_TIMEOUT)


# ========================= PROGRAM / BREAK ===================================
def snapshot_program():
    with LK:
        return dict(PROGRAM)


def set_program(source, camera_id=None, title=None, ad=None, ends_at=None):
    with LK:
        PROGRAM['rev'] += 1
        PROGRAM['source']    = source
        PROGRAM['camera_id'] = camera_id
        PROGRAM['title']     = title
        PROGRAM['ad']        = ad
        PROGRAM['ends_at']   = ends_at
        snap = dict(PROGRAM)
    broadcast('program', snap)
    return snap


def kill_break():
    """Abort a running ad-break (the worker thread will notice the token change)."""
    with LK:
        BREAK['active'] = False
        BREAK['token'] += 1
        BREAK['prev']   = None


def _break_worker(ids, mytoken):
    for aid in ids:
        with LK:
            ok = BREAK['active'] and BREAK['token'] == mytoken
        if not ok:
            return
        ad = get_ad(aid)
        if not ad:
            continue
        dur = float(ad.get('duration') or 10)
        end = time.time() + dur
        set_program('ad', None, ad.get('title') or 'ADVERTISEMENT', ad, end)
        while time.time() < end:
            with LK:
                ok = BREAK['active'] and BREAK['token'] == mytoken
            if not ok:
                return
            time.sleep(0.15)
    with LK:
        if BREAK['active'] and BREAK['token'] == mytoken:
            BREAK['active'] = False
            BREAK['token'] += 1
            prev = dict(BREAK['prev'] or {})
        else:
            prev = None
    if prev:
        set_program(prev.get('source', 'slate'), prev.get('camera_id'),
                    prev.get('title'), None, None)


# ============================== GC / RESOLVE =================================
def gc():
    now = time.time()
    dead = []
    with LK:
        for cid, cl in list(CLIENTS.items()):
            if now - cl['last_poll'] > GC_TIMEOUT:
                cam = cl.get('camera_id')
                if cam:
                    CAMS.pop(cam, None)
                    dead.append(cam)
                CLIENTS.pop(cid, None)
    for cam in dead:
        upsert_cam(cam, None, 'offline')
        broadcast('camera_status', {'camera_id': cam, 'status': 'offline'})
        with LK:
            is_live = PROGRAM['source'] == 'camera' and PROGRAM['camera_id'] == cam
        if is_live:
            set_program('slate', None, 'SIGNAL LOST · ' + cam, None, None)


def resolve(to):
    with LK:
        if to and to.startswith('CAMERA-'):
            return CAMS.get(to)
        return to


# ================================ PAGES ======================================
@app.route('/')
def index():    return render_template('index.html')

@app.route('/camera')
def cp():       return render_template('camera.html')

@app.route('/control')
def control():  return render_template('control.html')

@app.route('/watch')
def watch():    return render_template('watch.html')


# ================================ API ========================================
@app.route('/api/cameras')
def ac():
    return jsonify(list_cams())


@app.route('/api/next-id')
def an():
    mx = 0
    for r in list_cams():
        m = re.match(r'^CAMERA-(\d+)$', r['camera_id'])
        if m:
            mx = max(mx, int(m.group(1)))
    return jsonify({'camera_id': 'CAMERA-%03d' % (mx + 1)})


@app.route('/api/state')
def state():
    return jsonify({'program': snapshot_program(), 'score': dict(SCORE),
                    'ads': list(ADS), 'cameras': list_cams(),
                    'viewers': audience_count(), 'overlay': ov_snapshot()})


@app.route('/health')
def h():
    return jsonify({'ok': True, 'clients': len(CLIENTS),
                    'viewers': audience_count(), 'program': snapshot_program()})


# ------------------------------- POLL ----------------------------------------
@app.route('/poll')
def poll():
    cid  = (request.args.get('id') or '').strip()
    role = request.args.get('role', '')
    if not cid:
        return jsonify({'error': 'no id'}), 400
    try:
        since = int(request.args.get('since', '0') or 0)
    except Exception:
        since = 0

    gc()
    with LK:
        cl = CLIENTS.get(cid)
        if not cl:
            cl = {'role': role, 'last_poll': 0, 'events': [], 'seq': 0,
                  'camera_id': None, 'name': None}
            CLIENTS[cid] = cl
            queue_evt(cl, 'hello', {'client_id': cid})
        cl['last_poll'] = time.time()
        if role:
            cl['role'] = role
        evts      = [e for e in cl['events'] if e[0] > since]
        new_since = cl['seq']
        prog      = dict(PROGRAM)
        scr       = dict(SCORE)
        ads       = list(ADS)

    return jsonify({
        'events':      [{'evt': e[1], 'data': e[2]} for e in evts],
        'since':       new_since,
        'cameras':     list_cams(),
        'program':     prog,
        'score':       scr,
        'ads':         ads,
        'viewers':     audience_count(),
        'overlay':     ov_snapshot(),
        'server_time': time.time(),
    })


# ----------------------------- REGISTER --------------------------------------
@app.route('/register', methods=['POST'])
def register():
    d      = request.get_json(force=True, silent=True) or {}
    cid    = (d.get('client_id') or '').strip()
    cam_id = (d.get('camera_id') or '').strip().upper()
    name   = (d.get('name') or '').strip()[:64]
    if not cid or not CAM_RE.match(cam_id):
        return jsonify({'ok': False, 'error': 'invalid payload'}), 400
    with LK:
        cl = CLIENTS.get(cid)
        if not cl:
            return jsonify({'ok': False, 'error': 'Poll first, then click CONNECT'}), 400
        other = CAMS.get(cam_id)
        if other and other != cid:
            oc = CLIENTS.get(other)
            if oc and time.time() - oc['last_poll'] < GC_TIMEOUT:
                return jsonify({'ok': False, 'error': cam_id + ' already online'}), 409
            CAMS.pop(cam_id, None)
            if oc:
                oc['camera_id'] = None
        cl['camera_id'] = cam_id
        cl['name']      = name or cam_id
        CAMS[cam_id]    = cid
    upsert_cam(cam_id, name or cam_id, 'online')
    broadcast('camera_status', {'camera_id': cam_id, 'name': name or cam_id, 'status': 'online'})
    return jsonify({'ok': True, 'camera_id': cam_id})


# ------------------------------ SIGNAL ---------------------------------------
@app.route('/signal', methods=['POST'])
def signal():
    d    = request.get_json(force=True, silent=True) or {}
    frm  = d.get('from'); to = d.get('to'); evt = d.get('event')
    data = d.get('data') or {}
    if not (frm and to and evt):
        return jsonify({'ok': False, 'error': 'missing fields'}), 400
    tgt = resolve(to)
    if not tgt:
        return jsonify({'ok': False, 'error': 'target offline: ' + str(to)}), 404
    payload = {'from': frm, **data}
    with LK:
        s = CLIENTS.get(frm)
        if s and s.get('camera_id'):
            payload['camera_id'] = s['camera_id']
    push(tgt, evt, payload)
    return jsonify({'ok': True})


# ---------------------------- DIRECTOR OPS -----------------------------------
@app.route('/api/take', methods=['POST'])
def take():
    d   = request.get_json(force=True, silent=True) or {}
    cid = (d.get('camera_id') or '').strip().upper()
    if not CAM_RE.match(cid):
        return jsonify({'ok': False, 'error': 'bad camera id'}), 400
    name = cid
    for c in list_cams():
        if c['camera_id'] == cid and c.get('name'):
            name = c['name']
    kill_break()
    p = set_program('camera', cid, name, None, None)
    return jsonify({'ok': True, 'program': p})


@app.route('/api/slate', methods=['POST'])
def slate():
    d     = request.get_json(force=True, silent=True) or {}
    title = (d.get('title') or 'STANDBY').strip()[:80] or 'STANDBY'
    kill_break()
    p = set_program('slate', None, title, None, None)
    return jsonify({'ok': True, 'program': p})


@app.route('/api/ad/play', methods=['POST'])
def ad_play():
    d  = request.get_json(force=True, silent=True) or {}
    ad = get_ad(d.get('ad_id'))
    if not ad:
        return jsonify({'ok': False, 'error': 'ad not found'}), 404
    kill_break()
    dur = float(ad.get('duration') or 0)
    p = set_program('ad', None, ad.get('title') or 'ADVERTISEMENT', ad,
                    (time.time() + dur) if dur > 0 else None)
    return jsonify({'ok': True, 'program': p})


@app.route('/api/break/start', methods=['POST'])
def break_start():
    d   = request.get_json(force=True, silent=True) or {}
    ids = d.get('ids') or [a['id'] for a in list(ADS)]
    ids = [i for i in ids if get_ad(i)]
    if not ids:
        return jsonify({'ok': False, 'error': 'no ads in playlist'}), 400
    with LK:
        if BREAK['active']:
            return jsonify({'ok': False, 'error': 'break already running'}), 409
        BREAK['active'] = True
        BREAK['token'] += 1
        mytoken = BREAK['token']
        BREAK['prev'] = {'source': PROGRAM['source'],
                         'camera_id': PROGRAM['camera_id'],
                         'title': PROGRAM['title']}
    threading.Thread(target=_break_worker, args=(ids, mytoken), daemon=True).start()
    return jsonify({'ok': True, 'count': len(ids)})


@app.route('/api/break/stop', methods=['POST'])
def break_stop():
    with LK:
        if not BREAK['active']:
            return jsonify({'ok': False, 'error': 'no break running'}), 400
        BREAK['active'] = False
        BREAK['token'] += 1
        prev = dict(BREAK['prev'] or {})
    if prev:
        set_program(prev.get('source', 'slate'), prev.get('camera_id'),
                    prev.get('title'), None, None)
    return jsonify({'ok': True})


@app.route('/api/score', methods=['POST'])
def score_set():
    d = request.get_json(force=True, silent=True) or {}
    with LK:
        for k in ('title', 'line1', 'line2', 'note'):
            if k in d:
                SCORE[k] = str(d[k])[:120]
        if 'visible' in d:
            SCORE['visible'] = bool(d['visible'])
        snap = dict(SCORE)
    broadcast('score', snap)
    return jsonify({'ok': True, 'score': snap})


@app.route('/api/overlay', methods=['POST'])
def overlay_set():
    d = request.get_json(force=True, silent=True) or {}
    with LK:
        for section in ('header', 'footer', 'logo'):
            if section in d and isinstance(d[section], dict):
                for k, v in d[section].items():
                    OVERLAY[section][k] = v
        snap = json.loads(json.dumps(OVERLAY))
    broadcast('overlay', snap)
    return jsonify({'ok': True, 'overlay': snap})


# ------------------------------- ADS CRUD ------------------------------------
@app.route('/api/ads', methods=['GET'])
def ads_list():
    return jsonify(list(ADS))


@app.route('/api/ads', methods=['POST'])
def ads_add():
    d = request.get_json(force=True, silent=True) or {}
    kind = d.get('kind')
    if kind not in ('video', 'image', 'text'):
        kind = 'text'
    ad = {
        'id':       'ad-' + uuid.uuid4().hex[:8],
        'title':    (d.get('title') or 'Ad')[:60],
        'kind':     kind,
        'url':      (d.get('url') or '').strip(),
        'text':     (d.get('text') or '')[:300],
        'bg':       (d.get('bg') or '#0b3d91'),
        'duration': max(1, min(600, int(d.get('duration') or 10))),
    }
    if kind in ('video', 'image') and not ad['url']:
        return jsonify({'ok': False, 'error': 'url required for video/image'}), 400
    with LK:
        ADS.append(ad)
    save_ads()
    return jsonify({'ok': True, 'ad': ad})


@app.route('/api/ads/<aid>', methods=['DELETE'])
def ads_del(aid):
    with LK:
        for i, a in enumerate(ADS):
            if a.get('id') == aid:
                ADS.pop(i)
                save_ads()
                return jsonify({'ok': True})
    return jsonify({'ok': False, 'error': 'not found'}), 404


@app.route('/api/upload', methods=['POST'])
def upload():
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': 'no file'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.mp4', '.webm', '.ogv', '.jpg', '.jpeg', '.png', '.gif', '.webp'):
        return jsonify({'ok': False, 'error': 'unsupported type ' + ext}), 400
    name = uuid.uuid4().hex[:12] + ext
    f.save(os.path.join(MEDIA_DIR, name))
    return jsonify({'ok': True, 'url': '/media/' + name})


@app.route('/media/<path:name>')
def media(name):
    return send_from_directory(MEDIA_DIR, name)


# ================================ MAIN =======================================
if __name__ == '__main__':
    init_db()
    load_ads()
    p = int(os.environ.get('PORT', '5050'))
    print('* BROADCAST SERVER  http://0.0.0.0:%d' % p)
    print('    /control  -> operator   /watch -> audience   /camera -> camera node')
    app.run(ssl_context=('certs/cert.pem', 'certs/key.pem'), host='0.0.0.0', port=p, threaded=True, debug=False, use_reloader=False)
