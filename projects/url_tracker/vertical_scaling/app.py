from flask import Flask, jsonify, request
import sqlite3, time, os

app = Flask(__name__)

PORT        = 5000
DB     = "links.db"



def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS links (
            short_code TEXT PRIMARY KEY,
            long_url    TEXT NOT NULL,
            hits        INTEGER DEFAULT 0,
            created_at  REAL
        )""")


@app.route("/", methods=["GET"])
def index():
    return f"Link Tracker active on port {PORT}"



@app.route("/shorten", methods=["POST"])
def shorten():
    data = request.get_json(force=True, silent=True)
    if not data or "code" not in data or "url" not in data:
        return jsonify({"error": "body must be JSON with 'code' and 'url'"}), 400
    code = data["code"]
    url  = data["url"]
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO links VALUES (?,?,0,?)",
                   (code, url, time.time()))
    return jsonify({"short": f"http://localhost/{code}"})


@app.route("/<code>")
def redirect_link(code):
    with get_db() as db:
        row = db.execute("SELECT * FROM links WHERE short_code=?", (code,)).fetchone()
        if row is None:
            return jsonify({"error": f"code '{code}' not found"}), 404
        db.execute("UPDATE links SET hits = hits + 1 WHERE short_code=?", (code,))
    return jsonify({"url": row["long_url"], "hits": row["hits"] + 1})


if __name__ == "__main__":
    init_db()
    app.run(port=PORT, debug=False)
