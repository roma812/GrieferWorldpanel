import json
import os
import requests
import threading
import time
import uuid
from datetime import datetime
from flask import Flask, render_template, jsonify, request, redirect, url_for, session
from flask_socketio import SocketIO, join_room, leave_room, emit
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from typing import Optional
from cryptography.fernet import Fernet
import base64, hashlib

import eventlet
eventlet.monkey_patch()

# ─────────────────────────────────────────────
#  APP INIT
# ─────────────────────────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "mc-admin-super-secret-key-change-in-prod")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///mcadmin.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")
login_manager = LoginManager(app)
login_manager.login_view = "login"

REFRESH_INTERVAL = 5  # seconds between per-server refresh cycles

# ─────────────────────────────────────────────
#  ENCRYPTION HELPERS (API keys)
# ─────────────────────────────────────────────
def _get_fernet() -> Fernet:
    raw = app.config["SECRET_KEY"].encode()
    key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(key)

def encrypt_key(plain: str) -> str:
    return _get_fernet().encrypt(plain.encode()).decode()

def decrypt_key(token: str) -> str:
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except Exception:
        return token  # fallback for already-plain keys

# ─────────────────────────────────────────────
#  DATABASE MODELS
# ─────────────────────────────────────────────
class User(UserMixin, db.Model):
    __tablename__ = "users"
    id         = db.Column(db.Integer, primary_key=True)
    username   = db.Column(db.String(80), unique=True, nullable=False)
    password   = db.Column(db.String(256), nullable=False)
    is_admin   = db.Column(db.Boolean, default=False, nullable=False)
    is_super   = db.Column(db.Boolean, default=False, nullable=False)
    created    = db.Column(db.DateTime, default=datetime.utcnow)
    servers    = db.relationship("Server", backref="owner", lazy=True, cascade="all, delete-orphan")

class Server(db.Model):
    __tablename__ = "servers"
    id        = db.Column(db.String(16), primary_key=True)
    user_id   = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name      = db.Column(db.String(80), nullable=False)
    icon      = db.Column(db.String(8), default="🖥️")
    panel_url = db.Column(db.String(256), nullable=False)
    api_key   = db.Column(db.Text, nullable=False)    # stored encrypted
    server_id = db.Column(db.String(64), nullable=False)

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# ─────────────────────────────────────────────
#  IN-MEMORY STATUS CACHE
# ─────────────────────────────────────────────
SERVERS_DATA: dict = {}
data_lock = threading.Lock()

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def ptero_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

def fetch_server_resources(server: Server) -> dict:
    api_key = decrypt_key(server.api_key)
    url = f"{server.panel_url}/api/client/servers/{server.server_id}/resources"
    try:
        resp = requests.get(url, headers=ptero_headers(api_key), timeout=10)
        if resp.status_code == 403:
            print(f"[403] GET {url} → {resp.text}")
        resp.raise_for_status()
        attrs = resp.json().get("attributes", {})
        state = attrs.get("current_state", "offline")
        res   = attrs.get("resources", {})
        return {
            "ok": True,
            "state": state,
            "cpu": round(res.get("cpu_absolute", 0), 1),
            "ram_mb": round(res.get("memory_bytes", 0) / 1024 / 1024, 1),
            "disk_mb": round(res.get("disk_bytes", 0) / 1024 / 1024, 1),
            "uptime": res.get("uptime", 0),
            "network_rx": round(res.get("network_rx_bytes", 0) / 1024, 1),
            "network_tx": round(res.get("network_tx_bytes", 0) / 1024, 1),
        }
    except requests.exceptions.ConnectionError:
        return {"ok": False, "error": "Не вдалося підключитися до панелі"}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "Час очікування вичерпано"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def refresh_server_now(server: Server) -> dict:
    result = fetch_server_resources(server)
    with data_lock:
        SERVERS_DATA[server.id] = result
    socketio.emit("status_update", {"id": server.id, **result}, room=f"server_{server.id}")
    return result

def send_power_action(server: Server, action: str) -> dict:
    api_key = decrypt_key(server.api_key)
    url = f"{server.panel_url}/api/client/servers/{server.server_id}/power"
    try:
        resp = requests.post(url, headers=ptero_headers(api_key), json={"signal": action}, timeout=10)
        if resp.status_code == 403:
            print(f"[403] POST {url} → {resp.text}")
        if resp.status_code == 204:
            return {"ok": True}
        resp.raise_for_status()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def send_console_command(server: Server, command: str) -> dict:
    api_key = decrypt_key(server.api_key)
    url = f"{server.panel_url}/api/client/servers/{server.server_id}/command"
    try:
        resp = requests.post(url, headers=ptero_headers(api_key), json={"command": command}, timeout=10)
        if resp.status_code == 403:
            print(f"[403] POST {url} → {resp.text}")
        if resp.status_code == 204:
            return {"ok": True}
        resp.raise_for_status()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def get_pterodactyl_websocket_token(server: Server) -> Optional[dict]:
    api_key = decrypt_key(server.api_key)
    url = f"{server.panel_url}/api/client/servers/{server.server_id}/websocket"
    try:
        resp = requests.get(url, headers=ptero_headers(api_key), timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return {"token": data.get("token"), "socket": data.get("socket")}
    except Exception:
        return None

# ─────────────────────────────────────────────
#  BACKGROUND WORKER — parallel, per-server
# ─────────────────────────────────────────────
def _server_loop(server_id: str):
    """Independent loop for a single server — fetches immediately, then repeats."""
    while True:
        with app.app_context():
            server = db.session.get(Server, server_id)
            if server is None:
                break  # server was deleted — exit loop
            try:
                result = fetch_server_resources(server)
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            with data_lock:
                SERVERS_DATA[server.id] = result
            socketio.emit(
                "status_update",
                {"id": server.id, **result},
                room=f"server_{server.id}"
            )
        eventlet.sleep(REFRESH_INTERVAL)


_active_loops: set = set()  # track server_ids with running loops
_loops_lock = threading.Lock()


def ensure_server_loop(server_id: str):
    """Spawn a greenlet loop for this server if not already running."""
    with _loops_lock:
        if server_id not in _active_loops:
            _active_loops.add(server_id)
            socketio.start_background_task(_server_loop, server_id)


def background_worker():
    """Startup task: immediately launch loops for every existing server."""
    eventlet.sleep(0.5)  # brief yield so app fully initialises
    with app.app_context():
        servers = Server.query.all()
    for s in servers:
        with data_lock:
            SERVERS_DATA[s.id] = {"ok": None, "state": "pending"}
        ensure_server_loop(s.id)


socketio.start_background_task(background_worker)

# ─────────────────────────────────────────────
#  SOCKET.IO EVENTS
# ─────────────────────────────────────────────
@socketio.on("subscribe")
def on_subscribe(data):
    server_id = data.get("server_id")
    join_room(f"server_{server_id}")

@socketio.on("unsubscribe")
def on_unsubscribe(data):
    server_id = data.get("server_id")
    leave_room(f"server_{server_id}")

@socketio.on("request_ws_token")
def on_request_ws_token(data):
    server_id = data.get("server_id")
    if not current_user.is_authenticated:
        emit("ws_token", {"error": "Unauthorized"})
        return
    server = Server.query.filter_by(id=server_id, user_id=current_user.id).first()
    if not server:
        emit("ws_token", {"error": "Not found"})
        return
    creds = get_pterodactyl_websocket_token(server)
    if creds:
        emit("ws_token", {"server_id": server_id, **creds})
    else:
        emit("ws_token", {"error": "Не вдалося отримати WebSocket токен"})

# ─────────────────────────────────────────────
#  FLASK ROUTES — AUTH
# ─────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    servers = Server.query.filter_by(user_id=current_user.id).all()
    servers_meta = [{"id": s.id, "name": s.name, "icon": s.icon} for s in servers]
    for s in servers:
        with data_lock:
            if s.id not in SERVERS_DATA:
                SERVERS_DATA[s.id] = {"ok": None, "state": "pending"}
        ensure_server_loop(s.id)
    return render_template(
        "index.html",
        servers=servers_meta,
        username=current_user.username,
        is_admin=current_user.is_admin or current_user.is_super,
        is_super=current_user.is_super,
    )

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        data = request.get_json() or request.form
        username = (data.get("username") or "").strip()
        password = (data.get("password") or "").strip()
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            login_user(user, remember=True)
            if request.is_json:
                return jsonify({"ok": True})
            return redirect(url_for("index"))
        if request.is_json:
            return jsonify({"ok": False, "error": "Невірний логін або пароль"}), 401
        return render_template("login.html", error="Невірний логін або пароль")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

# ─────────────────────────────────────────────
#  FLASK ROUTES — ADMIN MANAGEMENT (superadmin)
# ─────────────────────────────────────────────
def superadmin_required(f):
    """Decorator: only superadmin can call this route."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_super:
            return jsonify({"ok": False, "error": "Доступ заборонено"}), 403
        return f(*args, **kwargs)
    return decorated

@app.route("/api/admin/users", methods=["GET"])
@login_required
@superadmin_required
def api_admin_list_users():
    users = User.query.all()
    return jsonify([{
        "id": u.id,
        "username": u.username,
        "is_admin": u.is_admin,
        "is_super": u.is_super,
        "created": u.created.isoformat(),
    } for u in users])

@app.route("/api/admin/users/create", methods=["POST"])
@login_required
@superadmin_required
def api_admin_create_user():
    body     = request.json or {}
    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()
    is_admin = bool(body.get("is_admin", False))

    if not username or not password:
        return jsonify({"ok": False, "error": "Заповни всі поля"}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "error": "Пароль мінімум 6 символів"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"ok": False, "error": "Ім'я вже зайнято"}), 400

    user = User(
        username=username,
        password=generate_password_hash(password),
        is_admin=is_admin,
        is_super=False,
    )
    db.session.add(user)
    db.session.commit()
    return jsonify({"ok": True, "user": {"id": user.id, "username": user.username, "is_admin": user.is_admin}})

@app.route("/api/admin/users/<int:user_id>/toggle_admin", methods=["POST"])
@login_required
@superadmin_required
def api_admin_toggle_admin(user_id: int):
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"ok": False, "error": "Користувача не знайдено"}), 404
    if user.is_super:
        return jsonify({"ok": False, "error": "Неможливо змінити superadmin"}), 400
    user.is_admin = not user.is_admin
    db.session.commit()
    return jsonify({"ok": True, "is_admin": user.is_admin})

@app.route("/api/admin/users/<int:user_id>/delete", methods=["DELETE"])
@login_required
@superadmin_required
def api_admin_delete_user(user_id: int):
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"ok": False, "error": "Користувача не знайдено"}), 404
    if user.is_super:
        return jsonify({"ok": False, "error": "Неможливо видалити superadmin"}), 400
    db.session.delete(user)
    db.session.commit()
    return jsonify({"ok": True})

# ─────────────────────────────────────────────
#  FLASK ROUTES — STATUS
# ─────────────────────────────────────────────
@app.route("/api/status/<server_id>")
@login_required
def api_status(server_id: str):
    server = Server.query.filter_by(id=server_id, user_id=current_user.id).first()
    if not server:
        return jsonify({"ok": False, "error": "Сервер не знайдено"}), 404
    with data_lock:
        data = SERVERS_DATA.get(server_id)
    if data is None or data.get("state") == "pending":
        return jsonify({"ok": None, "state": "pending", "error": "Оновлення..."})
    return jsonify(data)

@app.route("/api/status/all")
@login_required
def api_status_all():
    servers = Server.query.filter_by(user_id=current_user.id).all()
    with data_lock:
        snapshot = dict(SERVERS_DATA)
    result = {}
    for s in servers:
        entry = snapshot.get(s.id)
        if entry is None or entry.get("state") == "pending":
            result[s.id] = {"ok": None, "state": "pending", "error": "Оновлення..."}
        else:
            result[s.id] = entry
    return jsonify(result)

# ─────────────────────────────────────────────
#  FLASK ROUTES — POWER / COMMAND
# ─────────────────────────────────────────────
@app.route("/api/power/<server_id>", methods=["POST"])
@login_required
def api_power(server_id: str):
    server = Server.query.filter_by(id=server_id, user_id=current_user.id).first()
    if not server:
        return jsonify({"ok": False, "error": "Сервер не знайдено"}), 404
    action = (request.json or {}).get("action")
    if action not in ("start", "stop", "restart", "kill"):
        return jsonify({"ok": False, "error": "Невідома дія"}), 400
    result = send_power_action(server, action)
    if not result["ok"]:
        return jsonify(result)
    eventlet.sleep(1)
    fresh = refresh_server_now(server)
    return jsonify({"ok": True, "status": fresh})

@app.route("/api/command/<server_id>", methods=["POST"])
@login_required
def api_command(server_id: str):
    server = Server.query.filter_by(id=server_id, user_id=current_user.id).first()
    if not server:
        return jsonify({"ok": False, "error": "Сервер не знайдено"}), 404
    command = (request.json or {}).get("command", "").strip()
    if not command:
        return jsonify({"ok": False, "error": "Команда порожня"}), 400
    result = send_console_command(server, command)
    if not result["ok"]:
        return jsonify(result)
    with data_lock:
        fresh = SERVERS_DATA.get(server_id, {"ok": None, "state": "pending"})
    return jsonify({"ok": True, "status": fresh})

# ─────────────────────────────────────────────
#  FLASK ROUTES — CRUD SERVERS
# ─────────────────────────────────────────────
@app.route("/api/servers/add", methods=["POST"])
@login_required
def api_servers_add():
    body      = request.json or {}
    name      = body.get("name", "").strip()
    icon      = body.get("icon", "").strip() or "🖥️"
    panel_url = body.get("panel_url", "").strip().rstrip("/")
    api_key   = body.get("api_key", "").strip()
    server_id = body.get("server_id", "").strip()

    if not all([name, panel_url, api_key, server_id]):
        return jsonify({"ok": False, "error": "Заповни всі обов'язкові поля"}), 400

    new_id = uuid.uuid4().hex[:8]
    new_server = Server(
        id=new_id,
        user_id=current_user.id,
        name=name,
        icon=icon,
        panel_url=panel_url,
        api_key=encrypt_key(api_key),
        server_id=server_id,
    )
    db.session.add(new_server)
    db.session.commit()

    with data_lock:
        SERVERS_DATA[new_id] = {"ok": None, "state": "pending"}

    # Spin up a dedicated refresh loop immediately
    ensure_server_loop(new_id)

    return jsonify({"ok": True, "server": {"id": new_id, "name": name, "icon": icon}})

@app.route("/api/servers/delete/<server_id>", methods=["DELETE"])
@login_required
def api_servers_delete(server_id: str):
    server = Server.query.filter_by(id=server_id, user_id=current_user.id).first()
    if not server:
        return jsonify({"ok": False, "error": "Сервер не знайдено"}), 404
    db.session.delete(server)
    db.session.commit()
    with data_lock:
        SERVERS_DATA.pop(server_id, None)
    with _loops_lock:
        _active_loops.discard(server_id)  # loop will exit on next iteration
    return jsonify({"ok": True})

# ─────────────────────────────────────────────
#  INIT DB + SEED SUPERADMIN + RUN
# ─────────────────────────────────────────────
with app.app_context():
    db.create_all()

    # Auto-create superadmin if it doesn't exist
    if not User.query.filter_by(username="superadmin").first():
        superadmin = User(
            username="superadmin",
            password=generate_password_hash("superpass123"),
            is_admin=True,
            is_super=True,
        )
        db.session.add(superadmin)
        db.session.commit()
        print("[INIT] superadmin створено (superadmin / superpass123)")

if __name__ == "__main__":
    socketio.run(app, debug=False, host="0.0.0.0", port=8080)
