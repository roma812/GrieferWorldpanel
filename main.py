import json
import os
import requests
import threading
import time
import uuid
from flask import Flask, render_template, jsonify, request
from typing import Optional

app = Flask(__name__)

SERVERS_FILE = os.path.join(os.path.dirname(__file__), "servers.json")
REFRESH_INTERVAL = 15

# ─────────────────────────────────────────────
#  ДЕФОЛТНІ СЕРВЕРИ
# ─────────────────────────────────────────────
DEFAULT_SERVERS = [
    {
        "id": "01",
        "name": "Proxy",
        "icon": "🏰",
        "panel_url": "https://control.play2go.cloud",
        "api_key": "ptlc_XC09SlXfWdzsxsb6ZImsYazn2MtswQ8pGKf9SHaKpmZ",
        "server_id": "641e86e4",
    },
    {
        "id": "02",
        "name": "Lobby",
        "icon": "🔥",
        "panel_url": "https://control.play2go.cloud",
        "api_key": "ptlc_XC09SlXfWdzsxsb6ZImsYazn2MtswQ8pGKf9SHaKpmZ",
        "server_id": "c8b88e55",
    },
    {
        "id": "03",
        "name": "Grief-1",
        "icon": "⚔",
        "panel_url": "https://control.play2go.cloud",
        "api_key": "ptlc_XC09SlXfWdzsxsb6ZImsYazn2MtswQ8pGKf9SHaKpmZ",
        "server_id": "90001ca4",
    },
    {
        "id": "04",
        "name": "Grief-2",
        "icon": "⚔",
        "panel_url": "https://panel.prismex.host",
        "api_key": "ptlc_OlXKcJevfr1XWTX1BZP5aunEV13vcjrRMOoS6i8ZOjz",
        "server_id": "b1c83467",
    },
]

# ─────────────────────────────────────────────
#  ЖИВИЙ СПИСОК СЕРВЕРІВ
# ─────────────────────────────────────────────
SERVERS: list = []
servers_lock = threading.Lock()

SERVERS_DATA: dict = {}
data_lock = threading.Lock()


# ─────────────────────────────────────────────
#  РОБОТА З ФАЙЛОМ servers.json
# ─────────────────────────────────────────────
def load_servers_from_file() -> list:
    if not os.path.exists(SERVERS_FILE):
        save_servers_to_file(DEFAULT_SERVERS)
        return list(DEFAULT_SERVERS)
    try:
        with open(SERVERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return list(DEFAULT_SERVERS)


def save_servers_to_file(servers: list) -> None:
    with open(SERVERS_FILE, "w", encoding="utf-8") as f:
        json.dump(servers, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def get_server_by_id(server_id: str) -> Optional[dict]:
    with servers_lock:
        return next((s for s in SERVERS if s["id"] == server_id), None)


def ptero_headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }


def fetch_server_resources(server: dict) -> dict:
    url = f"{server['panel_url']}/api/client/servers/{server['server_id']}/resources"
    try:
        resp = requests.get(url, headers=ptero_headers(server["api_key"]), timeout=10)
        if resp.status_code == 403:
            print(f"[403 ERROR] GET {url}\nResponse: {resp.text}\n")
        resp.raise_for_status()
        data = resp.json()
        attrs = data.get("attributes", {})
        state = attrs.get("current_state", "offline")
        res = attrs.get("resources", {})
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


def refresh_server_now(server: dict) -> dict:
    """Негайно оновлює кеш для одного сервера і повертає свіжі дані."""
    result = fetch_server_resources(server)
    with data_lock:
        SERVERS_DATA[server["id"]] = result
    return result


def force_update(server_id: str) -> dict:
    """
    Ініціює примусове оновлення статусу конкретного сервера поза фоновим циклом.
    Знаходить сервер за ID, одразу оновлює SERVERS_DATA і повертає свіжі дані.
    Якщо сервер не знайдено — повертає помилку.
    """
    server = get_server_by_id(server_id)
    if not server:
        return {"ok": False, "error": "Сервер не знайдено"}
    return refresh_server_now(server)


def send_power_action(server: dict, action: str) -> dict:
    url = f"{server['panel_url']}/api/client/servers/{server['server_id']}/power"
    try:
        resp = requests.post(
            url,
            headers=ptero_headers(server["api_key"]),
            json={"signal": action},
            timeout=10,
        )
        if resp.status_code == 403:
            print(f"[403 ERROR] POST {url}\nResponse: {resp.text}\n")
        if resp.status_code == 204:
            return {"ok": True}
        resp.raise_for_status()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_console_command(server: dict, command: str) -> dict:
    url = f"{server['panel_url']}/api/client/servers/{server['server_id']}/command"
    try:
        resp = requests.post(
            url,
            headers=ptero_headers(server["api_key"]),
            json={"command": command},
            timeout=10,
        )
        if resp.status_code == 403:
            print(f"[403 ERROR] POST {url}\nResponse: {resp.text}\n")
        if resp.status_code == 204:
            return {"ok": True}
        resp.raise_for_status()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────
#  BACKGROUND WORKER — фоновий цикл кожні 15 с
# ─────────────────────────────────────────────
def background_worker():
    while True:
        with servers_lock:
            snapshot = list(SERVERS)

        for server in snapshot:
            try:
                result = fetch_server_resources(server)
            except Exception as e:
                result = {"ok": False, "error": str(e)}

            with data_lock:
                SERVERS_DATA[server["id"]] = result

        time.sleep(REFRESH_INTERVAL)


# ─────────────────────────────────────────────
#  ІНІЦІАЛІЗАЦІЯ
# ─────────────────────────────────────────────
loaded = load_servers_from_file()
SERVERS.extend(loaded)
for s in loaded:
    SERVERS_DATA[s["id"]] = {"ok": None, "state": "pending"}

worker_thread = threading.Thread(target=background_worker, daemon=True)
worker_thread.start()


# ─────────────────────────────────────────────
#  FLASK ROUTES — ОСНОВНІ
# ─────────────────────────────────────────────
@app.route("/")
def index():
    with servers_lock:
        servers_meta = [{"id": s["id"], "name": s["name"], "icon": s["icon"]} for s in SERVERS]
    return render_template("index.html", servers=servers_meta)


@app.route("/api/status/<server_id>")
def api_status(server_id: str):
    server = get_server_by_id(server_id)
    if not server:
        return jsonify({"ok": False, "error": "Сервер не знайдено"}), 404

    with data_lock:
        data = SERVERS_DATA.get(server_id)

    if data is None or data.get("state") == "pending":
        return jsonify({"ok": None, "state": "pending", "error": "Оновлення..."})

    return jsonify(data)


@app.route("/api/status/all")
def api_status_all():
    with data_lock:
        snapshot = dict(SERVERS_DATA)

    with servers_lock:
        ids = [s["id"] for s in SERVERS]

    result = {}
    for sid in ids:
        entry = snapshot.get(sid)
        if entry is None or entry.get("state") == "pending":
            result[sid] = {"ok": None, "state": "pending", "error": "Оновлення..."}
        else:
            result[sid] = entry
    return jsonify(result)


# ─────────────────────────────────────────────
#  FLASK ROUTES — POWER / COMMAND
#  Після команди: 1 с затримка → force_update → свіжий статус у відповіді
# ─────────────────────────────────────────────
@app.route("/api/power/<server_id>", methods=["POST"])
def api_power(server_id: str):
    server = get_server_by_id(server_id)
    if not server:
        return jsonify({"ok": False, "error": "Сервер не знайдено"}), 404

    action = (request.json or {}).get("action")
    if action not in ("start", "stop", "restart", "kill"):
        return jsonify({"ok": False, "error": "Невідома дія"}), 400

    # Відправляємо команду на панель
    result = send_power_action(server, action)
    if not result["ok"]:
        return jsonify(result)

    # Панелі потрібна мить, щоб змінити стан — чекаємо 1 с
    time.sleep(1)

    # force_update: примусово отримуємо свіжий стан поза фоновим циклом
    # і одразу записуємо в SERVERS_DATA
    fresh = force_update(server_id)

    return jsonify({"ok": True, "status": fresh})


@app.route("/api/command/<server_id>", methods=["POST"])
def api_command(server_id: str):
    server = get_server_by_id(server_id)
    if not server:
        return jsonify({"ok": False, "error": "Сервер не знайдено"}), 404

    command = (request.json or {}).get("command", "").strip()
    if not command:
        return jsonify({"ok": False, "error": "Команда порожня"}), 400

    result = send_console_command(server, command)
    if not result["ok"]:
        return jsonify(result)

    # Команди не змінюють state — повертаємо поточний кешований стан
    with data_lock:
        fresh = SERVERS_DATA.get(server_id, {"ok": None, "state": "pending"})
    return jsonify({"ok": True, "status": fresh})


# ─────────────────────────────────────────────
#  FLASK ROUTES — CRUD СЕРВЕРІВ
# ─────────────────────────────────────────────
@app.route("/api/servers/add", methods=["POST"])
def api_servers_add():
    body = request.json or {}
    name      = body.get("name", "").strip()
    icon      = body.get("icon", "").strip() or "🖥️"
    panel_url = body.get("panel_url", "").strip().rstrip("/")
    api_key   = body.get("api_key", "").strip()
    server_id = body.get("server_id", "").strip()

    if not all([name, panel_url, api_key, server_id]):
        return jsonify({"ok": False, "error": "Заповни всі обов'язкові поля"}), 400

    new_server = {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "icon": icon,
        "panel_url": panel_url,
        "api_key": api_key,
        "server_id": server_id,
    }

    with servers_lock:
        SERVERS.append(new_server)
        save_servers_to_file(SERVERS)

    # Відразу отримуємо перший статус у фоні, не блокуючи відповідь
    def _first_fetch():
        result = fetch_server_resources(new_server)
        with data_lock:
            SERVERS_DATA[new_server["id"]] = result

    with data_lock:
        SERVERS_DATA[new_server["id"]] = {"ok": None, "state": "pending"}

    threading.Thread(target=_first_fetch, daemon=True).start()

    return jsonify({"ok": True, "server": {
        "id": new_server["id"],
        "name": new_server["name"],
        "icon": new_server["icon"],
    }})


@app.route("/api/servers/delete/<server_id>", methods=["DELETE"])
def api_servers_delete(server_id: str):
    with servers_lock:
        before = len(SERVERS)
        SERVERS[:] = [s for s in SERVERS if s["id"] != server_id]
        if len(SERVERS) == before:
            return jsonify({"ok": False, "error": "Сервер не знайдено"}), 404
        save_servers_to_file(SERVERS)

    with data_lock:
        SERVERS_DATA.pop(server_id, None)

    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=8080)
