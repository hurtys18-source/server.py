import json
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8000"))
DB_PATH = Path(__file__).with_name("leaderboard.db")
ONLINE_WINDOW = 60

METRICS = {
    "total_earned": "total_earned DESC, level DESC, rolls DESC",
    "money": "money DESC, level DESC, rolls DESC",
    "level": "level DESC, total_earned DESC, rolls DESC",
    "rolls": "rolls DESC, level DESC, total_earned DESC",
}


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scores (
                player_id TEXT PRIMARY KEY,
                nickname TEXT NOT NULL DEFAULT 'Игрок',
                money INTEGER NOT NULL DEFAULT 0,
                total_earned INTEGER NOT NULL DEFAULT 0,
                level INTEGER NOT NULL DEFAULT 1,
                rolls INTEGER NOT NULL DEFAULT 0,
                last_seen REAL NOT NULL DEFAULT 0
            )
            """
        )


def save_score(data: dict, touch_online: bool = False) -> dict:
    player_id = str(data.get("player_id") or "").strip()[:64]
    if not player_id:
        raise ValueError("Нет player_id")

    nickname = str(data.get("nickname") or "Игрок").strip()[:24] or "Игрок"
    money = max(0, int(data.get("money") or 0))
    total_earned = max(0, int(data.get("total_earned") or money))
    level = max(1, int(data.get("level") or 1))
    rolls = max(0, int(data.get("rolls") or 0))
    now = time.time()

    with get_conn() as conn:
        row = conn.execute(
            "SELECT last_seen FROM scores WHERE player_id = ?", (player_id,)
        ).fetchone()
        last_seen = now if touch_online else (float(row["last_seen"]) if row else 0.0)

        conn.execute(
            """
            INSERT INTO scores (player_id, nickname, money, total_earned, level, rolls, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_id) DO UPDATE SET
                nickname = excluded.nickname,
                money = MAX(scores.money, excluded.money),
                total_earned = MAX(scores.total_earned, excluded.total_earned),
                level = MAX(scores.level, excluded.level),
                rolls = MAX(scores.rolls, excluded.rolls),
                last_seen = ?
            """,
            (player_id, nickname, money, total_earned, level, rolls, last_seen, last_seen),
        )

    return {"ok": True, "player_id": player_id}


def get_leaderboard(metric: str = "total_earned", limit: int = 20) -> dict:
    order_by = METRICS.get(metric, METRICS["total_earned"])
    safe_limit = max(1, min(100, limit))
    now = time.time()

    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT player_id, nickname, money, total_earned, level, rolls, last_seen
            FROM scores
            ORDER BY {order_by}
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
        online_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM scores WHERE last_seen >= ?",
            (now - ONLINE_WINDOW,),
        ).fetchone()

    items = [
        {
            "player_id": r["player_id"],
            "nickname": r["nickname"],
            "money": r["money"],
            "total_earned": r["total_earned"],
            "level": r["level"],
            "rolls": r["rolls"],
            "is_online": (now - float(r["last_seen"])) <= ONLINE_WINDOW,
        }
        for r in rows
    ]

    return {
        "metric": metric,
        "online": int(online_row["cnt"]) if online_row else 0,
        "items": items,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length") or 0)
        if size <= 0:
            return {}
        text = self.rfile.read(size).decode("utf-8")
        return json.loads(text) if text.strip() else {}

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json(200, {"status": "ok"})
            return
        if parsed.path == "/api/leaderboard":
            qs = parse_qs(parsed.query)
            metric = (qs.get("metric") or ["total_earned"])[0]
            try:
                limit = int((qs.get("limit") or ["20"])[0])
            except ValueError:
                limit = 20
            self._json(200, get_leaderboard(metric, limit))
            return
        self._json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in ("/api/score", "/api/heartbeat"):
            self._json(404, {"error": "Not found"})
            return
        try:
            res = save_score(self._body(), touch_online=(parsed.path == "/api/heartbeat"))
            self._json(200, res)
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Running on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
      
