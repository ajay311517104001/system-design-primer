"""
Lab 3 — Caching Layer
=====================
Implements two strategies decided in the architectural review:

  Strategy A — Cache-Aside (Lazy Loading) for URL resolution
    - app checks Redis first on every GET
    - on miss: fetches from SQLite, stores in Redis
    - TTL = 1 hour (URLs never change after creation — safe to cache long)

  Strategy B — Write-Behind for hit counter
    - every GET increments a Redis counter instantly (no DB write)
    - a background thread flushes accumulated counts to SQLite every 30 seconds
    - trade-off: hit count in DB may be ~30 seconds stale — acceptable

  Negative cache:
    - unknown short codes are also cached ("not found") for 30 seconds
    - prevents DB hammering when someone hits non-existent codes
"""

import redis
import json
import sqlite3
import time
import threading
import os

# ── Redis connection ───────────────────────────────────────────────────────────
# REDIS_URL is injected by docker-compose as an environment variable.
# Default points to localhost for running outside Docker.
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DB_PATH   = os.getenv("DB_PATH", "links.db")

r = redis.from_url(REDIS_URL, decode_responses=True)
# ──────────────────────────────────────────────────────────────────────────────

# ── TTL constants ──────────────────────────────────────────────────────────────
TTL_URL      = 3600   # 1 hour  — URL data never changes, long TTL is safe
TTL_MISS     = 30     # 30 sec  — negative cache (code not found), short TTL
FLUSH_EVERY  = 30     # seconds — how often write-behind flushes hits to DB
# ──────────────────────────────────────────────────────────────────────────────

# ── In-process hit/miss counters (for /cache/stats endpoint) ──────────────────
_stats = {"hits": 0, "misses": 0}
# ──────────────────────────────────────────────────────────────────────────────


# =============================================================================
# Strategy A — Cache-Aside: URL Resolution
# =============================================================================

def get_cached_link(code: str) -> dict | None:
    """
    Cache-aside READ.
    Returns cached link data if present, None on miss.
    Caller is responsible for fetching from DB on None and calling set_cached_link.
    """
    raw = r.get(f"link:{code}")

    if raw == "__NOT_FOUND__":
        # Negative cache hit — we already know this code doesn't exist
        _stats["hits"] += 1
        return {"__not_found__": True}

    if raw:
        _stats["hits"] += 1
        return json.loads(raw)

    # Cache miss — caller must go to DB
    _stats["misses"] += 1
    return None


def set_cached_link(code: str, data: dict):
    """
    Cache-aside WRITE.
    Called after a DB read to populate the cache for subsequent requests.
    """
    r.setex(f"link:{code}", TTL_URL, json.dumps(data))


def set_negative_cache(code: str):
    """
    Cache that this code does NOT exist.
    Prevents repeated DB lookups for invalid/non-existent codes.
    Example: bot scanning /aaa /aab /aac → each would hit DB without this.
    """
    r.setex(f"link:{code}", TTL_MISS, "__NOT_FOUND__")


def invalidate_link(code: str):
    """
    Cache invalidation on write.
    Called when a new short code is created so any negative cache is cleared.
    """
    r.delete(f"link:{code}")


# =============================================================================
# Strategy B — Write-Behind: Hit Counter
# =============================================================================

def increment_hit_counter(code: str) -> int:
    """
    Write-behind WRITE.
    Increments hit counter in Redis atomically.
    Does NOT touch the database — the background flusher handles that.

    Returns the current in-memory hit count (may be ahead of DB by up to FLUSH_EVERY seconds).
    """
    return r.incr(f"hits:{code}")


def get_hit_count_from_cache(code: str) -> int:
    """
    Returns the pending (unflushed) hit count from Redis.
    Used to show accurate hits in API response before DB flush.
    """
    val = r.get(f"hits:{code}")
    return int(val) if val else 0


# =============================================================================
# Background Thread — Write-Behind Flusher
# =============================================================================

def _flush_hit_counters():
    """
    Runs every FLUSH_EVERY seconds.
    Reads all pending hit counters from Redis and writes them to SQLite in batch.

    GETDEL is atomic — it gets the value AND deletes the key in one operation.
    This prevents double-counting if the flusher runs while new hits come in.
    """
    while True:
        time.sleep(FLUSH_EVERY)
        try:
            keys = r.keys("hits:*")
            if not keys:
                continue

            conn = sqlite3.connect(DB_PATH, timeout=10)
            with conn:
                for key in keys:
                    count = r.getdel(key)    # atomic get + delete
                    if count and int(count) > 0:
                        code = key[len("hits:"):]
                        conn.execute(
                            "UPDATE links SET hits = hits + ? WHERE short_code = ?",
                            (int(count), code)
                        )
            conn.close()
            print(f"[cache] Flushed {len(keys)} hit counter(s) to DB")

        except Exception as e:
            print(f"[cache] Flush error: {e}")


def start_hit_flusher():
    """
    Starts the write-behind background thread.
    Called once at app startup. daemon=True so it stops when the app stops.
    """
    t = threading.Thread(target=_flush_hit_counters, daemon=True)
    t.start()
    print(f"[cache] Hit counter flusher started (interval: {FLUSH_EVERY}s)")


# =============================================================================
# Cache Stats — for monitoring
# =============================================================================

def get_cache_stats() -> dict:
    """
    Returns cache performance metrics.
    Exposed via GET /cache/stats in app.py.
    """
    total = _stats["hits"] + _stats["misses"]
    hit_rate = round((_stats["hits"] / total * 100), 1) if total > 0 else 0

    try:
        info        = r.info("memory")
        used_memory = info.get("used_memory_human", "unknown")
        evictions   = r.info("stats").get("evicted_keys", 0)
    except Exception:
        used_memory = "unavailable"
        evictions   = "unavailable"

    return {
        "hits"        : _stats["hits"],
        "misses"      : _stats["misses"],
        "hit_rate_pct": hit_rate,
        "redis_memory": used_memory,
        "evictions"   : evictions,
        "cached_keys" : len(r.keys("link:*")),
        "pending_hits": len(r.keys("hits:*")),
    }
