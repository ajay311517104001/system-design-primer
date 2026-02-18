from flask import Flask, jsonify, request
import sqlite3, time, os
from caching import (
    get_cached_link,
    set_cached_link,
    set_negative_cache,
    invalidate_link,
    increment_hit_counter,
    get_cache_stats,
    start_hit_flusher,
)

app = Flask(__name__)

PORT        = int(os.getenv("PORT", 5000))
DB_PATH     = os.getenv("DB_PATH", "links.db")
SERVER_NAME = os.getenv("SERVER_NAME", "server_solo")


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
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
    return f"Link Tracker with Caching active on port {PORT}"


@app.route("/who")
def who():
    return jsonify({"handled_by": SERVER_NAME, "port": PORT, "db": DB_PATH})


# =============================================================================
# POST /shorten
# Strategy: Write-Through for new code creation
#   1. Write to DB (source of truth)
#   2. Invalidate any stale negative cache for this code
#      (someone may have tried to GET this code before it was created)
# We do NOT pre-populate the URL cache here — let cache-aside do that
# lazily on first GET. Reason: avoid caching codes that are never accessed.
# =============================================================================
@app.route("/shorten", methods=["POST"])
def shorten():
    data = request.get_json(force=True, silent=True)
    if not data or "code" not in data or "url" not in data:
        return jsonify({"error": "body must be JSON with 'code' and 'url'"}), 400

    code = data["code"]
    url  = data["url"]

    # 1. Write to DB — source of truth
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO links VALUES (?,?,0,?)",
                   (code, url, time.time()))

    # 2. Clear any negative cache — code now exists
    invalidate_link(code)

    return jsonify({"short": f"http://localhost/{code}", "served_by": SERVER_NAME})


# =============================================================================
# GET /<code>
# Strategy A — Cache-Aside for URL resolution:
#   1. Check Redis for "link:{code}"
#   2. HIT  → return immediately (no DB touch)
#   3. MISS → fetch from SQLite → store in Redis → return
#
# Strategy B — Write-Behind for hit counter:
#   1. Increment Redis counter "hits:{code}" atomically (instant, no DB write)
#   2. Background flusher writes accumulated counts to DB every 30 seconds
#   3. Response shows Redis counter (accurate) not DB value (stale)
# =============================================================================
@app.route("/<code>")
def redirect_link(code):

    # ── Strategy A: Cache-Aside READ ─────────────────────────────────────────
    cached = get_cached_link(code)

    if cached is not None:
        # Check for negative cache hit (code does not exist)
        if cached.get("__not_found__"):
            return jsonify({"error": f"code '{code}' not found"}), 404

        # Cache HIT — no DB query needed
        # Still need to count the hit via write-behind
        live_hits = increment_hit_counter(code)        # Strategy B
        return jsonify({
            "url"       : cached["url"],
            "hits"      : live_hits,                   # Redis counter (accurate)
            "served_by" : SERVER_NAME,
            "cache"     : "HIT",                       # visible in response for learning
        })

    # Cache MISS — must go to DB
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM links WHERE short_code=?", (code,)
        ).fetchone()

    if row is None:
        set_negative_cache(code)                       # cache the miss
        return jsonify({"error": f"code '{code}' not found"}), 404

    # Populate cache for next request (cache-aside write)
    link_data = {"url": row["long_url"]}
    set_cached_link(code, link_data)                   # Strategy A write

    # Count the hit via write-behind (no DB UPDATE here)
    live_hits = increment_hit_counter(code)            # Strategy B

    return jsonify({
        "url"       : row["long_url"],
        "hits"      : live_hits,
        "served_by" : SERVER_NAME,
        "cache"     : "MISS",                          # first request always misses
    })


# =============================================================================
# GET /cache/stats
# Monitoring endpoint — shows cache performance metrics in real time.
# Watch hit_rate_pct climb during the Postman performance test.
# Target: > 90% hit rate
# =============================================================================
@app.route("/cache/stats")
def cache_stats():
    return jsonify(get_cache_stats())


if __name__ == "__main__":
    init_db()
    start_hit_flusher()          # start write-behind background thread
    app.run(host="0.0.0.0", port=PORT, debug=False)
