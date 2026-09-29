"""
ICQ — настольный клиент (Tkinter + python-socketio).

Соответствует правилам общего сервера группы:
  1. Связь с сервером — библиотека python-socketio.
  2. Адрес сервера вводится в окне входа (запоминается в icq_client.json).
  3. Вход: событие join {login, password}.
  4. Отправка: событие message {text, time «ЧЧ:ММ»}.
  5. Принимаем message / system / history / error (+ users и typing, если сервер их шлёт).
  6. Интерфейс: логин и пароль, «Войти», отправка сообщений, чужие сообщения видны.
  7. При error — показываем текст и возвращаемся на экран входа.
Пароль нигде не сохраняется.

Запуск: python client_desktop.py
"""
import json
import os
import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

import socketio

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icq_client.json")
DEFAULT_SERVER = "http://127.0.0.1:5055"
SMILES = ["🙂", "😉", "😂", "😎", "😍", "🤔", "😢", "👍", "👋", "🌼", "🎉", "☕"]


# =====================================================================
# СЕТЬ: отдельный класс без интерфейса — его легко тестировать
# =====================================================================
class IcqConnection:
    """Подключение к ICQ-серверу. Все события складываются в очередь events: (тип, данные)."""

    def __init__(self):
        self.events = queue.Queue()
        self.sio = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
        for name in ("message", "system", "history", "error", "users", "typing"):
            self.sio.on(name, self._make_handler(name))
        self.sio.on("disconnect", lambda *a: self.events.put(("disconnect", None)))

    def _make_handler(self, name):
        return lambda data=None: self.events.put((name, data))

    def connect(self, server, login, password, timeout=10):
        """Подключиться и отправить join. Бросает исключение, если сервер недоступен."""
        self.sio.connect(server, transports=["polling", "websocket"], wait_timeout=timeout)
        self.sio.emit("join", {"login": login, "password": password})

    def send(self, text):
        self.sio.emit("message", {"text": text, "time": datetime.now().strftime("%H:%M")})

    def typing(self):
        if self.sio.connected:
            self.sio.emit("typing")

    def close(self):
        if self.sio.connected:
            self.sio.disconnect()


# =====================================================================
# ЗВУК «О-оу»: генерируем WAV в памяти (Windows); на других ОС — системный сигнал
# =====================================================================
def make_uh_oh_wav():
    import io
    import math
    import struct
    import wave
    rate, frames = 22050, bytearray()
    for freq, dur in ((880, 0.14), (587, 0.18)):
        n = int(rate * dur)
        for i in range(n):
            envelope = min(1, i / 300) * min(1, (n - i) / 900)
            frames += struct.pack("<h", int(9000 * envelope * math.sin(2 * math.pi * freq * i / rate)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def play_uh_oh(root):
    try:
        import winsound
        data = make_uh_oh_wav()
        threading.Thread(target=winsound.PlaySound, args=(data, winsound.SND_MEMORY), daemon=True).start()
    except Exception:
        root.bell()


# =====================================================================
# ИНТЕРФЕЙС
# =====================================================================
class App(tk.Tk):
    GREEN = "#3fb34f"

    def __init__(self):
        super().__init__()
        self.title("ICQ")
        self.geometry("420x520")
        self.minsize(380, 460)
        self.configure(bg="#eef4f8")
        self.conn = None
        self.me = None
        self.sound = tk.BooleanVar(value=True)
        self.config_data = self.load_config()
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self.style.configure("Green.TButton", background=self.GREEN, foreground="white", font=("Segoe UI", 10, "bold"))
        self.style.map("Green.TButton", background=[("active", "#2c8c3a")])
        self.frame = None
        self.show_login()
        self.after(100, self.poll)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- Настройки ----------
    def load_config(self):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"server": DEFAULT_SERVER, "login": ""}

    def save_config(self, server, login):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"server": server, "login": login}, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def clear(self):
        if self.frame:
            self.frame.destroy()
        self.frame = tk.Frame(self, bg="#eef4f8")
        self.frame.pack(fill="both", expand=True)
        return self.frame

    # ---------- Окно входа ----------
    def show_login(self, error=""):
        self.me = None
        f = self.clear()
        self.geometry("420x520")
        tk.Label(f, text="🌼", font=("Segoe UI Emoji", 54), bg="#eef4f8").pack(pady=(24, 0))
        tk.Label(f, text="ICQ", font=("Segoe UI", 22, "bold"), fg=self.GREEN, bg="#eef4f8").pack()
        form = tk.Frame(f, bg="#eef4f8")
        form.pack(padx=40, pady=10, fill="x")
        self.v_server = tk.StringVar(value=self.config_data.get("server", DEFAULT_SERVER))
        self.v_login = tk.StringVar(value=self.config_data.get("login", ""))
        self.v_password = tk.StringVar()
        for label, var, show in (("Адрес сервера", self.v_server, ""), ("Логин", self.v_login, ""), ("Пароль", self.v_password, "•")):
            tk.Label(form, text=label, bg="#eef4f8", fg="#5b6b7c", anchor="w").pack(fill="x", pady=(8, 2))
            e = ttk.Entry(form, textvariable=var, show=show, font=("Segoe UI", 11))
            e.pack(fill="x", ipady=3)
            e.bind("<Return>", lambda ev: self.do_login())
        ttk.Button(form, text="Войти", style="Green.TButton", command=self.do_login).pack(fill="x", pady=16, ipady=4)
        self.status = tk.Label(form, text=error, fg="#c0392b", bg="#eef4f8", wraplength=320, justify="center")
        self.status.pack()
        tk.Label(f, text="Адрес и логин запоминаются, пароль — нет", fg="#8a97a6", bg="#eef4f8", font=("Segoe UI", 8)).pack(side="bottom", pady=8)

    def do_login(self):
        server, login, password = self.v_server.get().strip(), self.v_login.get().strip(), self.v_password.get()
        if not server or not login or not password:
            self.status.config(text="Заполните адрес сервера, логин и пароль")
            return
        if not server.startswith(("http://", "https://")):
            server = "http://" + server
        self.status.config(text="Подключение…", fg="#5b6b7c")
        self.update_idletasks()
        self.save_config(server, login)
        self.config_data = {"server": server, "login": login}
        self.conn = IcqConnection()
        self.pending_login = login

        def worker():
            try:
                self.conn.connect(server, login, password)
            except Exception as e:
                self.conn.events.put(("connect_failed", str(e)))
        threading.Thread(target=worker, daemon=True).start()

    # ---------- Окно чата ----------
    def show_chat(self):
        f = self.clear()
        self.geometry("760x560")
        top = tk.Frame(f, bg=self.GREEN)
        top.pack(fill="x")
        tk.Label(top, text=f"🌼 ICQ — {self.me}", fg="white", bg=self.GREEN, font=("Segoe UI", 11, "bold")).pack(side="left", padx=10, pady=6)
        ttk.Checkbutton(top, text="звук", variable=self.sound).pack(side="right", padx=6)
        ttk.Button(top, text="Выйти", command=self.logout).pack(side="right", padx=6, pady=4)

        body = tk.PanedWindow(f, orient="horizontal", sashwidth=4, bg="#dfe7ee")
        body.pack(fill="both", expand=True)
        left = tk.Frame(body, bg="white")
        tk.Label(left, text="В сети", bg="white", fg="#5b6b7c", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=8, pady=(8, 2))
        self.users = tk.Listbox(left, bd=0, highlightthickness=0, font=("Segoe UI", 10), activestyle="none")
        self.users.pack(fill="both", expand=True, padx=4, pady=4)
        body.add(left, width=170)

        right = tk.Frame(body, bg="white")
        self.log = tk.Text(right, wrap="word", state="disabled", bd=0, padx=10, pady=8, font=("Segoe UI", 10), bg="#f7fafc")
        scroll = ttk.Scrollbar(right, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.tag_configure("who", foreground="#2f6db5", font=("Segoe UI", 9, "bold"))
        self.log.tag_configure("me", foreground="#2c8c3a", font=("Segoe UI", 9, "bold"))
        self.log.tag_configure("time", foreground="#8a97a6", font=("Segoe UI", 8))
        self.log.tag_configure("sys", foreground="#8a97a6", justify="center", font=("Segoe UI", 9, "italic"))
        self.typing_label = tk.Label(right, text="", bg="white", fg="#8a97a6", anchor="w", font=("Segoe UI", 8))
        smiles = tk.Frame(right, bg="white")
        for s in SMILES:
            tk.Button(smiles, text=s, bd=0, bg="white", font=("Segoe UI Emoji", 11),
                      command=lambda s=s: (self.entry.insert("end", s), self.entry.focus())).pack(side="left")
        bottom = tk.Frame(right, bg="white")
        self.entry = ttk.Entry(bottom, font=("Segoe UI", 11))
        self.entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(8, 6), pady=8)
        self.entry.bind("<Return>", lambda e: self.send())
        self.entry.bind("<Key>", lambda e: self.conn and self.conn.typing())
        ttk.Button(bottom, text="Отправить", style="Green.TButton", command=self.send).pack(side="right", padx=8)
        bottom.pack(side="bottom", fill="x")
        smiles.pack(side="bottom", fill="x", padx=6)
        self.typing_label.pack(side="bottom", fill="x", padx=10)
        scroll.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)
        body.add(right)
        self.entry.focus()

    def write(self, *parts):
        self.log.configure(state="normal")
        for text, tag in parts:
            self.log.insert("end", text, tag)
        self.log.configure(state="disabled")
        self.log.see("end")

    def add_message(self, m, silent=False):
        mine = m.get("nickname") == self.me
        self.write((f"{m.get('nickname', '?')} ", "me" if mine else "who"), (f"{m.get('time', '')}\n", "time"), (f"{m.get('text', '')}\n\n", ""))
        if not silent and not mine and self.sound.get():
            play_uh_oh(self)

    def send(self):
        text = self.entry.get().strip()
        if text and self.conn:
            self.conn.send(text)
            self.entry.delete(0, "end")

    def logout(self):
        if self.conn:
            self.conn.close()
        self.conn = None
        self.show_login()

    def on_close(self):
        if self.conn:
            self.conn.close()
        self.destroy()

    # ---------- Обработка событий сервера (в главном потоке Tk) ----------
    def poll(self):
        while self.conn:
            try:
                kind, data = self.conn.events.get_nowait()
            except queue.Empty:
                break
            self.handle(kind, data)
        self.after(100, self.poll)

    def handle(self, kind, data):
        if kind == "connect_failed":
            self.conn = None
            self.show_login(f"Сервер недоступен: {data}")
        elif kind == "history":
            if self.me is None:                      # history приходит сразу после успешного входа
                self.me = self.pending_login
                self.show_chat()
            for m in data or []:
                self.add_message(m, silent=True)
            self.write(("— конец истории —\n\n", "sys"))
        elif kind == "message":
            if self.me is None:
                self.me = self.pending_login
                self.show_chat()
            self.add_message(data or {})
        elif kind == "system":
            if self.me is None:                      # сервер без history: первое system = вход выполнен
                self.me = self.pending_login
                self.show_chat()
            self.write((f"{(data or {}).get('text', '')}\n\n", "sys"))
        elif kind == "users" and self.me:
            self.users.delete(0, "end")
            for u in data or []:
                self.users.insert("end", f"🌼 {u}" + ("  (вы)" if u == self.me else ""))
        elif kind == "typing" and self.me:
            self.typing_label.config(text=f"{(data or {}).get('nickname', '')} печатает…")
            self.after(2000, lambda: self.typing_label.config(text=""))
        elif kind == "error":
            text = (data or {}).get("text", "Ошибка")
            if self.me and "спам" in text.lower():   # предупреждение о спаме не выкидывает из чата
                self.write((f"⚠ {text}\n\n", "sys"))
                return
            if self.conn:
                self.conn.close()
            self.conn = None
            messagebox.showerror("ICQ", text)        # правило №7: показать ошибку и вернуться ко входу
            self.show_login(text)
        elif kind == "disconnect" and self.me:
            self.conn = None
            self.show_login("Соединение с сервером потеряно")


if __name__ == "__main__":
    App().mainloop()
