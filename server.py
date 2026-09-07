#!/usr/bin/env python3
"""
Cost telemetry ingest server for the Pi cost-telemetry extension.

POST per-turn usage JSON here; the Bearer key maps the request to a
configured session bucket, and the turn is stored in SQLite.

Config (passed via --config):
    {
      "session-a": { "keys": ["key-a", "key-b"] },
      "session-b": { "keys": ["key-c", "key-d"] }
    }

Run:
    python3 server.py --config config.json --db telemetry.db --listen-port 8000

Endpoints:
    POST /ingest  (or POST /)  Bearer-keyed turn payload, replies {}
    GET  /summary?session=&model=&since=&until=|&at=
         Totals grouped by session+model. Periods are strictly YYYY,
         YYYY-MM, or YYYY-MM-DD; since/until must share granularity,
         at selects one whole period and cannot combine with since/until.
         Boundaries are computed in UTC unless &tz=<IANA name> is given.
         Replies {"rows": [...]}, without firstMs/lastMs.
    GET  /health  Replies {"ok": true}.
"""

# Stdlib-only HTTP + SQLite server; threading via ThreadingHTTPServer.
import argparse
import datetime as _dt
import http.server
import json
import os
import re
import sqlite3
import sys
import threading
import traceback
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Turns table: one row per ingested turn; payload keeps the raw JSON.
SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at_ms INTEGER NOT NULL,
    session TEXT NOT NULL,
    pi_session_id TEXT NOT NULL DEFAULT '',
    turn_index INTEGER NOT NULL DEFAULT 0,
    event_ts_ms INTEGER NOT NULL DEFAULT 0,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    response_model TEXT NOT NULL DEFAULT '',
    thinking_level TEXT NOT NULL DEFAULT '',
    stop_reason TEXT NOT NULL DEFAULT '',
    in_tokens INTEGER NOT NULL DEFAULT 0,
    out_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    cache_write INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    cost_in REAL NOT NULL DEFAULT 0,
    cost_out REAL NOT NULL DEFAULT 0,
    cost_cache_read REAL NOT NULL DEFAULT 0,
    cost_cache_write REAL NOT NULL DEFAULT 0,
    cost_total REAL NOT NULL DEFAULT 0,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_turns_session_time ON turns (session, received_at_ms);
CREATE INDEX IF NOT EXISTS idx_turns_model ON turns (model);
CREATE INDEX IF NOT EXISTS idx_turns_time ON turns (received_at_ms);
"""


# Strict period shape: YYYY, YYYY-MM, or YYYY-MM-DD (validated further below).
PERIOD_RE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")
def parse_period(value, tz):
    """Convert a period string to a UTC-millisecond window.

    Args:
        value: Period text ("YYYY", "YYYY-MM", or "YYYY-MM-DD").
        tz: ZoneInfo used to interpret local day/month/year boundaries.

    Returns:
        Tuple of (start_ms, end_ms_exclusive, granularity) where
        granularity is "year", "month", or "day"; None if invalid.
    """
    # Reject anything that is not exactly YYYY[-MM[-DD]].
    m = PERIOD_RE.match(value or "")
    if not m:
        return None
    year, month, day = m.groups()
    y = int(year)
    # Year case: Jan 1 to Jan 1 of the next year.
    if month is None:
        start = _dt.datetime(y, 1, 1, tzinfo=tz)
        end = _dt.datetime(y + 1, 1, 1, tzinfo=tz)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000), "year"
    # Month must be 01-12; day stays None here for the month branch.
    mo = int(month)
    if not 1 <= mo <= 12:
        return None
    if day is None:
        # Month case: first of month to first of next month (handles Dec).
        start = _dt.datetime(y, mo, 1, tzinfo=tz)
        end = _dt.datetime(y + (mo == 12), (mo % 12) + 1, 1, tzinfo=tz)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000), "month"
    # Day case: start of that date through +24h; ValueError covers bad dates.
    try:
        start = _dt.datetime(y, mo, int(day), tzinfo=tz)
    except ValueError:
        return None
    end = start + _dt.timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000), "day"


def load_config(path):
    """Load session config and invert it to a key -> session map.

    Args:
        path: JSON file of {session: {"keys": [...]}}.

    Returns:
        Dict mapping each Bearer key to its session bucket name.

    Raises:
        ValueError: If the same key appears under two sessions.
    """
    # Read the raw session -> {keys} mapping from disk.
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    # Invert to key -> session for O(1) auth lookup per request.
    key_to_session = {}
    for session, entry in raw.items():
        for key in (entry or {}).get("keys", []):
            if key in key_to_session:
                raise ValueError(f"duplicate key for {session!r}")
            key_to_session[key] = session
    return key_to_session


def open_db(path):
    """Open (and init) the SQLite store.

    Args:
        path: Filesystem path, or ":memory:" for an ephemeral DB.

    Returns:
        sqlite3.Connection with WAL mode and the turns schema applied.
    """
    # Ensure the parent dir exists for file-backed DBs.
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # check_same_thread=False: guarded externally by db_lock.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA)
    return conn


def make_handler(key_to_session, db, db_lock):
    """Build the per-process request handler class.

    Args:
        key_to_session: Bearer key -> session bucket mapping.
        db: Shared SQLite connection.
        db_lock: Threading lock serializing DB access.

    Returns:
        TelemetryHandler subclass bound to the given config and DB.
    """

    class TelemetryHandler(http.server.BaseHTTPRequestHandler):
        """Routes /ingest, /summary, /health; one instance per request."""

        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            """Write access logs to stderr (default goes to stderr anyway)."""
            sys.stderr.write(
                "%s - - [%s] %s\n"
                % (self.client_address[0], self.log_date_time_string(), fmt % args)
            )

        def send_json(self, status, obj):
            """Send obj as JSON with explicit length and closed connection."""
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def bearer_key(self):
            """Extract the API key from Bearer or X-API-Key headers.

            Returns:
                The raw key string, or "" when no credential was sent.
            """
            # Preferred scheme: "Authorization: Bearer <key>".
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return auth[len("Bearer "):].strip()
            # Fallback for simple clients: "X-API-Key: <key>".
            if self.headers.get("X-API-Key"):
                return self.headers["X-API-Key"].strip()
            return ""

        def do_GET(self):
            """Route GET /health, GET /summary, else 404."""
            try:
                if self.path == "/health" or self.path == "/health/":
                    # Liveness probe, no auth required.
                    self.send_json(200, {"ok": True})
                elif self.path.startswith("/summary"):
                    # Auth first: unknown keys get 401 before any DB work.
                    if self.bearer_key() not in key_to_session:
                        self.send_json(401, {"error": "unknown api key"})
                        return
                    self.handle_summary()
                else:
                    self.send_json(404, {"error": "not found"})
            except Exception:
                # Never leak tracebacks to clients; log server-side.
                traceback.print_exc()
                try:
                    self.send_json(500, {"error": "internal error"})
                except Exception:
                    pass
            finally:
                # Force close: we always send Connection: close.
                self.close_connection = True

        def do_POST(self):
            """Route POST / or /ingest: auth, parse JSON, store turn."""
            try:
                # Only ingest paths accept POSTs.
                if self.path not in ("/", "/ingest", "/ingest/"):
                    self.send_json(404, {"error": "not found"})
                    return
                # Map the Bearer key to its session bucket.
                session = key_to_session.get(self.bearer_key())
                if session is None:
                    self.send_json(401, {"error": "unknown api key"})
                    return
                # Read exactly Content-Length bytes (empty body -> b"").
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b""
                # Body must be a JSON object; reject bad UTF-8/JSON.
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    self.send_json(400, {"error": "invalid json"})
                    return
                if not isinstance(payload, dict):
                    self.send_json(400, {"error": "object expected"})
                    return
                # Persist the turn, then ack with an empty object.
                self.store_turn(session, payload)
                self.send_json(200, {})
            except Exception:
                traceback.print_exc()
                try:
                    self.send_json(500, {"error": "internal error"})
                except Exception:
                    pass
            finally:
                self.close_connection = True

        def store_turn(self, session, p):
            """Insert one turn payload into the turns table.

            Args:
                session: Bucket name derived from the Bearer key.
                p: Decoded ingest JSON (usage/cost/model metadata).
            """
            # Split nested usage/cost blocks; default missing numerics to 0.
            usage = p.get("usage") or {}
            cost = p.get("cost") or {}
            row = (
                # Server receipt time, not client event time, for windowing.
                int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp() * 1000),
                session,
                p.get("sessionId", ""),
                p.get("turnIndex", 0),
                p.get("timestamp", 0),
                p.get("provider", ""),
                p.get("model", ""),
                p.get("responseModel") or "",
                p.get("thinkingLevel") or "",
                p.get("stopReason", ""),
                usage.get("input", 0),
                usage.get("output", 0),
                usage.get("cacheRead", 0),
                usage.get("cacheWrite", 0),
                usage.get("totalTokens", 0),
                usage.get("reasoning", 0),
                cost.get("input", 0),
                cost.get("output", 0),
                cost.get("cacheRead", 0),
                cost.get("cacheWrite", 0),
                cost.get("total", 0),
                # Keep full payload for debugging/reprocessing.
                json.dumps(p),
            )
            # Serialize writes: sqlite connection is shared across threads.
            with db_lock:
                db.execute(
                    "INSERT INTO turns (received_at_ms, session, pi_session_id,"
                    " turn_index, event_ts_ms, provider, model, response_model,"
                    " thinking_level, stop_reason, in_tokens, out_tokens,"
                    " cache_read, cache_write, total_tokens, reasoning_tokens,"
                    " cost_in, cost_out, cost_cache_read, cost_cache_write,"
                    " cost_total, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    row,
                )
                db.commit()

        def handle_summary(self):
            """Aggregate turns by session+model for the requested window."""
            from urllib.parse import urlparse, parse_qs

            # parse_qs gives {k: [v]}; unwrap to single values.
            qs = parse_qs(urlparse(self.path).query)
            get = lambda k: (qs.get(k) or [None])[0]
            clauses = []
            params: list = []
            session = get("session")
            model = get("model")
            at = get("at")
            since = get("since")
            until = get("until")
            # Default to UTC when no tz is supplied.
            tz_name = get("tz") or "UTC"
            try:
                tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                self.send_json(400, {"error": f"unknown timezone: {tz_name}"})
                return
            # at is a single whole period; it excludes since/until.
            if at is not None and (since is not None or until is not None):
                self.send_json(400, {"error": "at cannot combine with since/until"})
                return
            lo, hi = None, None
            if at is not None:
                # Single-period window: [start, end).
                parsed = parse_period(at, tz)
                if parsed is None:
                    self.send_json(400, {"error": "at must be YYYY, YYYY-MM, or YYYY-MM-DD"})
                    return
                lo, hi = parsed[0], parsed[1]
            else:
                # Range window: since gives inclusive start, until gives exclusive end.
                since_p = parse_period(since, tz) if since is not None else None
                until_p = parse_period(until, tz) if until is not None else None
                if since is not None and since_p is None:
                    self.send_json(400, {"error": "since must be YYYY, YYYY-MM, or YYYY-MM-DD"})
                    return
                if until is not None and until_p is None:
                    self.send_json(400, {"error": "until must be YYYY, YYYY-MM, or YYYY-MM-DD"})
                    return
                # Mixed granularities (e.g. month + day) would be ambiguous.
                if since_p is not None and until_p is not None and since_p[2] != until_p[2]:
                    self.send_json(400, {"error": "since and until must share granularity"})
                    return
                if since_p is not None:
                    lo = since_p[0]
                if until_p is not None:
                    hi = until_p[1]
            # Optional exact-match filters.
            if session:
                clauses.append("session = ?")
                params.append(session)
            if model:
                clauses.append("model = ?")
                params.append(model)
            # Time bounds apply to server receipt time.
            if lo is not None:
                clauses.append("received_at_ms >= ?")
                params.append(lo)
            if hi is not None:
                clauses.append("received_at_ms < ?")
                params.append(hi)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            # Grouped totals; NULL sums cannot occur (columns default to 0).
            with db_lock:
                cur = db.execute(
                    "SELECT session, model, COUNT(*),"
                    " SUM(in_tokens), SUM(out_tokens), SUM(cache_read),"
                    " SUM(cache_write), SUM(total_tokens), SUM(reasoning_tokens),"
                    " SUM(cost_in), SUM(cost_out), SUM(cost_cache_read),"
                    " SUM(cost_cache_write), SUM(cost_total)"
                    f" FROM turns {where} GROUP BY session, model"
                    " ORDER BY session, model",
                    params,
                )
                rows = [
                    {
                        "session": r[0],
                        "model": r[1],
                        "turns": r[2],
                        "inTokens": r[3],
                        "outTokens": r[4],
                        "cacheRead": r[5],
                        "cacheWrite": r[6],
                        "totalTokens": r[7],
                        "reasoningTokens": r[8],
                        "costIn": r[9],
                        "costOut": r[10],
                        "costCacheRead": r[11],
                        "costCacheWrite": r[12],
                        "costTotal": r[13],
                    }
                    for r in cur.fetchall()
                ]
            self.send_json(200, {"rows": rows})

    return TelemetryHandler


def main():
    """Start the telemetry HTTP server.

    Loads key config, opens SQLite, and serves /ingest, /summary,
    and /health until interrupted.

    Returns:
        None.
    """
    # CLI: config maps keys to sessions; db selects the sqlite file.
    parser = argparse.ArgumentParser(description="Pi cost-telemetry ingest server.")
    parser.add_argument("-c", "--config", required=True, help="session -> keys JSON file")
    parser.add_argument("-d", "--db", default="telemetry.db", help="sqlite file")
    parser.add_argument("-b", "--listen-host", default="127.0.0.1", help="bind address")
    parser.add_argument("-p", "--listen-port", type=int, default=8000, help="listen port")
    args = parser.parse_args()

    # Load auth map and init DB before binding the port.
    key_to_session = load_config(args.config)
    db = open_db(args.db)
    db_lock = threading.Lock()

    # Threaded server so concurrent POSTs do not block each other.
    handler = make_handler(key_to_session, db, db_lock)
    server = http.server.ThreadingHTTPServer(
        (args.listen_host, args.listen_port), handler
    )
    # Keep worker threads from blocking process exit.
    server.daemon_threads = True

    print(
        f"Listening on http://{args.listen_host}:{args.listen_port}"
        f" ({len(key_to_session)} keys) -> {os.path.abspath(args.db)}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # Clean Ctrl-C shutdown.
        print("\nShutting down...", flush=True)
    finally:
        server.server_close()
        db.close()


if __name__ == "__main__":
    main()
