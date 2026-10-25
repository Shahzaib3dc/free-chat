"""
Direct v2 - themed realtime chat
Install:  pip install flask flask-socketio simple-websocket
Run:      python chat_server.py        then in another terminal:  ngrok http 5000
"""
import json
import os
import random
import re
import time
import uuid
from collections import defaultdict, deque
from flask import Flask, request
from flask_socketio import SocketIO, emit, disconnect

# ===================== CONFIG =====================
ROOM_PASSWORD = "123"            # everyone else logs in with this
ADMIN_USERNAME = "Shahzaib"      # default admin
ADMIN_PASSWORD = "shahzaib123"   # CHANGE THIS - needed to log in as admin (stops people faking the name)
MAX_HISTORY = 300
PORT = 5000
PERSIST = True                   # keep history + bans in chat_history.json across restarts
DB_FILE = "chat_history.json"
# ==================================================

app = Flask(__name__)
app.config['SECRET_KEY'] = uuid.uuid4().hex
socketio = SocketIO(app, max_http_buffer_size=10 * 1024 * 1024, cors_allowed_origins="*")

users = {}          # sid -> {username, is_admin, muted}
banned = set()      # lowercase usernames
messages = []       # stored messages
state = {'locked': False, 'pinned': None}
recent = defaultdict(lambda: deque(maxlen=6))   # sid -> recent send times (anti-spam)
fails = defaultdict(list)                       # ip -> failed login times


def save():
    if not PERSIST:
        return
    try:
        with open(DB_FILE, 'w', encoding='utf-8') as f:
            json.dump({'messages': messages, 'banned': list(banned)}, f)
    except OSError:
        pass


def load():
    if PERSIST and os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, encoding='utf-8') as f:
                d = json.load(f)
            messages.extend(d.get('messages', []))
            banned.update(d.get('banned', []))
        except (OSError, ValueError):
            pass


def client_ip():
    return (request.headers.get('X-Forwarded-For') or request.remote_addr or '').split(',')[0].strip()


def login_blocked():
    now = time.time()
    fails[client_ip()] = [t for t in fails[client_ip()] if now - t < 60]
    return len(fails[client_ip()]) >= 5


def fail_login(msg):
    fails[client_ip()].append(time.time())
    return emit('login_response', {'success': False, 'message': msg})


load()


def find_msg(mid):
    return next((m for m in messages if m['id'] == mid), None)


def me():
    return users.get(request.sid)


def is_admin():
    u = me()
    return bool(u and u['is_admin'])


def user_list():
    return [{'sid': s, **u} for s, u in users.items()]


def system(text):
    socketio.emit('system_message', {'text': text, 'ts': int(time.time() * 1000)})


@app.route('/')
def index():
    return HTML


@socketio.on('authenticate')
def authenticate(d):
    if login_blocked():
        return emit('login_response', {'success': False, 'message': 'Too many wrong attempts. Wait a minute.'})
    name = re.sub(r'\s+', ' ', str(d.get('username', '')).strip())[:20]
    pw = str(d.get('password', '')).strip()
    if not name or not pw:
        return emit('login_response', {'success': False, 'message': 'Fill all fields'})
    admin = name.lower() == ADMIN_USERNAME.lower()
    if admin:
        if pw != ADMIN_PASSWORD:
            return fail_login('That name is reserved (admin password required)')
        name = ADMIN_USERNAME
    elif pw != ROOM_PASSWORD:
        return fail_login('Incorrect passcode')
    if name.lower() in banned:
        return emit('login_response', {'success': False, 'message': 'You are banned'})
    if any(u['username'].lower() == name.lower() for u in users.values()):
        return emit('login_response', {'success': False, 'message': 'Name already online'})
    users[request.sid] = {'username': name, 'is_admin': admin, 'muted': False}
    emit('login_response', {'success': True, 'username': name, 'is_admin': admin})
    emit('load_history', messages)
    emit('room_locked_status', {'locked': state['locked']})
    emit('pinned', find_msg(state['pinned']) if state['pinned'] else None)
    socketio.emit('update_users', user_list())
    system(f'{name} joined')


@socketio.on('disconnect')
def on_disconnect():
    u = users.pop(request.sid, None)
    if u:
        socketio.emit('update_users', user_list())
        system(f"{u['username']} left")


@socketio.on('send_message')
def send_message(d):
    u = me()
    if not u or u['muted'] or (state['locked'] and not u['is_admin']):
        return
    now = time.time()
    q = recent[request.sid]
    if len(q) == q.maxlen and now - q[0] < 5:
        return emit('system_message', {'text': 'Slow down - too many messages', 'ts': int(now * 1000)})
    q.append(now)
    text = str(d.get('text') or '').strip()[:2000]
    if text.startswith('/roll'):
        text = f"🎲 rolled {random.randint(1, 100)} (1-100)"
    elif text.startswith('/me '):
        text = f"* {u['username']} {text[4:]}"
    elif text.startswith('/shrug'):
        text = '¯\\_(ツ)_/¯'
    image = d.get('image')
    if not (isinstance(image, str) and image.startswith('data:image/')):
        image = None
    audio = d.get('audio')
    if not (isinstance(audio, str) and audio.startswith('data:audio/') and len(audio) < 3_000_000):
        audio = None
    wave = d.get('wave') if audio else None
    wave = [max(0, min(100, int(x))) for x in wave[:200] if isinstance(x, (int, float))] if isinstance(wave, list) else None
    try:
        dur = max(0.0, min(600.0, float(d.get('dur') or 0))) if audio else 0
    except (TypeError, ValueError):
        dur = 0
    if not text and not image and not audio:
        return
    msg = {
        'id': uuid.uuid4().hex[:12], 'user': u['username'], 'is_admin': u['is_admin'],
        'text': text, 'image': image, 'audio': audio, 'wave': wave, 'dur': dur, 'ts': int(time.time() * 1000),
        'reply_to': d.get('reply_to') if find_msg(d.get('reply_to')) else None,
        'reactions': {}, 'edited': False,
    }
    messages.append(msg)
    del messages[:-MAX_HISTORY]
    socketio.emit('receive_message', msg)
    save()


def push(m):
    save()
    socketio.emit('message_updated', m)
    if state['pinned'] == m['id']:
        socketio.emit('pinned', m)


@socketio.on('edit_message')
def edit_message(d):
    u, m = me(), find_msg(d.get('msg_id'))
    text = str(d.get('text') or '').strip()[:2000]
    if u and m and text and (u['is_admin'] or m['user'] == u['username']) and m['text']:
        m['text'], m['edited'] = text, True
        push(m)


@socketio.on('delete_message')
def delete_message(d):
    u, m = me(), find_msg(d.get('msg_id'))
    if u and m and (u['is_admin'] or m['user'] == u['username']):
        messages.remove(m)
        if state['pinned'] == m['id']:
            state['pinned'] = None
            socketio.emit('pinned', None)
        socketio.emit('remove_message', {'msg_id': m['id']})
        save()


@socketio.on('react')
def react(d):
    u, m = me(), find_msg(d.get('msg_id'))
    emoji = str(d.get('emoji', ''))[:8]
    if not (u and m and emoji):
        return
    lst = m['reactions'].setdefault(emoji, [])
    if u['username'] in lst:
        lst.remove(u['username'])
        if not lst:
            del m['reactions'][emoji]
    else:
        lst.append(u['username'])
    push(m)


@socketio.on('pin_message')
def pin_message(d):
    if not is_admin():
        return
    m = find_msg(d.get('msg_id'))
    if m and state['pinned'] != m['id']:
        state['pinned'] = m['id']
    else:
        state['pinned'] = None
    socketio.emit('pinned', find_msg(state['pinned']) if state['pinned'] else None)


@socketio.on('toggle_lock')
def toggle_lock():
    if is_admin():
        state['locked'] = not state['locked']
        socketio.emit('room_locked_status', {'locked': state['locked']})


@socketio.on('clear_chat_server')
def clear_chat():
    if is_admin():
        messages.clear()
        state['pinned'] = None
        socketio.emit('clear_chat_ui')
        save()
        socketio.emit('pinned', None)


@socketio.on('announce')
def announce(d):
    t = str(d.get('text', '')).strip()[:300]
    if is_admin() and t:
        socketio.emit('announcement', {'text': t})


def target(d):
    t = users.get(d.get('sid'))
    return t if is_admin() and t and not t['is_admin'] else None


@socketio.on('kick_user')
def kick(d):
    if target(d):
        socketio.emit('kicked', {'reason': 'kicked'}, to=d['sid'])
        disconnect(d['sid'])


@socketio.on('ban_user')
def ban(d):
    t = target(d)
    if t:
        banned.add(t['username'].lower())
        save()
        socketio.emit('kicked', {'reason': 'banned'}, to=d['sid'])
        disconnect(d['sid'])


@socketio.on('mute_user')
def mute(d):
    t = target(d)
    if t:
        t['muted'] = not t['muted']
        socketio.emit('update_users', user_list())
        socketio.emit('muted_status', {'muted': t['muted']}, to=d['sid'])


@socketio.on('typing')
def typing(d):
    u = me()
    if u:
        emit('user_typing', {'user': u['username']}, broadcast=True, include_self=False)


HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<title>Direct</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root,body[data-theme=amoled]{--bg:#000;--bg2:none;--panel:#121212;--panel-op:#121212;--border:#262626;--text:#fff;--muted:#8e8e8e;--accent:#0095f6;--sent:#3797f0;--sent-t:#fff;--recv:#262626;--r:22px;--font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;--blur:0px;--glow:none;--card-r:12px}
body[data-theme=glass]{--bg:#0b0f2a;--bg2:radial-gradient(circle at 15% 20%,#7b5cff 0,transparent 40%),radial-gradient(circle at 85% 15%,#00d4ff 0,transparent 38%),radial-gradient(circle at 70% 85%,#ff4fa3 0,transparent 42%),radial-gradient(circle at 20% 90%,#27e0a3 0,transparent 35%);--panel:rgba(255,255,255,.12);--panel-op:rgba(30,30,60,.85);--border:rgba(255,255,255,.28);--recv:rgba(255,255,255,.16);--sent:rgba(255,255,255,.32);--accent:#fff;--blur:22px;--r:24px;--card-r:28px;--muted:rgba(255,255,255,.65)}
body[data-theme=cyber]{--bg:#0a0010;--bg2:linear-gradient(rgba(255,0,200,.07) 1px,transparent 1px),linear-gradient(90deg,rgba(0,255,240,.07) 1px,transparent 1px);--panel:#12001f;--panel-op:#12001f;--border:#ff00c8;--text:#00fff0;--muted:#b06cff;--accent:#fcee0a;--sent:#fcee0a;--sent-t:#000;--recv:#1d0033;--r:4px;--card-r:0;--font:"Courier New",Consolas,monospace;--glow:0 0 10px rgba(255,0,200,.6)}
body[data-theme=matrix]{--bg:#000;--bg2:none;--panel:#021002;--panel-op:#021002;--border:#00ff41;--text:#00ff41;--muted:#0a9c2a;--accent:#00ff41;--sent:#003b0f;--sent-t:#7dff9b;--recv:#001a07;--r:2px;--card-r:0;--font:"Courier New",monospace;--glow:0 0 8px rgba(0,255,65,.45)}
body[data-theme=aurora]{--bg:#06141b;--bg2:linear-gradient(120deg,#00ffa3,#03e1ff,#7b2ff7,#00ffa3);--panel:rgba(6,20,27,.7);--panel-op:#0a1d26;--border:rgba(0,255,200,.3);--accent:#00ffc8;--sent:linear-gradient(135deg,#00c9a7,#7b2ff7);--recv:rgba(255,255,255,.1);--blur:16px;--muted:#8fb8c4}
body[data-theme=sunset]{--bg:#2b0a3d;--bg2:linear-gradient(160deg,#ff512f,#dd2476 45%,#2b0a3d);--panel:rgba(30,5,40,.55);--panel-op:#2e0b3a;--border:rgba(255,200,150,.3);--accent:#ffb36b;--sent:linear-gradient(135deg,#ff9966,#ff5e62);--recv:rgba(255,255,255,.13);--blur:14px;--muted:#e9b9c8}
body[data-theme=light]{--bg:#eef1f6;--bg2:none;--panel:#fff;--panel-op:#fff;--border:#dde2ea;--text:#1b1f27;--muted:#6b7280;--accent:#4f46e5;--sent:#4f46e5;--recv:#e9ecf3;--card-r:16px}
body{background:var(--bg);background-image:var(--bg2);background-size:40px 40px,40px 40px,auto,auto;color:var(--text);font-family:var(--font);height:100dvh;display:flex;justify-content:center;align-items:center;overflow:hidden}
body[data-theme=glass],body[data-theme=aurora],body[data-theme=sunset]{background-size:cover;background-attachment:fixed}
body[data-theme=aurora]{background-size:300% 300%;animation:flow 14s ease infinite}
@keyframes flow{50%{background-position:100% 50%}}
body[data-theme=matrix]::before,body[data-theme=cyber]::after{content:"";position:fixed;inset:0;pointer-events:none;background:repeating-linear-gradient(0deg,rgba(0,0,0,.18) 0 1px,transparent 1px 3px);z-index:50}
.card{background:var(--panel);backdrop-filter:blur(var(--blur)) saturate(160%);-webkit-backdrop-filter:blur(var(--blur)) saturate(160%);border:1px solid var(--border);border-radius:var(--card-r);box-shadow:var(--glow)}
body[data-theme=glass] .card{box-shadow:0 8px 40px rgba(0,0,0,.35),inset 0 1px 0 rgba(255,255,255,.4)}
#login{width:90%;max-width:350px;padding:30px 20px;text-align:center}
#login h1{font-size:2.2rem;font-weight:300;letter-spacing:2px;margin-bottom:6px}
#login p{color:var(--muted);font-size:.85rem;margin-bottom:18px}
input.f{width:100%;padding:11px;background:transparent;border:1px solid var(--border);border-radius:calc(var(--r)/3);color:var(--text);margin-bottom:10px;font:inherit;outline:none}
input.f:focus{border-color:var(--accent)}
.btn{width:100%;padding:10px;background:var(--accent);color:#000;font-weight:700;border:none;border-radius:calc(var(--r)/2.5);cursor:pointer;font:inherit;font-weight:700}
body[data-theme=amoled] .btn,body[data-theme=light] .btn{color:#fff}
#err{color:#ff5a6a;font-size:.85rem;min-height:18px;margin-bottom:8px}
#chat{display:none;width:100%;max-width:935px;height:100dvh;flex-direction:column;position:relative;border-radius:0;overflow:hidden}
@media(min-width:768px){#chat{height:92vh;border-radius:var(--card-r)}}
.header{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-bottom:1px solid var(--border);gap:8px}
.ui{display:flex;align-items:center;gap:10px;cursor:pointer;min-width:0}
.avatar{width:38px;height:38px;border-radius:50%;flex:none;background:linear-gradient(45deg,#f09433,#dc2743,#bc1888);display:flex;align-items:center;justify-content:center;font-weight:700;text-transform:uppercase;color:#fff}
.title{font-weight:600;display:flex;align-items:center;gap:6px}
.badge{background:var(--accent);color:#000;font-size:.6rem;padding:1px 5px;border-radius:4px;font-weight:700}
body[data-theme=amoled] .badge,body[data-theme=light] .badge{color:#fff}
.online{font-size:.75rem;color:#00f5a0}
.hb{display:flex;gap:5px;flex-wrap:wrap;justify-content:flex-end}
.tb{background:var(--recv);border:1px solid var(--border);color:var(--text);padding:5px 9px;border-radius:8px;font-size:.75rem;cursor:pointer;font-family:inherit}
.tb.dn{background:#ed4956;color:#fff;border-color:#ed4956}.tb.wn{background:#f09433;color:#000;border-color:#f09433}
.pop{display:none;position:absolute;top:62px;right:12px;z-index:20;width:270px;max-height:60vh;overflow:auto;padding:12px;background:var(--panel-op);border:1px solid var(--border);border-radius:14px;box-shadow:0 8px 30px rgba(0,0,0,.5)}
.pop h4{font-size:.8rem;color:var(--muted);margin-bottom:8px;text-transform:uppercase;letter-spacing:1px}
.mem{display:flex;justify-content:space-between;align-items:center;font-size:.85rem;padding:6px 0;border-bottom:1px solid var(--border);gap:4px}
.mem .a{display:flex;gap:3px}.mem button{font-size:.65rem;padding:2px 6px;border-radius:4px;border:none;cursor:pointer;background:var(--recv);color:var(--text)}
.mem button.r{background:#ed4956;color:#fff}
.themes{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.th{height:52px;border-radius:10px;border:2px solid transparent;cursor:pointer;color:#fff;font-size:.72rem;font-weight:600;display:flex;align-items:flex-end;padding:5px;text-shadow:0 1px 3px #000}
.th.on{border-color:var(--accent)}
.row-s{display:flex;justify-content:space-between;font-size:.85rem;margin-top:10px;align-items:center}
#banner{display:none;padding:6px 14px;font-size:.8rem;border-bottom:1px solid var(--border);background:var(--recv);cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#ann{display:none;padding:8px 14px;font-size:.85rem;font-weight:600;text-align:center;background:var(--accent);color:#000}
#box{flex:1;padding:14px;overflow-y:auto;display:flex;flex-direction:column;gap:6px;scroll-behavior:smooth}
.sys{align-self:center;font-size:.72rem;color:var(--muted);padding:2px 10px}
.mr{display:flex;flex-direction:column;max-width:78%;word-break:break-word;position:relative}
.mr.s{align-self:flex-end;align-items:flex-end}.mr.r{align-self:flex-start;align-items:flex-start}
.sn{font-size:.7rem;color:var(--muted);margin:0 4px 2px;display:flex;gap:4px;align-items:center}
.bub{padding:9px 15px;border-radius:var(--r);font-size:.95rem;line-height:1.4;backdrop-filter:blur(var(--blur));-webkit-backdrop-filter:blur(var(--blur))}
.mr.s .bub{background:var(--sent);color:var(--sent-t);border-bottom-right-radius:5px}
.mr.r .bub{background:var(--recv);border-bottom-left-radius:5px}
body[data-theme=glass] .bub{border:1px solid var(--border);box-shadow:inset 0 1px 0 rgba(255,255,255,.35)}
body[data-theme=cyber] .bub,body[data-theme=matrix] .bub{border:1px solid var(--border);box-shadow:var(--glow)}
.bub a{color:inherit;text-decoration:underline}
.rq{font-size:.75rem;opacity:.8;border-left:3px solid var(--accent);padding:2px 8px;margin-bottom:5px;background:rgba(0,0,0,.18);border-radius:6px;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.img{max-width:260px;max-height:320px;border-radius:calc(var(--r)*.8);margin-top:4px;cursor:pointer;border:1px solid var(--border)}
.meta{font-size:.65rem;color:var(--muted);margin-top:2px;padding:0 4px}
.rx{display:flex;gap:4px;flex-wrap:wrap;margin-top:3px}
.rx span{font-size:.75rem;background:var(--recv);border:1px solid var(--border);border-radius:12px;padding:1px 7px;cursor:pointer}
.rx span.me{border-color:var(--accent)}
.tools{display:none;position:absolute;top:-14px;background:var(--panel-op);border:1px solid var(--border);border-radius:14px;padding:2px 4px;gap:2px;z-index:5;white-space:nowrap}
.mr.s .tools{right:0}.mr.r .tools{left:0}
.mr:hover .tools,.mr.tap .tools{display:flex}
.tools button{background:none;border:none;cursor:pointer;font-size:.85rem;padding:2px 4px;color:var(--text)}
#typ{font-size:.75rem;color:var(--muted);padding:0 16px 4px;height:18px;font-style:italic}
#rbar{display:none;padding:6px 16px;font-size:.78rem;border-top:1px solid var(--border);justify-content:space-between;color:var(--muted)}
.ib{padding:10px 14px;border-top:1px solid var(--border);position:relative}
.iw{display:flex;align-items:center;background:var(--recv);border:1px solid var(--border);border-radius:calc(var(--r) + 4px);padding:3px 6px 3px 12px}
#msg{flex:1;background:transparent;border:none;color:var(--text);outline:none;font:inherit;font-size:.95rem;padding:8px 0;min-width:0}
.ab{background:none;border:none;color:var(--accent);font-weight:700;cursor:pointer;padding:6px 10px;font:inherit;font-weight:700}
.ab:disabled{opacity:.3}
.ic{background:none;border:none;cursor:pointer;padding:5px;font-size:1.15rem}
#emo{display:none;position:absolute;bottom:62px;left:14px;background:var(--panel-op);border:1px solid var(--border);border-radius:14px;padding:8px;width:244px;flex-wrap:wrap;gap:2px;z-index:6}
#emo span{font-size:1.3rem;cursor:pointer;padding:3px}
.card{position:relative;z-index:1}
#fx{position:fixed;inset:0;width:100%;height:100%;pointer-events:none;z-index:0}
.mention{font-weight:700;opacity:.95;text-decoration:underline dotted}
.mention.me{background:rgba(255,220,0,.35);border-radius:4px;padding:0 3px;text-decoration:none}
audio{max-width:250px;height:36px;margin-top:4px}
#sq{margin:0;border:none;border-bottom:1px solid var(--border);border-radius:0}
#mic.rec{animation:pulse 1s infinite}
@keyframes pulse{50%{opacity:.35}}
.i{width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;flex:none;display:block}
.tb{background:transparent;border:none;display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;padding:0;border-radius:50%}
.tb:hover{background:var(--recv)}.tb.dn,.tb.dn:hover{background:transparent;color:#ed4956}.tb.wn,.tb.wn:hover{background:transparent;color:#f5a623}
.sel{background:var(--recv);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:6px 10px;font:inherit;font-size:.8rem;cursor:pointer}
.ic{color:var(--text);display:flex;align-items:center}#mic.rec{color:#ed4956}
.avatar{border:2px solid transparent;color:var(--text);background:linear-gradient(var(--panel-op),var(--panel-op)) padding-box,linear-gradient(45deg,#f09433,#dc2743,#bc1888) border-box}
.online::before,.dot{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:#00f5a0;margin-right:5px}
body[data-theme=amoled] .mr.s .bub,body[data-theme=light] .mr.s .bub{background:linear-gradient(135deg,#7a5cff,#3797f0 55%,#0095f6);background-attachment:fixed;color:#fff}
.mr.r{margin-left:36px}
.av2{position:absolute;left:-34px;bottom:16px;width:26px;height:26px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:.7rem;font-weight:700;color:#fff;text-transform:uppercase}
.tools .i{width:16px;height:16px}.rq{cursor:pointer}
.td{display:inline-flex;gap:3px;margin-left:5px}.td i{width:4px;height:4px;border-radius:50%;background:currentColor;animation:bl 1.2s infinite}
.td i:nth-child(2){animation-delay:.2s}.td i:nth-child(3){animation-delay:.4s}@keyframes bl{50%{opacity:.2}}
#fab{display:none;position:absolute;right:16px;bottom:88px;width:40px;height:40px;border-radius:50%;background:var(--panel-op);border:1px solid var(--border);color:var(--text);cursor:pointer;align-items:center;justify-content:center;z-index:5}
#fab b{position:absolute;top:-6px;right:-4px;background:var(--accent);color:#000;font-size:.65rem;border-radius:9px;padding:0 5px}
#lb{display:none;position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.92);align-items:center;justify-content:center;cursor:zoom-out}
#lb img{max-width:96vw;max-height:92vh;border-radius:8px}
@media(max-width:600px){
.pop{position:fixed;left:0;right:0;top:auto;bottom:0;width:auto;max-height:65vh;border-radius:20px 20px 0 0;padding-bottom:calc(14px + env(safe-area-inset-bottom))}
.mr{max-width:88%}.img{max-width:68vw}.header{padding:8px 10px;padding-top:calc(8px + env(safe-area-inset-top))}
.hb{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;gap:0}.tb{width:34px;height:34px}
.ib{padding-bottom:calc(10px + env(safe-area-inset-bottom))}#msg{font-size:16px}#box{padding:10px}}
body[data-theme=whatsapp]{--bg:#0b141a;--bg2:none;--panel:#111b21;--panel-op:#111b21;--border:#222d34;--text:#e9edef;--muted:#8696a0;--accent:#00a884;--sent:#005c4b;--recv:#202c33;--r:10px;--card-r:12px}
body[data-theme=telegram]{--bg:#0e1621;--bg2:none;--panel:#17212b;--panel-op:#17212b;--border:#0e1621;--text:#fff;--muted:#6d7f8f;--accent:#5eb5f7;--sent:#2b5278;--recv:#182533;--r:16px;--card-r:10px}
body[data-theme=synthwave]{--bg:#1a0033;--bg2:linear-gradient(rgba(255,42,109,.13) 1px,transparent 1px),linear-gradient(90deg,rgba(5,217,232,.13) 1px,transparent 1px),linear-gradient(180deg,#1a0033,#3a0a5e 65%,#7a1b6b);--panel:rgba(26,0,51,.82);--panel-op:#240045;--border:#ff2a6d;--text:#f5e9ff;--muted:#b78ad8;--accent:#05d9e8;--sent:linear-gradient(135deg,#ff2a6d,#7b2ff7);--recv:#2d0a52;--r:14px;--card-r:14px;--glow:0 0 22px rgba(255,42,109,.35);background-size:40px 40px,40px 40px,100% 100%}
body[data-theme=ocean]{--bg:#021b3a;--bg2:linear-gradient(160deg,#021b3a,#065a82 60%,#1c92b8);--panel:rgba(2,27,58,.55);--panel-op:#06304f;--border:rgba(120,220,255,.28);--text:#eaf8ff;--muted:#8fc4d9;--accent:#4cc9f0;--sent:linear-gradient(135deg,#00b4d8,#0077b6);--recv:rgba(255,255,255,.12);--blur:16px;--r:20px;background-size:cover;background-attachment:fixed}
body[data-theme=forest]{--bg:#0f1f17;--bg2:none;--panel:#16281d;--panel-op:#16281d;--border:#244230;--text:#e6f4ea;--muted:#8db79a;--accent:#7bd389;--sent:#2d6a4f;--recv:#1f3a2a;--r:18px}
body[data-theme=rose]{--bg:#ffe8ee;--bg2:linear-gradient(160deg,#ffe8ee,#ffd3e0);--panel:rgba(255,255,255,.85);--panel-op:#fff;--border:#f5c2d0;--text:#3a1f27;--muted:#a1707f;--accent:#e5446d;--sent:linear-gradient(135deg,#f06292,#e5446d);--recv:#ffe0e8;--blur:12px;--r:20px;--card-r:20px;background-size:cover;background-attachment:fixed}
body[data-theme=dracula]{--bg:#282a36;--bg2:none;--panel:#21222c;--panel-op:#21222c;--border:#44475a;--text:#f8f8f2;--muted:#8b93b8;--accent:#bd93f9;--sent:#6a4fb3;--recv:#343746;--r:14px}
body[data-theme=rose] .btn,body[data-theme=whatsapp] .btn,body[data-theme=rose] .badge,body[data-theme=whatsapp] .badge{color:#fff}
body[data-bub=wa]{--r:10px}body[data-bub=wa] .mr.s .bub{border-top-right-radius:0;border-bottom-right-radius:10px}body[data-bub=wa] .mr.r .bub{border-top-left-radius:0;border-bottom-left-radius:10px}
body[data-bub=tg]{--r:16px}body[data-bub=tg] .mr.s .bub{border-bottom-right-radius:4px}body[data-bub=sharp]{--r:4px}
body.doodle #box{background-image:radial-gradient(rgba(255,255,255,.07) 1.3px,transparent 1.5px);background-size:22px 22px}
body[data-theme=light].doodle #box,body[data-theme=rose].doodle #box{background-image:radial-gradient(rgba(0,0,0,.09) 1.3px,transparent 1.5px)}
.sys.day{background:var(--recv);border-radius:10px;padding:3px 12px;margin:8px 0;font-weight:600}
.bub.big{background:transparent!important;border:none!important;box-shadow:none!important;font-size:2.4rem;padding:0 4px;line-height:1.1}
#dlg{display:none;position:fixed;inset:0;z-index:200;background:rgba(0,0,0,.55);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);align-items:center;justify-content:center;padding:20px}
.dbox{width:100%;max-width:330px;background:var(--panel-op);color:var(--text);border:1px solid var(--border);border-radius:18px;padding:22px 18px 0;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.5);animation:pp .18s ease;overflow:hidden}
@keyframes pp{from{transform:scale(.92);opacity:0}}
.dt{font-weight:700;font-size:1.05rem;margin-bottom:6px}.dt:empty{display:none}
.dm{color:var(--muted);font-size:.9rem;margin-bottom:16px;white-space:pre-wrap;word-break:break-word}.dm:empty{display:none}
.da{display:flex;flex-direction:column;margin:0 -18px}
.dbtn{background:none;border:none;border-top:1px solid var(--border);color:var(--text);padding:14px;font:inherit;cursor:pointer}
.dbtn:hover{background:var(--recv)}.dbtn.pri{color:var(--accent);font-weight:700}.dbtn.dan{color:#ed4956;font-weight:700}
#toasts{position:fixed;top:14px;left:50%;transform:translateX(-50%);z-index:300;display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none}
.toast{background:var(--panel-op);color:var(--text);border:1px solid var(--border);padding:9px 16px;border-radius:20px;font-size:.85rem;box-shadow:0 6px 24px rgba(0,0,0,.4);animation:pp .2s}
#recui{display:none;align-items:center;gap:12px;background:var(--recv);border:1px solid var(--border);border-radius:28px;padding:5px 6px 5px 10px}
.rc-x{color:#ed4956;background:none;border:none;cursor:pointer}.rd{width:10px;height:10px;border-radius:50%;background:#ed4956;animation:pulse 1s infinite;flex:none}
#rt{font-variant-numeric:tabular-nums;min-width:34px}#rw{flex:1;height:30px;display:flex;align-items:center;justify-content:flex-end;gap:2px;overflow:hidden}
#rw i{flex:none;width:3px;border-radius:2px;background:var(--accent);min-height:3px}
.rc-send{width:40px;height:40px;border-radius:50%;border:none;background:#00a884;color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;flex:none}
.vbub{padding:8px 12px 6px!important;min-width:min(250px,72vw)}.vm{display:flex;align-items:center;gap:10px}
.vp{width:38px;height:38px;border-radius:50%;border:none;cursor:pointer;display:flex;align-items:center;justify-content:center;background:#fff;color:#222;flex:none}
.mr.r .vp{background:var(--accent);color:#000}.vwrap{flex:1;min-width:0}
.vw{display:flex;align-items:center;gap:2px;height:30px;cursor:pointer}.vw i{flex:1;min-width:2px;border-radius:2px;background:currentColor;opacity:.4;transition:opacity .15s}.vw i.on{opacity:1}
.vt{display:flex;justify-content:space-between;align-items:center;font-size:.68rem;opacity:.9;margin-top:1px}
.vs{background:rgba(0,0,0,.28);color:inherit;border:none;border-radius:10px;font-size:.65rem;padding:1px 8px;cursor:pointer}
::-webkit-scrollbar{width:6px}::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
</style></head><body data-theme="amoled">
<canvas id="fx"></canvas>
<svg width="0" height="0" style="position:absolute"><defs>
<symbol id="i-send" viewBox="0 0 24 24"><path d="M22 3 11 14M22 3l-7 19-4-8-8-4z"/></symbol>
<symbol id="i-img" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 21"/></symbol>
<symbol id="i-mic" viewBox="0 0 24 24"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v4"/></symbol>
<symbol id="i-stop" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2"/></symbol>
<symbol id="i-search" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></symbol>
<symbol id="i-users" viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/></symbol>
<symbol id="i-cog" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9 7 7M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1"/></symbol>
<symbol id="i-lock" viewBox="0 0 24 24"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></symbol>
<symbol id="i-unlock" viewBox="0 0 24 24"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 7-2.6"/></symbol>
<symbol id="i-mega" viewBox="0 0 24 24"><path d="M3 11v2a1 1 0 0 0 1 1h3l8 5V5L7 10H4a1 1 0 0 0-1 1zM19 8a5 5 0 0 1 0 8"/></symbol>
<symbol id="i-dl" viewBox="0 0 24 24"><path d="M12 3v12m0 0-4-4m4 4 4-4M4 21h16"/></symbol>
<symbol id="i-trash" viewBox="0 0 24 24"><path d="M3 6h18M8 6V4h8v2M6 6l1 15h10l1-15"/></symbol>
<symbol id="i-edit" viewBox="0 0 24 24"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></symbol>
<symbol id="i-reply" viewBox="0 0 24 24"><path d="M9 14 4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 6 6v3"/></symbol>
<symbol id="i-pin" viewBox="0 0 24 24"><path d="M12 17v5M9 3h6l-1 7 3 3v2H7v-2l3-3z"/></symbol>
<symbol id="i-x" viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></symbol>
<symbol id="i-smile" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M8 14s1.5 2 4 2 4-2 4-2M9 9h.01M15 9h.01"/></symbol>
<symbol id="i-copy" viewBox="0 0 24 24"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></symbol>
<symbol id="i-down" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></symbol>
<symbol id="i-play" viewBox="0 0 24 24"><path d="M7 4v16l13-8z" fill="currentColor"/></symbol>
<symbol id="i-pause" viewBox="0 0 24 24"><path d="M6 4h4v16H6zM14 4h4v16h-4z" fill="currentColor"/></symbol>
</defs></svg>
<div id="lb" onclick="this.style.display='none'"><img alt=""></div>
<div id="dlg"><div class="dbox"><div class="dt" id="dt"></div><div class="dm" id="dm"></div><input class="f" id="di" style="display:none;margin:0 0 14px"><div class="da"><button class="dbtn pri" id="dok">OK</button><button class="dbtn" id="dc">Cancel</button></div></div></div>
<div id="toasts"></div>

<div id="login" class="card">
  <h1>Direct</h1><p>Secure Workspace Node</p>
  <div id="err"></div>
  <input class="f" id="u" placeholder="Username" autocomplete="off" maxlength="20">
  <input class="f" id="p" type="password" placeholder="Passcode" onkeypress="if(event.key==='Enter')login()">
  <button class="btn" onclick="login()">Log In</button>
  <div class="row-s" style="justify-content:center;margin-top:16px"><select id="lt" class="sel" onchange="setTheme(this.value)"></select></div>
</div>

<div id="chat" class="card">
  <div class="header">
    <div class="ui" onclick="pop('mem')">
      <div class="avatar" id="av">I</div>
      <div><div class="title">Secure Chat <span id="ab" class="badge" style="display:none">ADMIN</span></div><div class="online" id="oc">1 online</div></div>
    </div>
    <div class="hb">
      <button class="tb wn" id="lk" style="display:none" title="Lock room" onclick="socket.emit('toggle_lock')"><svg class="i"><use href="#i-lock"/></svg></button>
      <button class="tb" id="an" style="display:none" title="Announce" onclick="announce()"><svg class="i"><use href="#i-mega"/></svg></button>
      <button class="tb" id="ex" style="display:none" title="Export chat" onclick="exportChat()"><svg class="i"><use href="#i-dl"/></svg></button>
      <button class="tb dn" id="wp" style="display:none" title="Wipe chat" onclick="wipe()"><svg class="i"><use href="#i-trash"/></svg></button>
      <button class="tb" title="Search" onclick="toggleSearch()"><svg class="i"><use href="#i-search"/></svg></button>
      <button class="tb" title="Settings" onclick="pop('set')"><svg class="i"><use href="#i-cog"/></svg></button>
      <button class="tb" title="Members" onclick="pop('mem')"><svg class="i"><use href="#i-users"/></svg></button>
    </div>
  </div>
  <div class="pop" id="mem"><h4>Connected nodes</h4><div id="ml"></div></div>
  <div class="pop" id="set"><h4>Theme</h4><div class="themes" id="tg"></div>
    <div class="row-s"><span>Sound</span><input type="checkbox" id="snd" checked onchange="localStorage.snd=this.checked?1:0"></div>
    <div class="row-s"><span>Accent</span><input type="color" id="ac" value="#0095f6" onchange="setAccent(this.value)"></div>
    <div class="row-s"><span>Text size</span><input type="range" id="fs" min="13" max="20" value="16" oninput="setFs(this.value)"></div>
    <div class="row-s"><span>Bubbles</span><select class="sel" id="bb" onchange="setBub(this.value)"><option value="round">Instagram</option><option value="wa">WhatsApp</option><option value="tg">Telegram</option><option value="sharp">Sharp</option></select></div>
    <div class="row-s"><span>Dot wallpaper</span><input type="checkbox" id="dd" onchange="setDoodle(this.checked)"></div>
    <div class="row-s"><button class="sel" onclick="resetLook()">Reset look</button><span style="color:var(--muted);font-size:.7rem">/roll /me /shrug</span></div></div>
  <div id="ann"></div>
  <div id="banner" onclick="goPinned()"></div>
  <input class="f" id="sq" placeholder="Search messages..." style="display:none" oninput="search()">
  <div id="box"></div>
  <button id="fab" onclick="toBottom()"><svg class="i"><use href="#i-down"/></svg><b id="fn" style="display:none">0</b></button>
  <div id="typ"></div>
  <div id="rbar"><span id="rtxt"></span><span style="cursor:pointer;display:flex" onclick="setReply(null)"><svg class="i"><use href="#i-x"/></svg></span></div>
  <div class="ib">
    <div id="emo"></div>
    <div id="recui"><button class="ic rc-x" title="Cancel" onclick="micStop(true)"><svg class="i"><use href="#i-trash"/></svg></button><span class="rd"></span><span id="rt">0:00</span><div id="rw"></div><button class="rc-send" title="Send voice note" onclick="micStop(false)"><svg class="i"><use href="#i-send"/></svg></button></div>
    <div class="iw" id="iw">
      <button class="ic" onclick="toggleEmo()"><svg class="i"><use href="#i-smile"/></svg></button>
      <label class="ic"><svg class="i"><use href="#i-img"/></svg><input type="file" id="fi" accept="image/*" style="display:none" onchange="pickImg(this.files[0])"></label>
      <button class="ic" id="mic" onclick="mic()"><svg class="i"><use href="#i-mic"/></svg></button>
      <input id="msg" placeholder="Message..." autocomplete="off" maxlength="2000" oninput="onType()" onkeypress="if(event.key==='Enter'){event.preventDefault();send()}">
      <button class="ab" id="sb" onclick="send()" disabled>Send</button>
    </div>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.5.4/socket.io.min.js"></script>
<script>
const socket=io();
const THEMES=[['amoled','AMOLED','#000,#262626'],['glass','Liquid Glass','#7b5cff,#00d4ff,#ff4fa3'],['cyber','Cyberpunk','#fcee0a,#ff00c8,#00fff0'],['matrix','Matrix','#000,#00ff41'],['aurora','Aurora','#00ffa3,#7b2ff7'],['sunset','Sunset','#ff512f,#dd2476'],['light','Light','#eef1f6,#4f46e5'],['whatsapp','WhatsApp','#005c4b,#111b21'],['telegram','Telegram','#2b5278,#17212b'],['synthwave','Synthwave','#ff2a6d,#7b2ff7,#05d9e8'],['ocean','Ocean','#021b3a,#1c92b8'],['forest','Forest','#2d6a4f,#0f1f17'],['rose','Rose','#ffd3e0,#e5446d'],['dracula','Dracula','#282a36,#bd93f9']];
const EMO='😀😂🤣😊😍😘😎🤔😅😭😡🥳👍👎👏🙏💪🔥❤️💯✨🎉👀🤝😴🤯'.match(/\p{Extended_Pictographic}\uFE0F?/gu);
let me='',admin=false,locked=false,muted=false,replyTo=null,pinnedId=null,lastType=0;
const msgs={},typers={};let unread=0;

const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const $=id=>document.getElementById(id);
const fmt=ts=>new Date(ts).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
const linkify=t=>esc(t).replace(/(https?:\/\/[^\s<]+)/g,'<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>')
  .replace(/(^|\s)@(\w{1,20})/g,(a,b,n)=>`${b}<span class="mention${n.toLowerCase()===me.toLowerCase()?' me':''}">@${n}</span>`);

/* ---------- themes ---------- */
function setTheme(t){document.body.dataset.theme=t;localStorage.theme=t;$('lt').value=t;document.querySelectorAll('.th').forEach(e=>e.classList.toggle('on',e.dataset.t===t))}
THEMES.forEach(([k,n,c])=>{
  $('lt').innerHTML+=`<option value="${k}">${n}</option>`;
  const d=document.createElement('div');d.className='th';d.dataset.t=k;d.textContent=n;
  d.style.background=`linear-gradient(135deg,${c})`;d.onclick=()=>setTheme(k);$('tg').appendChild(d);
});
setTheme(localStorage.theme||'glass');
$('snd').checked=localStorage.snd!=='0';
$('emo').innerHTML=EMO.map(e=>`<span>${e}</span>`).join('');
$('emo').onclick=e=>{if(e.target.tagName==='SPAN'){$('msg').value+=e.target.textContent;$('msg').focus();onType()}};

function pop(id){['mem','set'].forEach(i=>{const e=$(i);e.style.display=(i===id&&e.style.display!=='block')?'block':'none'})}
function toggleEmo(){$('emo').style.display=$('emo').style.display==='flex'?'none':'flex'}

/* ---------- audio / title ---------- */
function beep(){if(!$('snd').checked)return;try{const c=new(window.AudioContext||window.webkitAudioContext)(),o=c.createOscillator(),g=c.createGain();o.frequency.value=660;g.gain.value=.08;o.connect(g);g.connect(c.destination);o.start();o.stop(c.currentTime+.1)}catch(e){}}
document.addEventListener('visibilitychange',()=>{if(!document.hidden){unread=0;document.title='Direct'}});
function notify(){beep();if(document.hidden){unread++;document.title=`(${unread}) Direct`}}

/* ---------- login ---------- */
function login(){
  const u=$('u').value.trim(),p=$('p').value.trim();
  if(!u||!p){$('err').innerText='Fill all fields';return}
  socket.emit('authenticate',{username:u,password:p});
}
socket.on('login_response',d=>{
  if(!d.success){$('err').innerText=d.message;return}
  me=d.username;admin=d.is_admin;
  $('login').style.display='none';$('chat').style.display='flex';$('av').innerText=me[0];
  if(admin){['ab','lk','an','ex','wp'].forEach(i=>$(i).style.display='')}
  $('msg').focus();
});
socket.on('kicked',async d=>{await ui.alert(d.reason==='banned'?'An admin banned you from this room.':'An admin removed you from this room.',d.reason==='banned'?'You were banned':'You were kicked');location.reload()});
socket.on('muted_status',d=>{muted=d.muted;refreshInput();addSys(muted?'You were muted by admin':'You were unmuted')});

/* ---------- users ---------- */
socket.on('update_users',list=>{
  $('oc').innerText=`${list.length} online`;
  $('ml').innerHTML=list.map(u=>`<div class="mem"><span><i class="dot"></i>${esc(u.username)} ${u.is_admin?'<span class="badge">ADMIN</span>':''} ${u.muted?'<small style="color:var(--muted)">muted</small>':''}</span>
    ${admin&&!u.is_admin?`<span class="a"><button data-a="mute_user" data-s="${esc(u.sid)}">${u.muted?'Unmute':'Mute'}</button><button data-a="kick_user" data-s="${esc(u.sid)}" class="r">Kick</button><button data-a="ban_user" data-s="${esc(u.sid)}" class="r">Ban</button></span>`:''}</div>`).join('');
});
$('ml').onclick=async e=>{const b=e.target.closest('button');if(!b)return;const a=b.dataset.a,n=b.closest('.mem').firstElementChild.textContent.replace('muted','').trim(),ban=a==='ban_user';if(a==='mute_user'||await ui.confirm(ban?'They will not be able to join again.':'They can rejoin with the passcode.',{title:(ban?'Ban ':'Kick ')+n+'?',ok:ban?'Ban':'Kick',danger:true}))socket.emit(a,{sid:b.dataset.s})};

socket.on('room_locked_status',d=>{locked=d.locked;$('lk').querySelector('use').setAttribute('href',locked?'#i-unlock':'#i-lock');$('lk').title=locked?'Unlock room':'Lock room';refreshInput()});
function refreshInput(){
  const off=muted||(locked&&!admin);
  $('msg').disabled=off;$('msg').placeholder=muted?'You are muted':locked&&!admin?'Room locked by admin':'Message...';
}

/* ---------- messages ---------- */
function render(m,scroll=true){
  const old=$('row-'+m.id);const s=m.user===me;
  const row=document.createElement('div');row.id='row-'+m.id;row.dataset.id=m.id;row.className='mr '+(s?'s':'r');
  const q=m.reply_to&&msgs[m.reply_to];
  const rx=Object.entries(m.reactions||{}).map(([e,l])=>`<span data-e="${esc(e)}" class="${l.includes(me)?'me':''}" title="${esc(l.join(', '))}">${esc(e)} ${l.length}</span>`).join('');
  const can=admin||s;
  row.innerHTML=(s?'':`<div class="av2" style="background:${hue(m.user)}">${esc(m.user[0])}</div><div class="sn">${esc(m.user)} ${m.is_admin?'<span class="badge">ADMIN</span>':''}</div>`)+
    (m.text||q?`<div class="bub${!q&&/^(\p{Extended_Pictographic}\uFE0F?){1,3}$/u.test((m.text||'').replace(/\s/g,''))?' big':''}">${m.reply_to?`<div class="rq" data-q="${esc(m.reply_to)}">${q?esc(q.user)+': '+esc((q.text||'Photo').slice(0,60)):'original message'}</div>`:''}${linkify(m.text||'')}</div>`:'')+
    (m.image?`<img class="img" src="${esc(m.image)}">`:'')+
    (m.audio?`<div class="bub vbub">${voiceHTML(m)}</div>`:'')+
    `<div class="meta">${fmt(m.ts)}${m.edited?' · edited':''}</div>${rx?`<div class="rx">${rx}</div>`:''}`+
    `<div class="tools">${['👍','❤️','😂','🔥'].map(e=>`<button data-r="${e}">${e}</button>`).join('')}<button data-act="reply" title="Reply">${ic('reply')}</button><button data-act="copy" title="Copy">${ic('copy')}</button>${can&&m.text?`<button data-act="edit" title="Edit">${ic('edit')}</button>`:''}${can?`<button data-act="del" title="Delete">${ic('trash')}</button>`:''}${admin?`<button data-act="pin" title="Pin">${ic('pin')}</button>`:''}</div>`;
  msgs[m.id]=m;
  const ov=old&&old.querySelector('.vm'),nv=row.querySelector('.vm');
  if(ov&&nv)nv.replaceWith(ov);else row.querySelectorAll('audio').forEach(initAudio);
  if(old)old.replaceWith(row);else{const dk=new Date(m.ts).toDateString();if(dk!==lastDay){lastDay=dk;const sp=document.createElement('div');sp.className='sys day';sp.textContent=dayLabel(m.ts);$('box').appendChild(sp)}$('box').appendChild(row)}
  if(scroll&&!old)$('box').scrollTop=$('box').scrollHeight;
}
$('box').onclick=e=>{
  const row=e.target.closest('.mr');if(!row)return;const id=row.dataset.id,m=msgs[id];
  if(voiceClick(e))return;
  if(e.target.matches('.img'))return openLb(e.target.src);
  const rq=e.target.closest('.rq');if(rq){const t=$('row-'+rq.dataset.q);if(t)t.scrollIntoView({behavior:'smooth',block:'center'});return}
  const r=e.target.closest('[data-r]');if(r)return socket.emit('react',{msg_id:id,emoji:r.dataset.r});
  const x=e.target.closest('.rx span');if(x)return socket.emit('react',{msg_id:id,emoji:x.dataset.e});
  const a=e.target.closest('[data-act]')?.dataset.act;
  if(a==='reply')setReply(m);
  else if(a==='copy')navigator.clipboard&&navigator.clipboard.writeText(m.text||'');
  else if(a==='edit'){ui.prompt('Edit message',{value:m.text,ok:'Save'}).then(t=>{if(t&&t.trim())socket.emit('edit_message',{msg_id:id,text:t})})}
  else if(a==='del'){ui.confirm('This message will be removed for everyone.',{title:'Delete message?',ok:'Delete',danger:true}).then(y=>y&&socket.emit('delete_message',{msg_id:id}))}
  else if(a==='pin')socket.emit('pin_message',{msg_id:id});
  else{document.querySelectorAll('.mr.tap').forEach(n=>n!==row&&n.classList.remove('tap'));row.classList.toggle('tap')}
};
function addSys(t){const d=document.createElement('div');d.className='sys';d.textContent=t;$('box').appendChild(d);$('box').scrollTop=$('box').scrollHeight}
socket.on('system_message',d=>addSys(d.text));
socket.on('receive_message',m=>{const near=atBottom()||m.user===me;render(m,near);if(!near){newCount++;$('fn').textContent=newCount;$('fn').style.display='block';$('fab').style.display='flex'}if(m.user!==me)notify()});
socket.on('message_updated',m=>render(m,false));
socket.on('remove_message',d=>{$('row-'+d.msg_id)?.remove();delete msgs[d.msg_id]});
socket.on('load_history',l=>{$('box').innerHTML='';lastDay='';l.forEach(m=>msgs[m.id]=m);l.forEach(m=>render(m,false));$('box').scrollTop=$('box').scrollHeight});
socket.on('clear_chat_ui',()=>{$('box').innerHTML='';lastDay='';Object.keys(msgs).forEach(k=>delete msgs[k])});
socket.on('pinned',m=>{
  pinnedId=m?m.id:null;const b=$('banner');
  b.style.display=m?'block':'none';if(m)b.innerHTML='<svg class="i" style="display:inline;width:14px;height:14px;vertical-align:-2px"><use href="#i-pin"/></svg> '+esc(m.user)+': '+esc(m.text||'Photo');
});
function goPinned(){const r=$('row-'+pinnedId);if(r){r.scrollIntoView({behavior:'smooth',block:'center'});r.style.outline='2px solid var(--accent)';setTimeout(()=>r.style.outline='',1500)}}
socket.on('announcement',d=>{const a=$('ann');a.textContent=d.text;a.style.display='block';notify();setTimeout(()=>a.style.display='none',12000)});

/* ---------- typing ---------- */
function onType(){
  $('sb').disabled=!$('msg').value.trim();
  if(Date.now()-lastType>1200){lastType=Date.now();socket.emit('typing')}
}
socket.on('user_typing',d=>{
  clearTimeout(typers[d.user]);typers[d.user]=setTimeout(()=>{delete typers[d.user];showTyp()},2500);showTyp();
});
function showTyp(){const n=Object.keys(typers);$('typ').innerHTML=n.length?esc(n.length>2?'Several people are typing':n.join(', ')+(n.length>1?' are':' is')+' typing')+'<span class="td"><i></i><i></i><i></i></span>':''}

/* ---------- send ---------- */
function setReply(m){replyTo=m;$('rbar').style.display=m?'flex':'none';if(m){$('rtxt').textContent='Replying to '+m.user+': '+(m.text||'image').slice(0,50);$('msg').focus()}}
function send(){
  const t=$('msg').value.trim();if(!t)return;
  socket.emit('send_message',{text:t,reply_to:replyTo?replyTo.id:null});
  $('msg').value='';$('sb').disabled=true;setReply(null);$('emo').style.display='none';
}
function pickImg(f){
  if(!f||!f.type.startsWith('image/'))return;
  const r=new FileReader();
  r.onload=e=>{const i=new Image();i.onload=()=>{
    const s=Math.min(1,1280/Math.max(i.width,i.height)),c=document.createElement('canvas');
    c.width=i.width*s;c.height=i.height*s;c.getContext('2d').drawImage(i,0,0,c.width,c.height);
    socket.emit('send_message',{text:'',image:c.toDataURL('image/jpeg',.82),reply_to:replyTo?replyTo.id:null});setReply(null);
  };i.src=e.target.result};
  r.readAsDataURL(f);$('fi').value='';
}
$('msg').addEventListener('paste',e=>{const f=[...e.clipboardData.files].find(f=>f.type.startsWith('image/'));if(f){e.preventDefault();pickImg(f)}});

/* ---------- icons, avatars, lightbox, scroll helpers ---------- */
const ic=n=>`<svg class="i"><use href="#i-${n}"/></svg>`;
const hue=n=>{let h=0;for(const c of n)h=(h*31+c.charCodeAt(0))%360;return `linear-gradient(135deg,hsl(${h},75%,55%),hsl(${(h+50)%360},75%,42%))`};
function micUI(on){$('mic').querySelector('use').setAttribute('href',on?'#i-stop':'#i-mic');$('mic').classList.toggle('rec',on)}
function openLb(src){$('lb').querySelector('img').src=src;$('lb').style.display='flex'}
let newCount=0;
const atBottom=()=>$('box').scrollHeight-$('box').scrollTop-$('box').clientHeight<80;
function toBottom(){$('box').scrollTop=$('box').scrollHeight}
$('box').onscroll=()=>{if(atBottom()){$('fab').style.display='none';newCount=0;$('fn').style.display='none'}else $('fab').style.display='flex'};
$('box').ondblclick=e=>{const row=e.target.closest('.mr');if(row&&!e.target.closest('.tools'))socket.emit('react',{msg_id:row.dataset.id,emoji:'❤️'})};
addEventListener('keydown',e=>{if(e.key==='Escape'){$('lb').style.display='none';pop('')}});

/* ---------- dialogs & toasts (replace alert/confirm/prompt) ---------- */
const ui=(()=>{
  let res=null;const di=$('di'),ok=$('dok'),cn=$('dc');
  const close=v=>{$('dlg').style.display='none';if(res){const r=res;res=null;r(v)}};
  const open=o=>new Promise(r=>{
    if(res)close(null);res=r;$('dt').textContent=o.title||'';$('dm').textContent=o.msg||'';
    di.style.display=o.input?'block':'none';di.value=o.value||'';di.placeholder=o.ph||'';
    ok.textContent=o.ok||'OK';ok.className='dbtn '+(o.danger?'dan':'pri');cn.style.display=o.cancel===false?'none':'block';
    $('dlg').style.display='flex';setTimeout(()=>(o.input?di:ok).focus(),30);
  });
  ok.onclick=()=>close(di.style.display==='none'?true:di.value);
  cn.onclick=()=>close(di.style.display==='none'?false:null);
  di.onkeydown=e=>{if(e.key==='Enter')ok.click()};
  $('dlg').onclick=e=>{if(e.target.id==='dlg')cn.style.display!=='none'?cn.click():ok.click()};
  addEventListener('keydown',e=>{if(e.key==='Escape'&&$('dlg').style.display==='flex')cn.style.display!=='none'?cn.click():ok.click()});
  return{alert:(msg,title)=>open({title,msg,cancel:false}),confirm:(msg,o={})=>open({msg,...o}),prompt:(title,o={})=>open({title,input:true,...o})};
})();
function toast(msg){const t=document.createElement('div');t.className='toast';t.textContent=msg;$('toasts').appendChild(t);setTimeout(()=>t.remove(),3200)}

/* ---------- WhatsApp-style voice player ---------- */
let lastDay='',curAudio=null;
const fmtT=s=>{s=Math.max(0,Math.round(s||0));return Math.floor(s/60)+':'+String(s%60).padStart(2,'0')};
function dayLabel(ts){const d=new Date(ts),t=new Date(),y=new Date(Date.now()-864e5);return d.toDateString()===t.toDateString()?'Today':d.toDateString()===y.toDateString()?'Yesterday':d.toLocaleDateString([],{weekday:'short',day:'numeric',month:'short'})}
function waveOf(m){
  const N=34;let w=m.wave&&m.wave.length?m.wave:null;
  if(!w){let h=0;for(const c of m.id)h=(h*33+c.charCodeAt(0))>>>0;w=Array.from({length:N},()=>{h=(h*1664525+1013904223)>>>0;return 15+(h>>>24)%70})}
  const out=[];for(let i=0;i<N;i++){const a=Math.floor(i*w.length/N),b=Math.max(a+1,Math.floor((i+1)*w.length/N));out.push(Math.max(...w.slice(a,b)))}
  const pk=Math.max(...out,1);return out.map(v=>Math.max(14,Math.round(v/pk*100)));
}
function voiceHTML(m){return `<div class="vm" data-d="${+m.dur||0}"><button class="vp" title="Play">${ic('play')}</button><div class="vwrap"><div class="vw">${waveOf(m).map(h=>`<i style="height:${h}%"></i>`).join('')}</div><div class="vt"><span class="vc">${fmtT(m.dur)}</span><button class="vs" title="Playback speed">1x</button></div></div><audio preload="metadata" src="${esc(m.audio)}"></audio></div>`}
function vprog(vm){
  const a=vm.querySelector('audio'),d=+vm.dataset.d||(isFinite(a.duration)?a.duration:0),p=d?Math.min(1,a.currentTime/d):0,bs=vm.querySelectorAll('.vw i'),n=Math.round(p*bs.length);
  bs.forEach((b,i)=>b.classList.toggle('on',i<n));vm.querySelector('.vc').textContent=fmtT(a.currentTime>0&&!a.ended?a.currentTime:d);
}
function initAudio(a){
  const vm=a.closest('.vm'),icon=n=>vm.querySelector('.vp use').setAttribute('href','#i-'+n);
  a.addEventListener('loadedmetadata',()=>{if(a.duration===Infinity){a.currentTime=1e101;a.addEventListener('timeupdate',()=>{a.currentTime=0},{once:true})}});
  a.addEventListener('timeupdate',()=>vprog(vm));
  a.addEventListener('play',()=>icon('pause'));a.addEventListener('pause',()=>icon('play'));
  a.addEventListener('ended',()=>{a.currentTime=0;icon('play');vprog(vm)});
  a.addEventListener('error',()=>toast('This voice note could not be played'));
}
function voiceClick(e){
  const vm=e.target.closest('.vm');if(!vm)return false;
  const a=vm.querySelector('audio'),sp=e.target.closest('.vs');
  if(sp){const l=[1,1.5,2],n=l[(l.indexOf(a.playbackRate)+1)%3];a.playbackRate=n;sp.textContent=n+'x';return true}
  if(e.target.closest('.vp')){if(!a.paused)a.pause();else{if(curAudio&&curAudio!==a)curAudio.pause();curAudio=a;a.play().catch(()=>toast('Could not play this voice note'))}return true}
  const w=e.target.closest('.vw');
  if(w){const r=w.getBoundingClientRect(),d=+vm.dataset.d||a.duration||0;if(d)a.currentTime=d*Math.min(1,Math.max(0,(e.clientX-r.left)/r.width));vprog(vm)}
  return true;
}
function setBub(v){document.body.dataset.bub=v;localStorage.bub=v}
function setDoodle(on){document.body.classList.toggle('doodle',on);localStorage.doodle=on?1:0}

/* ---------- animated background ---------- */
const cv=$('fx'),g=cv.getContext('2d');let cols=[],bubs=[];
function sizeFx(){cv.width=innerWidth;cv.height=innerHeight;cols=Array.from({length:Math.ceil(cv.width/16)},()=>Math.random()*cv.height/16);
  bubs=Array.from({length:18},()=>({x:Math.random()*cv.width,y:Math.random()*cv.height,r:10+Math.random()*50,v:.2+Math.random()*.6}))}
addEventListener('resize',sizeFx);sizeFx();
(function loop(){
  const t=document.body.dataset.theme;
  if(t==='matrix'||t==='cyber'){
    g.fillStyle='rgba(0,0,0,.08)';g.fillRect(0,0,cv.width,cv.height);g.font='14px monospace';
    cols.forEach((y,i)=>{g.fillStyle=t==='matrix'?'#00ff41':(i%2?'#00fff0':'#ff00c8');
      g.fillText(String.fromCharCode(0x30A0+Math.random()*60),i*16,y*16);
      cols[i]=(y*16>cv.height&&Math.random()>.975)?0:y+1});
  }else if(['glass','aurora','sunset','ocean'].includes(t)){
    g.clearRect(0,0,cv.width,cv.height);
    bubs.forEach(b=>{b.y-=b.v;if(b.y<-b.r)b.y=cv.height+b.r;g.beginPath();g.arc(b.x,b.y,b.r,0,7);
      g.fillStyle='rgba(255,255,255,.06)';g.fill();g.strokeStyle='rgba(255,255,255,.18)';g.stroke()});
  }else g.clearRect(0,0,cv.width,cv.height);
  requestAnimationFrame(loop);
})();

/* ---------- search ---------- */
function toggleSearch(){const s=$('sq'),open=s.style.display==='none';s.style.display=open?'block':'none';s.value='';search();if(open)s.focus()}
function search(){const q=$('sq').value.toLowerCase();document.querySelectorAll('#box .mr').forEach(r=>r.style.display=(!q||r.textContent.toLowerCase().includes(q))?'':'none')}

/* ---------- voice recorder ---------- */
let rec=null;
const pickMime=()=>['audio/webm;codecs=opus','audio/webm','audio/mp4','audio/ogg;codecs=opus'].find(t=>window.MediaRecorder&&MediaRecorder.isTypeSupported(t))||'';
function micStop(cancel){if(rec){rec.cancel=cancel;if(rec.r.state!=='inactive')rec.r.stop()}}
async function mic(){
  if(rec)return;
  if(!navigator.mediaDevices||!window.MediaRecorder)return ui.alert('Voice notes are not supported in this browser.','Not supported');
  let st;try{st=await navigator.mediaDevices.getUserMedia({audio:true})}
  catch(e){return ui.alert('Allow microphone access for this site (tap the lock icon in the address bar), then try again.','Microphone blocked')}
  const type=pickMime(),r=new MediaRecorder(st,type?{mimeType:type}:undefined),chunks=[],wave=[];
  const AC=window.AudioContext||window.webkitAudioContext,ac=new AC(),an=ac.createAnalyser();an.fftSize=256;
  ac.createMediaStreamSource(st).connect(an);const buf=new Uint8Array(an.fftSize),t0=Date.now();
  rec={r,cancel:false};$('iw').style.display='none';$('recui').style.display='flex';$('rw').innerHTML='';
  const tick=setInterval(()=>{
    an.getByteTimeDomainData(buf);let m=0;for(const v of buf)m=Math.max(m,Math.abs(v-128));
    const lv=Math.min(100,Math.round(m/128*160));wave.push(lv);
    const s=Math.floor((Date.now()-t0)/1000);$('rt').textContent=fmtT(s);
    const rw=$('rw'),bar=document.createElement('i');bar.style.height=Math.max(8,lv)+'%';rw.appendChild(bar);while(rw.children.length>45)rw.firstChild.remove();
    if(s>=180)micStop(false);
  },100);
  r.ondataavailable=e=>{if(e.data&&e.data.size)chunks.push(e.data)};
  r.onstop=()=>{
    clearInterval(tick);st.getTracks().forEach(t=>t.stop());try{ac.close()}catch(e){}
    $('recui').style.display='none';$('iw').style.display='';
    const cancel=rec.cancel,dur=(Date.now()-t0)/1000;rec=null;
    if(cancel||dur<.7||!chunks.length)return;
    const blob=new Blob(chunks,{type:r.mimeType||type||'audio/webm'});
    if(blob.size>2.2e6)return toast('Voice note too long');
    const N=Math.min(64,wave.length),w2=Array.from({length:N},(_,i)=>{const a=Math.floor(i*wave.length/N),b=Math.max(a+1,Math.floor((i+1)*wave.length/N));return Math.max(...wave.slice(a,b))});
    const fr=new FileReader();
    fr.onload=e=>{let u=e.target.result;if(!u.startsWith('data:audio/'))u=u.replace(/^data:[^;,]*/,'data:audio/'+(/mp4/.test(type)?'mp4':'webm'));
      socket.emit('send_message',{text:'',audio:u,wave:w2,dur:Math.round(dur*10)/10,reply_to:replyTo?replyTo.id:null});setReply(null)};
    fr.readAsDataURL(blob);
  };
  r.start(250);
}

/* ---------- drag & drop images ---------- */
document.addEventListener('dragover',e=>e.preventDefault());
document.addEventListener('drop',e=>{e.preventDefault();const f=e.dataTransfer.files[0];if(f&&me)pickImg(f)});

/* ---------- look settings ---------- */
function setAccent(v){document.body.style.setProperty('--accent',v);localStorage.acc=v}
function setFs(v){document.documentElement.style.fontSize=v+'px';localStorage.fs=v}
function resetLook(){document.body.style.removeProperty('--accent');document.documentElement.style.fontSize='';delete localStorage.acc;delete localStorage.fs;$('fs').value=16}
if(localStorage.acc){setAccent(localStorage.acc);$('ac').value=localStorage.acc}
setBub(localStorage.bub||'round');$('bb').value=localStorage.bub||'round';if(localStorage.doodle==='1'){setDoodle(true);$('dd').checked=true}
if(localStorage.fs){setFs(localStorage.fs);$('fs').value=localStorage.fs}

/* ---------- admin ---------- */
async function wipe(){if(await ui.confirm('Every message will be deleted for everyone. This cannot be undone.',{title:'Wipe chat?',ok:'Wipe',danger:true}))socket.emit('clear_chat_server')}
async function announce(){const t=await ui.prompt('Announcement',{ph:'Message for everyone...',ok:'Send'});if(t&&t.trim())socket.emit('announce',{text:t})}
function exportChat(){
  const t=Object.values(msgs).map(m=>`[${new Date(m.ts).toLocaleString()}] ${m.user}: ${m.text||'[image]'}`).join('\n');
  const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([t],{type:'text/plain'}));a.download='chat_backup.txt';a.click();
}
</script></body></html>
"""

if __name__ == '__main__':
    print(f"\n  Direct v2 running -> http://localhost:{PORT}\n  Now run:  ngrok http {PORT}\n")
    socketio.run(app, host='0.0.0.0', port=PORT, allow_unsafe_werkzeug=True)
