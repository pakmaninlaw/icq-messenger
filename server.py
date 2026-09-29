"""
ICQ-мессенджер («Аська») — сервер.

Протокол совместим с общим сервером группы (правила из закрепа учебного чата):
  клиент -> сервер:  join    {login, password}
                     message {text, time}          time — «ЧЧ:ММ»
  сервер -> клиент:  message {nickname, text, time} новое сообщение
                     system  {text}                 системное уведомление
                     history [message, ...]         история при входе
                     error   {text}                 ошибка (клиент показывает её и возвращается ко входу)
Расширения этого сервера (чужие клиенты их просто не заметят):
                     users   [ник, ...]             кто в сети
                     typing  {nickname}             «печатает…»

Запуск: python server.py  ->  http://127.0.0.1:5055 (там же веб-клиент)
"""
import logging
import os
import re
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime

from flask import Flask, render_template_string, request
from flask_socketio import SocketIO, emit
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.environ.get("ICQ_DB") or os.path.join(BASE_DIR, "icq.db")
PORT = int(os.environ.get("ICQ_PORT", 5055))

HISTORY_SIZE = 50            # сколько последних сообщений отдаём при входе
MAX_TEXT = 1000              # максимальная длина сообщения
MAX_WRONG_PASSWORDS = 5      # после стольких ошибок подряд — бан
BAN_SECONDS = int(os.environ.get("ICQ_BAN_SECONDS", 15 * 60))   # у Ксении — 3 дня, у нас по умолчанию 15 минут
SPAM_LIMIT = 5               # не больше 5 сообщений…
SPAM_WINDOW = 1.0            # …в секунду
LOGIN_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9_.\-]{2,20}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("ICQ_SECRET") or os.urandom(16).hex()
# cors_allowed_origins="*" — подключаться могут клиенты с любых адресов (веб, настольные, одногруппники)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("icq")

# ---------- Состояние сервера в памяти ----------
lock = threading.Lock()
online = {}          # sid (номер соединения) -> логин
wrong = {}           # логин -> число неверных паролей подряд
banned = {}          # логин -> время окончания бана (unix time)
recent = {}          # sid -> очередь времён последних сообщений (антиспам)


# ==================== БАЗА ДАННЫХ ====================
def db_connect():
    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with db_connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                login         TEXT PRIMARY KEY COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                nickname TEXT NOT NULL,
                text     TEXT NOT NULL,
                time     TEXT NOT NULL,
                ts       TEXT NOT NULL
            );
        """)


def history():
    with db_connect() as db:
        rows = db.execute("SELECT nickname, text, time FROM messages ORDER BY id DESC LIMIT ?", (HISTORY_SIZE,)).fetchall()
    return [dict(r) for r in reversed(rows)]


def check_user(login, password):
    """Вход: известный логин — сверяем пароль с хешем; новый — регистрируем (первый вход = регистрация)."""
    with db_connect() as db:
        row = db.execute("SELECT password_hash FROM users WHERE login = ?", (login,)).fetchone()
        if row is None:
            db.execute("INSERT INTO users (login, password_hash, created_at) VALUES (?, ?, ?)",
                       (login, generate_password_hash(password), datetime.now().isoformat(timespec="seconds")))
            return "new"
        return "ok" if check_password_hash(row["password_hash"], password) else "wrong"


# ==================== СОБЫТИЯ SOCKET.IO ====================
def users_online():
    return sorted(set(online.values()), key=str.lower)


@socketio.on("connect")
def on_connect():
    log.info("Подключение %s (%s)", request.sid, request.remote_addr)


@socketio.on("join")
def on_join(data):
    data = data if isinstance(data, dict) else {}
    login = str(data.get("login", "")).strip()
    password = str(data.get("password", ""))
    if not LOGIN_RE.match(login):
        return emit("error", {"text": "Логин: 2–20 символов — буквы, цифры, «_», «.», «-»"})
    if not 1 <= len(password) <= 64:
        return emit("error", {"text": "Введите пароль (до 64 символов)"})
    with lock:
        until = banned.get(login.lower(), 0)
        if until > time.time():
            return emit("error", {"text": f"Слишком много неверных паролей. Вход закрыт до {datetime.fromtimestamp(until):%H:%M}"})
    result = check_user(login, password)
    with lock:
        if result == "wrong":
            n = wrong.get(login.lower(), 0) + 1
            wrong[login.lower()] = n
            if n >= MAX_WRONG_PASSWORDS:
                banned[login.lower()] = time.time() + BAN_SECONDS
                wrong.pop(login.lower(), None)
                log.warning("Бан за подбор пароля: %s", login)
                return emit("error", {"text": f"Неверный пароль {n} раз подряд — вход закрыт на {BAN_SECONDS // 60} мин."})
            log.warning("Неверный пароль: %s (%s/%s)", login, n, MAX_WRONG_PASSWORDS)
            return emit("error", {"text": f"Неверный пароль (попытка {n} из {MAX_WRONG_PASSWORDS})"})
        wrong.pop(login.lower(), None)
        online[request.sid] = login
        users = users_online()
    log.info("Вход: %s (%s)", login, "новый пользователь" if result == "new" else "пароль верный")
    emit("history", history())
    emit("system", {"text": "Добро пожаловать! Вы зарегистрированы." if result == "new" else f"С возвращением, {login}!"})
    emit("system", {"text": f"{login} в сети"}, broadcast=True, include_self=False)
    emit("users", users, broadcast=True)


@socketio.on("message")
def on_message(data):
    data = data if isinstance(data, dict) else {}
    login = online.get(request.sid)
    if not login:
        return emit("error", {"text": "Сначала войдите (событие join)"})
    text = str(data.get("text", "")).strip()
    if not text:
        return
    if len(text) > MAX_TEXT:
        return emit("error", {"text": f"Сообщение длиннее {MAX_TEXT} символов"})
    now = time.time()
    with lock:
        q = recent.setdefault(request.sid, deque())
        while q and now - q[0] > SPAM_WINDOW:
            q.popleft()
        if len(q) >= SPAM_LIMIT:
            log.warning("Спам от %s", login)
            return emit("error", {"text": "Не больше 5 сообщений в секунду — не спамьте"})
        q.append(now)
    msg_time = str(data.get("time", ""))
    if not TIME_RE.match(msg_time):
        msg_time = datetime.now().strftime("%H:%M")
    msg = {"nickname": login, "text": text, "time": msg_time}
    with db_connect() as db:
        db.execute("INSERT INTO messages (nickname, text, time, ts) VALUES (?, ?, ?, ?)",
                   (login, text, msg_time, datetime.now().isoformat(timespec="seconds")))
    emit("message", msg, broadcast=True)


@socketio.on("typing")
def on_typing():
    login = online.get(request.sid)
    if login:
        emit("typing", {"nickname": login}, broadcast=True, include_self=False)


@socketio.on("disconnect")
def on_disconnect(*args):
    with lock:
        login = online.pop(request.sid, None)
        recent.pop(request.sid, None)
        still_online = login in online.values()
        users = users_online()
    if login:
        log.info("Выход: %s", login)
        if not still_online:
            emit("system", {"text": f"{login} вышел из сети"}, broadcast=True)
        emit("users", users, broadcast=True)


# ==================== ВЕБ-КЛИЕНТ ====================
@app.route("/")
def index():
    return render_template_string(WEB_CLIENT)


@app.route("/health")
def health():
    return {"ok": True, "online": len(set(online.values()))}


WEB_CLIENT = r"""
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ICQ — веб-клиент</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<style>
  :root { --green: #3fb34f; --green-d: #2c8c3a; --bg: #e7eef5; --win: #fff; --line: #d7e0ea; --text: #1d2733; --muted: #6b7a8c; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: 'Inter', Tahoma, sans-serif; background: linear-gradient(160deg, #cfe3f5, #e9f3e3); color: var(--text); min-height: 100vh; }
  .flower { width: 28px; height: 28px; position: relative; display: inline-block; flex: none; }
  .flower i { position: absolute; left: 10px; top: 0; width: 8px; height: 13px; border-radius: 50%; background: var(--green); transform-origin: 4px 14px; }
  .flower i:nth-child(1) { background: #e8453c; }
  .flower i:nth-child(2) { transform: rotate(51deg); } .flower i:nth-child(3) { transform: rotate(103deg); }
  .flower i:nth-child(4) { transform: rotate(154deg); } .flower i:nth-child(5) { transform: rotate(206deg); }
  .flower i:nth-child(6) { transform: rotate(257deg); } .flower i:nth-child(7) { transform: rotate(308deg); }
  .flower b { position: absolute; left: 9px; top: 9px; width: 10px; height: 10px; border-radius: 50%; background: #f4d03f; }
  .flower.off i { background: #b9c2cc !important; } .flower.off b { background: #dde2e7; }
  .flower.big { transform: scale(2.2); margin: 26px; }
  .win { background: var(--win); border-radius: 14px; box-shadow: 0 18px 50px rgba(30,60,90,.18); overflow: hidden; border: 1px solid var(--line); }
  .titlebar { background: linear-gradient(180deg, #4fb95e, #2f9a40); color: #fff; padding: 10px 14px; display: flex; align-items: center; gap: 10px; font-weight: 700; }
  .titlebar .dots { margin-left: auto; display: flex; gap: 6px; } .titlebar .dots span { width: 12px; height: 12px; border-radius: 50%; background: rgba(255,255,255,.55); }
  #login { max-width: 380px; margin: 9vh auto; }
  #login .body { padding: 22px 26px 26px; text-align: center; }
  label { display: block; text-align: left; font-size: .85rem; color: var(--muted); margin: 12px 0 5px; }
  input { width: 100%; padding: 10px 12px; border: 1px solid var(--line); border-radius: 10px; font: inherit; }
  button { font: inherit; cursor: pointer; }
  .btn { background: var(--green); color: #fff; border: 0; border-radius: 10px; padding: 11px 16px; font-weight: 700; width: 100%; margin-top: 18px; transition: background .2s; }
  .btn:hover { background: var(--green-d); }
  .err { color: #c0392b; min-height: 1.3em; margin-top: 10px; font-size: .9rem; }
  .hint { font-size: .78rem; color: var(--muted); margin-top: 12px; }
  #app { display: none; max-width: 1080px; margin: 3vh auto; height: 88vh; grid-template-columns: 260px 1fr; gap: 16px; padding: 0 12px; }
  #app.on { display: grid; }
  .side, .chat { display: flex; flex-direction: column; }
  .me { padding: 14px; display: flex; gap: 10px; align-items: center; border-bottom: 1px solid var(--line); }
  .me b { display: block; } .me small { color: var(--green-d); }
  .list-title { font-size: .75rem; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); padding: 12px 14px 6px; }
  #users { list-style: none; margin: 0; padding: 0 8px; overflow-y: auto; flex: 1; }
  #users li { display: flex; align-items: center; gap: 10px; padding: 7px 8px; border-radius: 8px; }
  #users li:hover { background: #f0f6ee; }
  .side-foot { padding: 12px 14px; border-top: 1px solid var(--line); display: flex; gap: 8px; }
  .side-foot button { flex: 1; border: 1px solid var(--line); background: #fff; border-radius: 8px; padding: 7px; font-size: .85rem; }
  #messages { flex: 1; overflow-y: auto; padding: 16px 18px; background: #f7fafc; }
  .msg { margin-bottom: 10px; max-width: 78%; }
  .msg .who { font-size: .78rem; font-weight: 700; color: #2f6db5; }
  .msg .bubble { background: #fff; border: 1px solid var(--line); padding: 8px 12px; border-radius: 4px 14px 14px 14px; white-space: pre-wrap; word-wrap: break-word; }
  .msg .t { font-size: .7rem; color: var(--muted); margin-left: 6px; font-weight: 400; }
  .msg.mine { margin-left: auto; } .msg.mine .who { color: var(--green-d); text-align: right; }
  .msg.mine .bubble { background: #e3f5e1; border-color: #c8e8c4; border-radius: 14px 4px 14px 14px; }
  .sys { text-align: center; font-size: .8rem; color: var(--muted); margin: 8px 0; }
  #typing { height: 18px; font-size: .78rem; color: var(--muted); padding: 0 18px; }
  .composer { display: flex; gap: 8px; padding: 10px 12px; border-top: 1px solid var(--line); align-items: center; }
  .composer input { flex: 1; }
  .composer .send { width: auto; margin: 0; padding: 10px 18px; }
  .smiles { display: flex; gap: 2px; flex-wrap: wrap; padding: 6px 12px 0; }
  .smiles button { border: 0; background: none; font-size: 1.2rem; padding: 2px 4px; border-radius: 6px; }
  .smiles button:hover { background: #eef3f7; }
  @media (max-width: 760px) { #app.on { grid-template-columns: 1fr; height: auto; } .side { max-height: 220px; } .chat { height: 75vh; } }
</style>
</head>
<body>
<div id="login" class="win">
  <div class="titlebar"><div class="flower"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><b></b></div> ICQ<div class="dots"><span></span><span></span></div></div>
  <div class="body">
    <div class="flower big"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><b></b></div>
    <form id="loginForm">
      <label>Логин (ник)</label><input id="lg" autocomplete="username" required maxlength="20">
      <label>Пароль</label><input id="pw" type="password" autocomplete="current-password" required maxlength="64">
      <button class="btn">Войти</button>
    </form>
    <div class="err" id="err"></div>
    <div class="hint">Первый вход с новым ником — регистрация. Пароль хранится на сервере только в виде хеша.</div>
  </div>
</div>

<div id="app">
  <div class="win side">
    <div class="titlebar"><div class="flower"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><b></b></div> ICQ</div>
    <div class="me"><div class="flower"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><b></b></div><div><b id="meName"></b><small>● в сети</small></div></div>
    <div class="list-title">В сети (<span id="cnt">0</span>)</div>
    <ul id="users"></ul>
    <div class="side-foot"><button id="soundBtn">🔔 Звук: вкл</button><button id="logout">Выйти</button></div>
  </div>
  <div class="win chat">
    <div class="titlebar">Общий чат<div class="dots"><span></span><span></span></div></div>
    <div id="messages"></div>
    <div id="typing"></div>
    <div class="smiles" id="smiles"></div>
    <form class="composer" id="sendForm">
      <input id="text" placeholder="Сообщение…" maxlength="1000" autocomplete="off">
      <button class="btn send">Отправить</button>
    </form>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/socket.io-client@4.7.5/dist/socket.io.min.js"></script>
<script>
  const $ = id => document.getElementById(id);
  const ROOT = {{ request.script_root|tojson }};
  let socket = null, me = null, soundOn = true, typingTimer = null, lastTyping = 0;

  // «О-оу!» — два тона, как в классической ICQ (звук синтезируется в браузере, файл не нужен)
  function uhOh() {
    if (!soundOn) return;
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      [[880, 0], [587, .16]].forEach(([freq, at]) => {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.type = 'triangle'; o.frequency.value = freq;
        g.gain.setValueAtTime(.0001, ctx.currentTime + at);
        g.gain.exponentialRampToValueAtTime(.25, ctx.currentTime + at + .02);
        g.gain.exponentialRampToValueAtTime(.0001, ctx.currentTime + at + .15);
        o.connect(g).connect(ctx.destination); o.start(ctx.currentTime + at); o.stop(ctx.currentTime + at + .16);
      });
    } catch (e) {}
  }

  function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
  function hhmm() { const d = new Date(); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); }

  function addMessage(m, silent) {
    const box = $('messages'), div = document.createElement('div');
    div.className = 'msg' + (m.nickname === me ? ' mine' : '');
    div.innerHTML = `<div class="who">${esc(m.nickname)}<span class="t">${esc(m.time || '')}</span></div><div class="bubble">${esc(m.text)}</div>`;
    box.appendChild(div); box.scrollTop = box.scrollHeight;
    if (!silent && m.nickname !== me) { uhOh(); if (document.hidden) document.title = '✉ ' + m.nickname + ' — ICQ'; }
  }
  function addSystem(text) {
    const div = document.createElement('div'); div.className = 'sys'; div.textContent = text;
    $('messages').appendChild(div); $('messages').scrollTop = $('messages').scrollHeight;
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) document.title = 'ICQ — веб-клиент'; });

  function showLogin(error) {
    $('app').classList.remove('on'); $('login').style.display = 'block'; $('err').textContent = error || '';
    if (socket) { socket.off(); socket.disconnect(); socket = null; }
  }

  $('loginForm').addEventListener('submit', e => {
    e.preventDefault();
    const login = $('lg').value.trim(), password = $('pw').value;
    $('err').textContent = 'Подключение…';
    socket = io({path: ROOT + '/socket.io'});
    socket.on('connect', () => socket.emit('join', {login, password}));
    socket.on('connect_error', () => showLogin('Сервер недоступен'));
    socket.on('history', list => {
      me = login; $('meName').textContent = login; $('messages').innerHTML = '';
      $('login').style.display = 'none'; $('app').classList.add('on'); $('pw').value = '';
      list.forEach(m => addMessage(m, true)); addSystem('— история: последние ' + list.length + ' сообщений —');
      try { localStorage.setItem('icq-login', login); } catch (e) {}
      $('text').focus();
    });
    socket.on('message', m => addMessage(m));
    socket.on('system', m => addSystem(m.text));
    socket.on('error', m => { if (me && $('app').classList.contains('on') && !/пароль|войдите|вход/i.test(m.text)) { addSystem('⚠ ' + m.text); } else { me = null; showLogin(m.text); } });
    socket.on('users', list => {
      $('cnt').textContent = list.length;
      $('users').innerHTML = list.map(u => `<li><div class="flower"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><b></b></div>${esc(u)}${u === me ? ' <small>(вы)</small>' : ''}</li>`).join('');
    });
    socket.on('typing', m => {
      $('typing').textContent = m.nickname + ' печатает…';
      clearTimeout(typingTimer); typingTimer = setTimeout(() => $('typing').textContent = '', 2000);
    });
    socket.on('disconnect', () => { if (me) addSystem('Связь потеряна — переподключаемся…'); });
  });

  $('sendForm').addEventListener('submit', e => {
    e.preventDefault();
    const text = $('text').value.trim();
    if (!text || !socket) return;
    socket.emit('message', {text, time: hhmm()});
    $('text').value = '';
  });
  $('text').addEventListener('input', () => {
    if (socket && Date.now() - lastTyping > 1500) { socket.emit('typing'); lastTyping = Date.now(); }
  });
  $('logout').addEventListener('click', () => { me = null; showLogin(''); });
  $('soundBtn').addEventListener('click', () => { soundOn = !soundOn; $('soundBtn').textContent = soundOn ? '🔔 Звук: вкл' : '🔕 Звук: выкл'; });
  ['🙂', '😉', '😂', '😎', '😍', '🤔', '😢', '😡', '👍', '👋', '🌼', '🎉', '☕', '🍻'].forEach(s => {
    const b = document.createElement('button'); b.type = 'button'; b.textContent = s;
    b.onclick = () => { $('text').value += s; $('text').focus(); };
    $('smiles').appendChild(b);
  });
  try { $('lg').value = localStorage.getItem('icq-login') || ''; } catch (e) {}
</script>
</body>
</html>
"""

init_db()

if __name__ == "__main__":
    print(f"\n{'=' * 50}\n🌼 ICQ-сервер запущен: http://127.0.0.1:{PORT}\n   База: {DB_FILE}\n{'=' * 50}\n")
    socketio.run(app, host=os.environ.get("ICQ_HOST", "0.0.0.0"), port=PORT, allow_unsafe_werkzeug=True)
