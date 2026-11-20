import hashlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
DB_FILE = os.environ.get("SERVER_DB_FILE", "accounts_state.json")
ONLINE_TTL = 60

STATE_LOCK = threading.Lock()
MEM_STATE = {
    "accounts": {},
    "players": {},
    "reset_epoch": 0,
}


def log(msg: str) -> None:
    print(f"[SERVER] {msg}", flush=True)


def hash_pw(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def make_pid(username: str) -> str:
    h = hashlib.sha256(username.strip().lower().encode("utf-8")).hexdigest()[:12]
    return f"acc_{h}"


def to_int(val, default: int = 0) -> int:
    if val is None or isinstance(val, bool):
        return default
    try:
        return int(float(str(val).strip()))
    except Exception:
        return default


def load_initial_state() -> None:
    if not os.path.exists(DB_FILE):
        return
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            if isinstance(data.get("accounts"), dict):
                MEM_STATE["accounts"] = data["accounts"]
            if isinstance(data.get("players"), dict):
                MEM_STATE["players"] = data["players"]
            MEM_STATE["reset_epoch"] = to_int(data.get("reset_epoch"), 0)
        log(f"Loaded state from {DB_FILE}: {len(MEM_STATE['accounts'])} accounts")
    except Exception as e:
        log(f"Warning loading state: {e}")


def persist_state() -> None:
    try:
        snapshot = {
            "accounts": MEM_STATE["accounts"],
            "players": MEM_STATE["players"],
            "reset_epoch": MEM_STATE["reset_epoch"],
        }
        tmp = DB_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
        os.replace(tmp, DB_FILE)
    except Exception as e:
        log(f"Persist skipped ({e})")


def touch_player(pid: str, nick: str, score: dict | None = None) -> None:
    if not pid:
        return
    now = time.time()
    p = MEM_STATE["players"].get(pid) or {
        "player_id": pid,
        "nickname": nick or "Игрок",
        "money": 0,
        "level": 1,
        "rolls": 0,
        "last_seen": now,
    }
    if nick:
        p["nickname"] = nick.strip()[:24]
    if isinstance(score, dict):
        if "money" in score and score["money"] is not None:
            p["money"] = to_int(score["money"], p.get("money", 0))
        if "level" in score and score["level"] is not None:
            p["level"] = max(1, to_int(score["level"], p.get("level", 1)))
        if "rolls" in score and score["rolls"] is not None:
            p["rolls"] = max(0, to_int(score["rolls"], p.get("rolls", 0)))
    p["last_seen"] = now
    MEM_STATE["players"][pid] = p


def online_count() -> int:
    now = time.time()
    return sum(
        1 for p in MEM_STATE["players"].values()
        if now - float(p.get("last_seen", 0) or 0) <= ONLINE_TTL
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log(f"{self.address_string()} -> {fmt % args}")

    def add_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self.send_header("Access-Control-Max-Age", "86400")

    def send_json(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.add_cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_body(self) -> dict:
        length = to_int(self.headers.get("Content-Length"), 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
        return parsed if isinstance(parsed, dict) else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.add_cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)

            if path in ("/", "/health", "/api/health"):
                with STATE_LOCK:
                    self.send_json(200, {
                        "ok": True,
                        "status": "online",
                        "online": online_count(),
                        "reset_epoch": MEM_STATE["reset_epoch"],
                    })
                return

            if path == "/api/leaderboard":
                metric = (qs.get("metric", ["money"])[0] or "money").strip()
                if metric not in ("money", "level", "rolls"):
                    metric = "money"
                limit = max(1, min(100, to_int(qs.get("limit", ["20"])[0], 20)))

                with STATE_LOCK:
                    now = time.time()
                    items = []
                    for p in MEM_STATE["players"].values():
                        is_on = (now - float(p.get("last_seen", 0) or 0)) <= ONLINE_TTL
                        items.append({
                            "player_id": p.get("player_id", ""),
                            "nickname": p.get("nickname", "Игрок"),
                            "username": p.get("nickname", "Игрок"),
                            "money": to_int(p.get("money"), 0),
                            "level": max(1, to_int(p.get("level"), 1)),
                            "rolls": max(0, to_int(p.get("rolls"), 0)),
                            "is_online": is_on,
                            "online": is_on,
                        })
                    items.sort(key=lambda x: x.get(metric, 0), reverse=True)
                    self.send_json(200, {
                        "ok": True,
                        "metric": metric,
                        "online": online_count(),
                        "reset_epoch": MEM_STATE["reset_epoch"],
                        "items": items[:limit],
                    })
                return

            self.send_json(404, {"ok": False, "error": "Not found"})
        except Exception as e:
            log(f"GET error: {e}")
            self.send_json(500, {"ok": False, "error": str(e)})

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            body = self.read_body()
            log(f"POST {path} keys={list(body.keys())}")

            if path == "/api/auth/register":
                username = str(body.get("username") or "").strip()[:24]
                password = str(body.get("password") or "")
                save_data = body.get("save_data")
                score = body.get("score") if isinstance(body.get("score"), dict) else {}

                if len(username) < 2:
                    self.send_json(400, {"ok": False, "error": "Имя должно быть не короче 2 символов"})
                    return
                if len(password) < 3:
                    self.send_json(400, {"ok": False, "error": "Пароль должен быть не короче 3 символов"})
                    return

                with STATE_LOCK:
                    key = username.lower()
                    if key in MEM_STATE["accounts"]:
                        self.send_json(409, {"ok": False, "error": "Такое имя уже занято. Выберите другое или войдите."})
                        return

                    pid = make_pid(username)
                    MEM_STATE["accounts"][key] = {
                        "username": username,
                        "password_hash": hash_pw(password),
                        "player_id": pid,
                        "save_data": save_data,
                    }
                    touch_player(pid, username, score)
                    persist_state()
                    log(f"Registered account: {username} ({pid})")

                    self.send_json(200, {
                        "ok": True,
                        "username": username,
                        "player_id": pid,
                        "save_data": save_data,
                        "online": online_count(),
                        "reset_epoch": MEM_STATE["reset_epoch"],
                    })
                return

            if path == "/api/auth/login":
                username = str(body.get("username") or "").strip()[:24]
                password = str(body.get("password") or "")

                if not username or not password:
                    self.send_json(400, {"ok": False, "error": "Введите имя и пароль"})
                    return

                with STATE_LOCK:
                    acc = MEM_STATE["accounts"].get(username.lower())
                    if not acc or acc.get("password_hash") != hash_pw(password):
                        self.send_json(401, {"ok": False, "error": "Неверное имя или пароль"})
                        return

                    pid = acc.get("player_id") or make_pid(acc["username"])
                    acc["player_id"] = pid
                    touch_player(pid, acc["username"], None)
                    persist_state()

                    self.send_json(200, {
                        "ok": True,
                        "username": acc["username"],
                        "player_id": pid,
                        "save_data": acc.get("save_data"),
                        "online": online_count(),
                        "reset_epoch": MEM_STATE["reset_epoch"],
                    })
                return

            if path == "/api/account/save":
                username = str(body.get("username") or "").strip()[:24]
                password = str(body.get("password") or "")
                save_data = body.get("save_data")
                score = body.get("score") if isinstance(body.get("score"), dict) else {}

                if not username or not password:
                    self.send_json(401, {"ok": False, "error": "Требуется авторизация"})
                    return

                with STATE_LOCK:
                    acc = MEM_STATE["accounts"].get(username.lower())
                    if not acc or acc.get("password_hash") != hash_pw(password):
                        self.send_json(403, {"ok": False, "error": "Ошибка авторизации"})
                        return

                    pid = acc.get("player_id") or make_pid(acc["username"])
                    acc["player_id"] = pid
                    if save_data is not None:
                        acc["save_data"] = save_data
                    touch_player(pid, acc["username"], score)
                    persist_state()

                    self.send_json(200, {
                        "ok": True,
                        "online": online_count(),
                        "reset_epoch": MEM_STATE["reset_epoch"],
                    })
                return

            if path == "/api/account/rename":
                username = str(body.get("username") or "").strip()[:24]
                password = str(body.get("password") or "")
                new_nick = str(body.get("new_nickname") or "").strip()[:24]

                if not username or not password or len(new_nick) < 2:
                    self.send_json(400, {"ok": False, "error": "Некорректные данные"})
                    return

                with STATE_LOCK:
                    old_key = username.lower()
                    acc = MEM_STATE["accounts"].get(old_key)
                    if not acc or acc.get("password_hash") != hash_pw(password):
                        self.send_json(403, {"ok": False, "error": "Ошибка авторизации"})
                        return

                    new_key = new_nick.lower()
                    if new_key != old_key and new_key in MEM_STATE["accounts"]:
                        self.send_json(409, {"ok": False, "error": "Этот ник уже занят"})
                        return

                    pid = acc.get("player_id") or make_pid(new_nick)
                    acc["username"] = new_nick
                    acc["player_id"] = pid
                    if new_key != old_key:
                        del MEM_STATE["accounts"][old_key]
                        MEM_STATE["accounts"][new_key] = acc

                    touch_player(pid, new_nick, None)
                    persist_state()

                    self.send_json(200, {
                        "ok": True,
                        "username": new_nick,
                        "nickname": new_nick,
                        "player_id": pid,
                    })
                return

            if path in ("/api/heartbeat", "/api/score"):
                pid = str(body.get("player_id") or "").strip()
                nick = str(body.get("nickname") or body.get("username") or "Игрок").strip()[:24] or "Игрок"
                if not pid:
                    self.send_json(400, {"ok": False, "error": "player_id обязателен"})
                    return

                with STATE_LOCK:
                    touch_player(pid, nick, body)
                    self.send_json(200, {
                        "ok": True,
                        "online": online_count(),
                        "reset_epoch": MEM_STATE["reset_epoch"],
                    })
                return

            self.send_json(404, {"ok": False, "error": "Not found"})
        except Exception as e:
            log(f"POST error: {e}")
            self.send_json(500, {"ok": False, "error": str(e)})


def run() -> None:
    load_initial_state()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"Listening on {HOST}:{PORT} (python {sys.version.split()[0]})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("Stopped")
    finally:
        srv.server_close()


if __name__ == "__main__":
    run()
