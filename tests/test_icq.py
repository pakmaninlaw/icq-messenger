"""
Автотесты ICQ: python tests/test_icq.py
1) протокол сервера через тестовый клиент Flask-SocketIO;
2) настоящее сетевое подключение настольного клиента (python-socketio) к запущенному серверу.
"""
import os
import sys
import tempfile
import threading
import time

os.environ["ICQ_DB"] = os.path.join(tempfile.mkdtemp(), "test_icq.db")
os.environ["ICQ_BAN_SECONDS"] = "60"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import server  # noqa: E402


def events(client, name=None):
    got = client.get_received()
    for e in got:   # у события «message» тестовый клиент кладёт данные без обёртки-списка
        e["data"] = e["args"][0] if isinstance(e["args"], list) and e["args"] else e["args"]
    return [e for e in got if name is None or e["name"] == name]


def test_join_history_and_broadcast():
    a = server.socketio.test_client(server.app)
    b = server.socketio.test_client(server.app)
    a.emit("join", {"login": "Сергей", "password": "secret1"})
    got = events(a)
    names = [e["name"] for e in got]
    assert "history" in names and "users" in names, names
    b.emit("join", {"login": "Ксения", "password": "pw"})
    assert any(e["name"] == "system" and "Ксения в сети" in e["data"]["text"] for e in events(a))
    events(b)
    a.emit("message", {"text": "Привет, группа!", "time": "21:15"})
    msg = events(b, "message")[0]["data"]
    assert msg == {"nickname": "Сергей", "text": "Привет, группа!", "time": "21:15"}, msg
    c = server.socketio.test_client(server.app)
    c.emit("join", {"login": "Анна", "password": "pw"})
    history = events(c, "history")[0]["data"]
    assert history[-1]["text"] == "Привет, группа!"
    for cl in (a, b, c):
        cl.disconnect()


def test_errors_and_protection():
    x = server.socketio.test_client(server.app)
    x.emit("message", {"text": "без входа", "time": "10:00"})
    assert "войдите" in events(x, "error")[0]["data"]["text"]
    x.emit("join", {"login": "a", "password": "p"})
    assert "Логин" in events(x, "error")[0]["data"]["text"]
    for i in range(server.MAX_WRONG_PASSWORDS):
        x.emit("join", {"login": "Сергей", "password": "wrong"})
    texts = [e["data"]["text"] for e in events(x, "error")]
    assert "закрыт" in texts[-1], texts
    x.emit("join", {"login": "Сергей", "password": "secret1"})          # даже верный пароль — бан
    assert "закрыт" in events(x, "error")[0]["data"]["text"]
    server.banned.clear()
    y = server.socketio.test_client(server.app)
    y.emit("join", {"login": "Спамер", "password": "pw"})
    events(y)
    for i in range(7):
        y.emit("message", {"text": f"спам {i}", "time": "bad"})
    got = events(y)
    assert sum(e["name"] == "message" for e in got) == server.SPAM_LIMIT
    assert any(e["name"] == "error" and "спам" in e["data"]["text"] for e in got)
    assert all(":" in e["data"]["time"] for e in got if e["name"] == "message")   # неверное время заменено
    x.disconnect()
    y.disconnect()


def test_desktop_client_over_network():
    import client_desktop
    port = 5093
    t = threading.Thread(target=lambda: server.socketio.run(server.app, host="127.0.0.1", port=port,
                                                            allow_unsafe_werkzeug=True, log_output=False), daemon=True)
    t.start()
    time.sleep(1.5)
    c1, c2 = client_desktop.IcqConnection(), client_desktop.IcqConnection()
    c1.connect(f"http://127.0.0.1:{port}", "Десктоп1", "pw1")
    c2.connect(f"http://127.0.0.1:{port}", "Десктоп2", "pw2")
    time.sleep(0.8)
    c1.send("Сообщение по сети")
    deadline, got = time.time() + 5, None
    while time.time() < deadline and got is None:
        try:
            kind, data = c2.events.get(timeout=0.5)
            if kind == "message" and data["text"] == "Сообщение по сети":
                got = data
        except Exception:
            pass
    assert got and got["nickname"] == "Десктоп1", got
    c1.close()
    c2.close()


def test_uh_oh_sound_is_valid_wav():
    import client_desktop
    wav = client_desktop.make_uh_oh_wav()
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE" and len(wav) > 10000


if __name__ == "__main__":
    # Сетевой тест — первым: тестовый клиент Flask-SocketIO мешает потом запустить настоящий сервер в том же процессе
    tests = [test_desktop_client_over_network] + [v for k, v in list(globals().items())
                                                  if k.startswith("test_") and k != "test_desktop_client_over_network"]
    for t in tests:
        t()
        print(f"✔ {t.__name__}")
    print(f"Все тесты пройдены: {len(tests)}")
    os._exit(0)
